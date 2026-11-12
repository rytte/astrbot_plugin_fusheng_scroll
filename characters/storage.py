from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from ..core.domain import StudioError, canonical, digest, record_ref
from .codec import DEFAULT_OPTIONS, load_json, normalize_card


class CardStorage:
    """Shares the studio connection; card saves and milestones use one transaction."""

    JSON_FIELDS = ("document", "owner", "options", "baseline")

    def __init__(self, storage):
        self.storage = storage
        self.connection = storage.connection
        self.assets = storage.path.parent / "assets"

    @staticmethod
    def decode(row) -> dict | None:
        if row is None:
            return None
        import json

        result = dict(row)
        try:
            for key in CardStorage.JSON_FIELDS:
                result[key] = (
                    json.loads(result[key]) if result[key] is not None else None
                )
            result["document"] = normalize_card(result["document"])
            if result["owner"] is not None and (
                not isinstance(result["owner"], list)
                or len(result["owner"]) != 2
                or any(
                    not isinstance(item, str) or not item for item in result["owner"]
                )
            ):
                raise ValueError("invalid owner")
            if not isinstance(result["options"], dict):
                raise ValueError("invalid options")
        except (ValueError, KeyError, TypeError, StudioError) as exc:
            raise StudioError("storage_error", "角色卡数据损坏，已拒绝访问。") from exc
        return result

    def get(self, card_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM cards WHERE card_id=?", (card_id,)
        ).fetchone()
        card = self.decode(row)
        if card is None:
            raise StudioError("not_found_or_forbidden", "角色卡不存在或无权访问。")
        return card

    def by_persona(self, persona_id: str) -> dict | None:
        return self.decode(
            self.connection.execute(
                "SELECT * FROM cards WHERE persona_id=? AND deleted=0", (persona_id,)
            ).fetchone()
        )

    def list(self, actor, *, deleted: bool = False) -> list[dict]:
        query = "SELECT * FROM cards WHERE deleted=?"
        args = [int(deleted)]
        if not actor.admin:
            query += " AND owner=?"
            args.append(canonical(actor.owner))
        return [
            self.decode(row)
            for row in self.connection.execute(
                query + " ORDER BY updated_at DESC, card_id", args
            )
        ]

    def known_personas(self) -> tuple[set[str], set[tuple[str, str]]]:
        bound = {
            row[0]
            for row in self.connection.execute(
                "SELECT persona_id FROM cards WHERE persona_id IS NOT NULL"
            )
        }
        seen = {
            tuple(row)
            for row in self.connection.execute(
                "SELECT persona_id, persona_ref FROM card_persona_sources "
                "UNION SELECT persona_id, persona_ref FROM card_deleted_personas"
            )
        }
        return bound, seen

    def resource_refs(self, contents):
        """Find both original images and the cache keys used by saved documents."""
        assets, keys = set(), set()
        for document, asset_id in contents:
            if asset_id:
                assets.add(asset_id)
            # Preserve every declared image, including inactive/ambiguous icons.
            # Such a card must remain deletable even if its avatar cannot render.
            for resource in document["data"].get("assets") or []:
                uri = resource.get("uri")
                if not isinstance(uri, str) or uri.lower() == "ccdefault:":
                    continue
                keys.add(
                    digest(
                        {
                            "uri": uri,
                            "container": asset_id
                            if uri.startswith("__asset:")
                            else None,
                        }
                    )
                )
        return assets, keys

    def resource_contents(self, *, card_id=None):
        condition = " WHERE card_id=?" if card_id is not None else ""
        args = (card_id,) if card_id is not None else ()
        contents = [
            (load_json(row["document"]), row["asset_id"])
            for row in self.connection.execute(
                "SELECT document, asset_id FROM cards" + condition, args
            )
        ]
        for row in self.connection.execute(
            "SELECT snapshot FROM card_snapshots" + condition, args
        ):
            snapshot = json.loads(row["snapshot"])
            contents.append((snapshot["document"], snapshot["asset_id"]))
        for row in self.connection.execute(
            "SELECT document, asset_id FROM card_imports"
            + (
                condition
                if card_id is not None
                else " WHERE card_id IS NOT NULL OR created_at>=?"
            ),
            args if card_id is not None else (time.time() - 1800,),
        ):
            contents.append((load_json(row["document"]), row["asset_id"]))
        return contents

    def delete(self, actor, card, expected_revision):
        """Remove archived content atomically; file removal is retried separately."""
        card_id = card["card_id"]
        assets, keys = self.resource_refs(self.resource_contents(card_id=card_id))
        assets.update(
            row["asset_id"]
            for row in self.connection.execute("SELECT * FROM card_asset_cache")
            if row["cache_key"] in keys
        )
        with self.connection:
            # Keep only the source identity, never the deleted card's content.
            self.connection.execute(
                "INSERT OR IGNORE INTO card_deleted_personas "
                "SELECT persona_id, persona_ref FROM card_persona_sources WHERE card_id=?",
                (card_id,),
            )
            if card["persona_id"] and card["persona_ref"]:
                self.connection.execute(
                    "INSERT OR IGNORE INTO card_deleted_personas VALUES (?, ?)",
                    (card["persona_id"], card["persona_ref"]),
                )
            self.connection.executemany(
                "INSERT OR IGNORE INTO card_asset_garbage VALUES (?)",
                [(asset_id,) for asset_id in assets],
            )
            for table in ("card_snapshots", "card_imports", "card_persona_sources"):
                self.connection.execute(
                    f"DELETE FROM {table} WHERE card_id=?", (card_id,)
                )
            result = self.connection.execute(
                "DELETE FROM cards WHERE card_id=? AND revision=? AND deleted=1 AND locked=0",
                (card_id, expected_revision),
            )
            if result.rowcount != 1:
                raise StudioError(
                    "revision_conflict", "角色卡状态已改变，请刷新后再操作。"
                )
            self.storage.audit(actor, "card.delete", card_id, "success")

    def cleanup_assets(self):
        """Collect only queued images, preserving every remaining content reference."""
        queued = [
            row[0]
            for row in self.connection.execute(
                "SELECT asset_id FROM card_asset_garbage"
            )
        ]
        if not queued:
            return 0
        assets, keys = self.resource_refs(self.resource_contents())
        assets.update(
            row[0]
            for row in self.connection.execute(
                "SELECT asset_id FROM card_image_uploads WHERE created_at>=?",
                (time.time() - 1800,),
            )
        )
        cache = list(self.connection.execute("SELECT * FROM card_asset_cache"))
        assets.update(row["asset_id"] for row in cache if row["cache_key"] in keys)
        pending = 0
        for asset_id in queued:
            if asset_id in assets:
                continue
            # Commit cache invalidation first so a failed unlink can safely retry.
            with self.connection:
                self.connection.execute(
                    "DELETE FROM card_asset_cache WHERE asset_id=?", (asset_id,)
                )
            try:
                self.asset_path(asset_id).unlink(missing_ok=True)
            except OSError:
                pending += 1
                continue
            with self.connection:
                self.connection.execute(
                    "DELETE FROM card_asset_garbage WHERE asset_id=?", (asset_id,)
                )
        return pending

    def _remember_persona(self, card_id, persona_id, reference):
        if persona_id and reference:
            self.connection.execute(
                "INSERT OR IGNORE INTO card_persona_sources VALUES (?, ?, ?)",
                (persona_id, reference, card_id),
            )

    def put_asset(self, image: bytes, ext: str = "png") -> str:
        if ext not in ("png", "jpg", "webp"):
            raise StudioError("invalid_asset", "资源格式无效。")
        asset_id = hashlib.sha256(image).hexdigest() + "." + ext
        self.assets.mkdir(parents=True, exist_ok=True)
        path = self.asset_path(asset_id)
        if not path.exists():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=self.assets, prefix=".asset-", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(image)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return asset_id

    def asset_path(self, asset_id: str) -> Path:
        if not isinstance(asset_id, str) or not re.fullmatch(
            r"[0-9a-f]{64}\.(png|jpg|webp)", asset_id
        ):
            raise StudioError("invalid_asset", "资源标识无效。")
        path = self.assets / asset_id
        if path.is_symlink():
            raise StudioError("invalid_asset", "资源文件不能是符号链接。")
        return path

    def cached_asset(self, cache_key: str):
        return self.connection.execute(
            "SELECT asset_id, media_type FROM card_asset_cache WHERE cache_key=?",
            (cache_key,),
        ).fetchone()

    def cache_asset(self, cache_key: str, asset_id: str, media_type: str):
        # The first successful resolution is immutable, including across restarts.
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO card_asset_cache VALUES (?, ?, ?)",
                (cache_key, asset_id, media_type),
            )

    def prepare_image(self, actor, asset_id: str, kind: str) -> str:
        upload_id = str(uuid.uuid4())
        with self.connection:
            self.connection.execute(
                "DELETE FROM card_image_uploads WHERE created_at<?",
                (time.time() - 1800,),
            )
            self.connection.execute(
                "INSERT INTO card_image_uploads VALUES (?, ?, ?, ?, ?)",
                (upload_id, canonical(actor.owner), asset_id, kind, time.time()),
            )
        return upload_id

    def get_image(self, actor, upload_id: str, kind: str) -> str:
        if not isinstance(upload_id, str):
            raise StudioError("invalid_payload", "图片上传标识必须为字符串。")
        row = self.connection.execute(
            "SELECT * FROM card_image_uploads WHERE upload_id=? AND owner=? AND kind=?",
            (upload_id, canonical(actor.owner), kind),
        ).fetchone()
        if row is None or row["created_at"] < time.time() - 1800:
            raise StudioError(
                "upload_expired", "图片上传不存在或已过期，请重新选择图片。"
            )
        return row["asset_id"]

    def prepare_import(self, actor, document: dict, image: bytes | None) -> str:
        document = normalize_card(document)
        import_id = str(uuid.uuid4())
        asset_id = self.put_asset(image) if image else None
        with self.connection:
            self.connection.execute(
                "DELETE FROM card_imports WHERE created_at<? AND card_id IS NULL",
                (time.time() - 1800,),
            )
            self.connection.execute(
                "INSERT INTO card_imports VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    import_id,
                    canonical(actor.owner),
                    canonical(document),
                    asset_id,
                    time.time(),
                ),
            )
        return import_id

    def get_import(self, actor, import_id: str) -> dict:
        if not isinstance(import_id, str):
            raise StudioError("invalid_payload", "import_id 必须是字符串。")
        row = self.connection.execute(
            "SELECT * FROM card_imports WHERE import_id=? AND owner=?",
            (import_id, canonical(actor.owner)),
        ).fetchone()
        if row is None or (
            row["card_id"] is None and row["created_at"] < time.time() - 1800
        ):
            raise StudioError("import_expired", "导入预览不存在或已过期，请重新上传。")
        return {**dict(row), "document": load_json(row["document"])}

    def _snapshot(self, card: dict, reason: str) -> str:
        snapshot_id = str(uuid.uuid4())
        snapshot = {key: card[key] for key in ("document", "options", "asset_id")}
        self.connection.execute(
            "INSERT INTO card_snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                card["card_id"],
                card["document"]["data"]["character_version"],
                time.time(),
                reason,
                canonical(snapshot),
                digest(snapshot),
            ),
        )
        return snapshot_id

    def create(
        self, actor, document, *, options=None, asset_id=None, imported=None
    ) -> dict:
        return self._create(
            document,
            actor.owner,
            options or DEFAULT_OPTIONS,
            asset_id=asset_id,
            imported=imported,
            reason="import" if imported else "create",
        )

    def adopt(self, persona, document, options, owner) -> dict:
        return self._create(document, owner, options, persona=persona, reason="persona")

    def _create(
        self,
        document,
        owner,
        options,
        *,
        asset_id=None,
        imported=None,
        persona=None,
        reason="create",
    ):
        document = normalize_card(document)
        card_id, now = str(uuid.uuid4()), time.time()
        persona_id = persona["persona_id"] if persona else None
        reference = record_ref(persona) if persona else None
        baseline = (
            {
                "managed": {
                    "system_prompt": persona.get("system_prompt"),
                    "begin_dialogs": persona.get("begin_dialogs") or [],
                },
                "options": options,
            }
            if persona
            else None
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, 0, 0, ?, ?)",
                (
                    card_id,
                    canonical(document),
                    canonical(owner),
                    canonical(options),
                    asset_id,
                    persona_id,
                    reference,
                    canonical(baseline) if baseline else None,
                    "synced" if persona else "unbound",
                    now,
                    now,
                ),
            )
            card = self.get(card_id)
            self._snapshot(card, reason)
            self._remember_persona(card_id, persona_id, reference)
            if imported:
                self.connection.execute(
                    "UPDATE card_imports SET card_id=? WHERE import_id=?",
                    (card_id, imported),
                )
        return card

    def save(
        self,
        card: dict,
        expected_revision: int,
        *,
        snapshot: bool = False,
        reason="version",
    ) -> dict:
        card_id = card["card_id"]
        with self.connection:
            result = self.connection.execute(
                """
                UPDATE cards SET document=?, options=?, asset_id=?,
                    revision=revision+1, updated_at=?, sync_status=?, sync_error=NULL,
                    deleted=? WHERE card_id=? AND revision=?
            """,
                (
                    canonical(normalize_card(card["document"])),
                    canonical(card["options"]),
                    card["asset_id"],
                    time.time(),
                    "pending" if card["persona_id"] else "unbound",
                    card["deleted"],
                    card_id,
                    expected_revision,
                ),
            )
            if result.rowcount != 1:
                raise StudioError(
                    "revision_conflict", "角色卡已被其他入口修改，请刷新后再保存。"
                )
            saved = self.get(card_id)
            if snapshot:
                self._snapshot(saved, reason)
        return saved

    def sync_state(
        self,
        card_id: str,
        status: str,
        *,
        error=None,
        persona_id=None,
        persona_ref=None,
        baseline=None,
        unbind=False,
    ):
        with self.connection:
            previous = self.connection.execute(
                "SELECT persona_id, persona_ref FROM cards WHERE card_id=?", (card_id,)
            ).fetchone()
            if previous:
                self._remember_persona(card_id, previous[0], previous[1])
            self.connection.execute(
                "UPDATE cards SET sync_status=?, sync_error=? WHERE card_id=?",
                (status, error, card_id),
            )
            if unbind:
                self.connection.execute(
                    "UPDATE cards SET persona_id=NULL, persona_ref=NULL, baseline=NULL WHERE card_id=?",
                    (card_id,),
                )
            else:
                for column, value in (
                    ("persona_id", persona_id),
                    ("persona_ref", persona_ref),
                    ("baseline", canonical(baseline) if baseline is not None else None),
                ):
                    if value is not None:
                        self.connection.execute(
                            f"UPDATE cards SET {column}=? WHERE card_id=?",
                            (value, card_id),
                        )
                current = self.connection.execute(
                    "SELECT persona_id, persona_ref FROM cards WHERE card_id=?",
                    (card_id,),
                ).fetchone()
                if current:
                    self._remember_persona(card_id, current[0], current[1])

    def set_locked(self, card_id: str, locked: bool):
        with self.connection:
            self.connection.execute(
                "UPDATE cards SET locked=?, revision=revision+1 WHERE card_id=?",
                (int(locked), card_id),
            )

    def snapshots(self, card_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT snapshot_id, character_version, created_at, reason FROM card_snapshots WHERE card_id=? ORDER BY created_at DESC, snapshot_id",
                (card_id,),
            )
        ]

    def snapshot(self, card_id: str, snapshot_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM card_snapshots WHERE card_id=? AND snapshot_id=?",
            (card_id, snapshot_id),
        ).fetchone()
        if row is None:
            raise StudioError("not_found_or_forbidden", "快照不存在或无权访问。")
        import json

        try:
            content = json.loads(row["snapshot"])
            if digest(content) != row["checksum"] or set(content) != {
                "document",
                "options",
                "asset_id",
            }:
                raise ValueError("invalid checksum")
            normalize_card(content["document"])
        except (ValueError, TypeError, KeyError, StudioError) as exc:
            raise StudioError("snapshot_corrupt", "快照校验失败，不能恢复。") from exc
        return {**dict(row), "snapshot": content}
