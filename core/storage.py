from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .domain import CONTENT_FIELDS, StudioError, canonical, digest, record_ref


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        marker = path.with_suffix(".initialized")
        if marker.exists() and (
            marker.read_text(encoding="ascii") != "persona-studio-v1"
            or not path.is_file()
        ):
            raise StudioError(
                "storage_error",
                "已初始化的权限数据库缺失或标记损坏，拒绝重建空权限库。",
            )
        self.connection = sqlite3.connect(path, timeout=5)
        self.path = path
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA synchronous = FULL")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            tables = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if version == 0 and not tables:
                self.connection.executescript("""
                    BEGIN;
                    CREATE TABLE records (
                        chain_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL,
                        persona_ref TEXT NOT NULL, owner TEXT,
                        locked INTEGER NOT NULL CHECK (locked IN (0, 1)),
                        revision INTEGER NOT NULL CHECK (revision > 0),
                        deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
                        UNIQUE (persona_id, persona_ref)
                    );
                    CREATE TABLE backups (
                        backup_id TEXT PRIMARY KEY,
                        chain_id TEXT NOT NULL REFERENCES records(chain_id),
                        created_at REAL NOT NULL, action TEXT NOT NULL,
                        snapshot TEXT NOT NULL, checksum TEXT NOT NULL
                    );
                    CREATE INDEX backups_chain ON backups(chain_id, created_at);
                    CREATE TABLE audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at REAL NOT NULL, entry TEXT NOT NULL
                    );
                    PRAGMA user_version = 1;
                    COMMIT;
                """)
            elif version not in (1, 2, 3, 4, 5, 6):
                raise StudioError(
                    "storage_error", "不支持的权限数据库版本，不进行兼容迁移。"
                )
            if version in (0, 1):
                self.connection.executescript("""
                    BEGIN;
                    CREATE TABLE cards (
                        card_id TEXT PRIMARY KEY, document TEXT NOT NULL,
                        owner TEXT NOT NULL, options TEXT NOT NULL,
                        asset_id TEXT, persona_id TEXT UNIQUE, persona_ref TEXT,
                        baseline TEXT, sync_status TEXT NOT NULL,
                        sync_error TEXT, revision INTEGER NOT NULL DEFAULT 1,
                        locked INTEGER NOT NULL DEFAULT 0,
                        deleted INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL
                    );
                    CREATE TABLE card_snapshots (
                        snapshot_id TEXT PRIMARY KEY,
                        card_id TEXT NOT NULL REFERENCES cards(card_id),
                        character_version TEXT NOT NULL, created_at REAL NOT NULL,
                        reason TEXT NOT NULL, snapshot TEXT NOT NULL,
                        checksum TEXT NOT NULL
                    );
                    CREATE INDEX card_snapshots_card ON card_snapshots(card_id, created_at);
                    CREATE TABLE card_imports (
                        import_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                        document TEXT NOT NULL, asset_id TEXT,
                        created_at REAL NOT NULL, card_id TEXT
                    );
                    PRAGMA user_version = 2;
                    COMMIT;
                """)
            if version in (0, 1, 2):
                self.connection.executescript("""
                    BEGIN;
                    CREATE TABLE card_persona_sources (
                        persona_id TEXT NOT NULL, persona_ref TEXT NOT NULL,
                        card_id TEXT NOT NULL REFERENCES cards(card_id),
                        PRIMARY KEY (persona_id, persona_ref)
                    );
                    INSERT INTO card_persona_sources
                        SELECT persona_id, persona_ref, card_id FROM cards
                        WHERE persona_id IS NOT NULL AND persona_ref IS NOT NULL;
                    PRAGMA user_version = 3;
                    COMMIT;
                """)
            if version in (0, 1, 2, 3):
                self.connection.executescript("""
                    BEGIN;
                    CREATE TABLE card_asset_cache (
                        cache_key TEXT PRIMARY KEY, asset_id TEXT NOT NULL,
                        media_type TEXT NOT NULL
                    );
                    PRAGMA user_version = 4;
                    COMMIT;
                """)
            if version in (0, 1, 2, 3, 4):
                self.connection.executescript("""
                    BEGIN;
                    CREATE TABLE card_image_uploads (
                        upload_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                        asset_id TEXT NOT NULL, kind TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                    PRAGMA user_version = 5;
                    COMMIT;
                """)
            if version in (0, 1, 2, 3, 4, 5):
                self.connection.executescript("""
                    BEGIN;
                    CREATE TABLE card_deleted_personas (
                        persona_id TEXT NOT NULL, persona_ref TEXT NOT NULL,
                        PRIMARY KEY (persona_id, persona_ref)
                    );
                    CREATE TABLE card_asset_garbage (
                        asset_id TEXT PRIMARY KEY
                    );
                    PRAGMA user_version = 6;
                    COMMIT;
                """)
            if self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise StudioError("storage_error", "权限数据库完整性检查失败。")
            self.records()
            if not marker.exists():
                with marker.open("x", encoding="ascii") as stream:
                    stream.write("persona-studio-v1")
                    stream.flush()
                    os.fsync(stream.fileno())
        except BaseException:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def records(self, persona_id: str | None = None) -> list[dict]:
        rows = self.connection.execute(
            "SELECT * FROM records"
            + (" WHERE persona_id=?" if persona_id is not None else ""),
            (persona_id,) if persona_id is not None else (),
        ).fetchall()
        result = []
        for row in rows:
            record = dict(row)
            try:
                record["owner"] = (
                    json.loads(record["owner"]) if record["owner"] is not None else None
                )
                reference = json.loads(record["persona_ref"])
                owner = record["owner"]
                if owner is not None and (
                    not isinstance(owner, list)
                    or len(owner) != 2
                    or any(not isinstance(item, str) or not item for item in owner)
                ):
                    raise ValueError("invalid owner")
                if (
                    record["locked"] not in (0, 1)
                    or record["deleted"] not in (0, 1)
                    or type(record["revision"]) is not int
                    or record["revision"] < 1
                ):
                    raise ValueError("invalid state")
                if type(reference.get("id")) is not int or not isinstance(
                    reference.get("created_at"), str
                ):
                    raise ValueError("invalid reference")
                uuid.UUID(record["chain_id"])
            except (ValueError, TypeError, AttributeError) as exc:
                raise StudioError(
                    "storage_error", "人格权限元数据损坏，已拒绝访问。"
                ) from exc
            result.append(record)
        return result

    def find(self, persona_id: str, reference: str) -> dict | None:
        return next(
            (
                record
                for record in self.records(persona_id)
                if record["persona_ref"] == reference
            ),
            None,
        )

    def register(
        self, persona_id: str, reference: str, owner: list[str] | None
    ) -> dict:
        record = {
            "chain_id": str(uuid.uuid4()),
            "persona_id": persona_id,
            "persona_ref": reference,
            "owner": owner,
            "locked": 0,
            "revision": 1,
            "deleted": 0,
        }
        with self.connection:
            self.connection.execute(
                "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record["chain_id"],
                    persona_id,
                    reference,
                    canonical(owner) if owner is not None else None,
                    0,
                    1,
                    0,
                ),
            )
        return record

    def save_record(self, record: dict) -> None:
        with self.connection:
            result = self.connection.execute(
                "UPDATE records SET persona_ref=?, locked=?, revision=?, deleted=? WHERE chain_id=?",
                (
                    record["persona_ref"],
                    record["locked"],
                    record["revision"],
                    record["deleted"],
                    record["chain_id"],
                ),
            )
            if result.rowcount != 1:
                raise StudioError("storage_error", "人格版本链不存在，不能保存元数据。")

    def backup(self, record: dict, persona: dict, action: str) -> str:
        backup_id = str(uuid.uuid4())
        snapshot = {
            "backup_id": backup_id,
            "created_at": time.time(),
            "action": action,
            "chain_id": record["chain_id"],
            "revision": record["revision"],
            "original_owner": record["owner"],
            "persona_ref": record["persona_ref"],
            "persona": persona,
        }
        with self.connection:
            self.connection.execute(
                "INSERT INTO backups VALUES (?, ?, ?, ?, ?, ?)",
                (
                    backup_id,
                    record["chain_id"],
                    snapshot["created_at"],
                    action,
                    canonical(snapshot),
                    digest(snapshot),
                ),
            )
        return backup_id

    def backups(self, chain_id: str) -> list[dict]:
        rows = self.connection.execute(
            "SELECT backup_id, created_at, action FROM backups WHERE chain_id=? ORDER BY created_at DESC, backup_id DESC",
            (chain_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_backup(self, backup_id: str, actor) -> tuple[dict, dict]:
        row = self.connection.execute(
            "SELECT * FROM backups WHERE backup_id=?", (backup_id,)
        ).fetchone()
        if row is None:
            raise StudioError("not_found_or_forbidden", "备份不存在或无权访问。")
        record = next(
            (item for item in self.records() if item["chain_id"] == row["chain_id"]),
            None,
        )
        if record is None:
            raise StudioError("storage_error", "备份的权限元数据缺失。")
        if not actor.admin and record["owner"] != actor.owner:
            raise StudioError("not_found_or_forbidden", "备份不存在或无权访问。")
        try:
            snapshot = json.loads(row["snapshot"])
            if not isinstance(snapshot, dict) or set(snapshot) != {
                "backup_id",
                "created_at",
                "action",
                "chain_id",
                "revision",
                "original_owner",
                "persona_ref",
                "persona",
            }:
                raise ValueError("invalid snapshot schema")
            if not isinstance(snapshot["persona"], dict) or not CONTENT_FIELDS <= set(
                snapshot["persona"]
            ):
                raise ValueError("incomplete persona snapshot")
            if snapshot["persona_ref"] != record_ref(snapshot["persona"]):
                raise ValueError("invalid snapshot reference")
            if (
                not isinstance(snapshot, dict)
                or digest(snapshot) != row["checksum"]
                or snapshot["backup_id"] != backup_id
                or snapshot["chain_id"] != record["chain_id"]
                or snapshot["original_owner"] != record["owner"]
                or snapshot["persona"]["persona_id"] != record["persona_id"]
                or not isinstance(snapshot["created_at"], int | float)
                or type(snapshot["revision"]) is not int
                or snapshot["revision"] < 1
            ):
                raise ValueError("invalid snapshot")
        except (ValueError, TypeError, KeyError, StudioError) as exc:
            raise StudioError(
                "backup_corrupt", "备份内容或校验值损坏，不能使用。"
            ) from exc
        return record, snapshot

    def remove_backup(self, backup_id: str) -> None:
        with self.connection:
            result = self.connection.execute(
                "DELETE FROM backups WHERE backup_id=?", (backup_id,)
            )
            if result.rowcount != 1:
                raise StudioError("not_found_or_forbidden", "备份不存在或已删除。")

    def prune(self, record: dict, count: int, days: int) -> list[str]:
        if record["locked"]:
            return []
        cutoff = time.time() - days * 86400
        removed = [
            item["backup_id"]
            for index, item in enumerate(self.backups(record["chain_id"]))
            if index >= count or item["created_at"] < cutoff
        ]
        if removed:
            with self.connection:
                self.connection.executemany(
                    "DELETE FROM backups WHERE backup_id=?",
                    [(backup_id,) for backup_id in removed],
                )
        return removed

    def audit(
        self,
        actor,
        action: str,
        target: str | None,
        result: str,
        *,
        reason: str | None = None,
        changed_fields=(),
    ) -> None:
        entry = {
            "actor": actor.owner if actor is not None else None,
            "action": action,
            "target": target,
            "fields": sorted(changed_fields),
            "result": result,
            "reason": reason,
        }
        with self.connection:
            self.connection.execute(
                "INSERT INTO audit(created_at, entry) VALUES (?, ?)",
                (time.time(), canonical(entry)),
            )
