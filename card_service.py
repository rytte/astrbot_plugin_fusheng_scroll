from __future__ import annotations

import asyncio
import copy
import time

from astrbot.api import logger

from .card_assets import (
    CardAssets,
    CardImage,
    image_info,
    image_png,
    main_icon,
    prepare_image,
    set_main_icon,
)
from .card_storage import CardStorage
from .cards import (
    DEFAULT_OPTIONS,
    compile_card,
    export_png,
    from_persona,
    import_card,
    normalize_card,
    persona_origin,
    render_macros,
)
from .domain import (
    CONTENT_FIELDS,
    Actor,
    StudioError,
    check_payload,
    digest,
    identify,
    record_ref,
    validate_content,
    validate_id,
)

CARD_ACTIONS = {
    "preview": ({"card", "options"}, {"card"}),
    "list": ({"offset", "limit", "deleted"}, set()),
    "get": ({"card_id"}, {"card_id"}),
    "create": ({"card", "import_id", "options", "bind"}, set()),
    "update": (
        {
            "card_id",
            "expected_revision",
            "card",
            "data",
            "options",
            "import_id",
            "images",
        },
        {"card_id", "expected_revision"},
    ),
    "sync": (
        {"card_id", "expected_revision", "overwrite", "expected_core_token"},
        {"card_id", "expected_revision"},
    ),
    "bind": (
        {"card_id", "expected_revision", "persona_id"},
        {"card_id", "expected_revision"},
    ),
    "unbind": ({"card_id", "expected_revision"}, {"card_id", "expected_revision"}),
    "archive": ({"card_id", "expected_revision"}, {"card_id", "expected_revision"}),
    "unarchive": ({"card_id", "expected_revision"}, {"card_id", "expected_revision"}),
    "delete": (
        {"card_id", "expected_revision", "confirm_name"},
        {"card_id", "expected_revision", "confirm_name"},
    ),
    "snapshots": ({"card_id"}, {"card_id"}),
    "snapshot": ({"card_id", "snapshot_id"}, {"card_id", "snapshot_id"}),
    "restore": (
        {"card_id", "snapshot_id", "expected_revision"},
        {"card_id", "snapshot_id", "expected_revision"},
    ),
    "lock": (
        {"card_id", "expected_revision", "locked"},
        {"card_id", "expected_revision", "locked"},
    ),
}


class CardStudio:
    def __init__(self, studio):
        self.studio = studio
        self.storage = CardStorage(studio.storage)
        self.images = CardAssets(self.storage)

    @staticmethod
    def web_actor(username: str) -> Actor:
        if not isinstance(username, str) or not username.strip():
            raise StudioError("forbidden", "请先登录 AstrBot WebUI。")
        return Actor("webui", username, True)

    def authorize(self, actor, card, *, write=False, allow_deleted=False):
        if (not actor.admin and card["owner"] != actor.owner) or (
            card["deleted"] and not allow_deleted
        ):
            raise StudioError("not_found_or_forbidden", "角色卡不存在或无权访问。")
        if write and card["locked"]:
            raise StudioError("card_locked", "角色卡已锁定，请先由真实用户解锁。")

    def reauthorize(self, actor, event):
        if event is not None:
            current = identify(event, self.studio.settings())
            if current != actor:
                raise StudioError("forbidden", "调用者权限已经改变，请重新操作。")

    @staticmethod
    def managed(persona):
        return {
            "system_prompt": persona.get("system_prompt"),
            "begin_dialogs": persona.get("begin_dialogs") or [],
        }

    async def state(self, card):
        current = (
            await self.studio.current(card["persona_id"])
            if card["persona_id"]
            else None
        )
        status = card["sync_status"]
        same_record = current is not None and record_ref(current) == card["persona_ref"]
        if card["persona_id"]:
            if current is None and card["persona_ref"] is not None:
                status = "missing"
            elif current is not None and not same_record:
                status = "conflict"
            elif (
                current is not None
                and card["baseline"]
                and self.managed(current) != card["baseline"]["managed"]
            ):
                status = "conflict"
        else:
            status = "unbound"
        core_locked = False
        if current:
            record = self.studio.storage.find(
                current["persona_id"], record_ref(current)
            )
            core_locked = bool(record and record["locked"])
        compiled = compile_card(card["document"])
        return {
            "status": status,
            "error": card["sync_error"],
            "current": current,
            "desired": {
                key: compiled[key] for key in ("system_prompt", "begin_dialogs")
            },
            "same_record": same_record,
            "core_locked": core_locked,
            "expected_core_token": digest(current),
        }

    async def detail(self, card):
        return {
            **{key: value for key, value in card.items() if key != "baseline"},
            "sync": await self.state(card),
            "compiled": compile_card(card["document"]),
            "snapshots": self.storage.snapshots(card["card_id"]),
        }

    def options(self, patch, current=None):
        if not isinstance(patch, dict):
            raise StudioError("invalid_payload", "options 必须是对象。")
        check_payload(patch, set(DEFAULT_OPTIONS))
        options = {**(current or DEFAULT_OPTIONS), **patch}
        if type(options["greeting_index"]) is not int or options["greeting_index"] < 0:
            raise StudioError("invalid_payload", "greeting_index 必须为非负整数。")
        validate_content(
            {key: options[key] for key in CONTENT_FIELDS if key in options},
            self.studio.settings(),
        )
        self.studio.validate_capabilities(
            {
                key: options[key]
                for key in ("tools", "skills")
                if current is None or options[key] != current[key]
            }
        )
        return options

    def validate(self, document, options):
        compiled = compile_card(document)
        content = {
            "system_prompt": compiled["system_prompt"],
            **{key: options[key] for key in CONTENT_FIELDS if key in options},
        }
        # Existing personas can have empty prompts or an odd number of dialogs.
        # Their original values remain valid until the corresponding card text changes.
        allow_empty = (
            compiled["literal_system_prompt"] and not content["system_prompt"].strip()
        )
        if allow_empty:
            del content["system_prompt"]
        validate_content(content, self.studio.settings(), create=not allow_empty)
        if (
            sum(map(len, compiled["begin_dialogs"] or []))
            + sum(len(m["content"]) for block in compiled["examples"] for m in block)
            > self.studio.settings().max_dialog_chars
        ):
            raise StudioError("limit_exceeded", "示例对话超过预设对话字符上限。")

    async def discover_personas(self, actor, event=None):
        """Convert visible, previously unmanaged core records without writing to core."""
        personas = await self.studio.manager.get_all_personas()
        self.reauthorize(actor, event)
        bound, seen = self.storage.known_personas()
        errors = []
        for item in personas:
            persona = item.model_dump(mode="json")
            reference = record_ref(persona)
            record = self.studio.storage.find(persona["persona_id"], reference)
            owner = record["owner"] if record else None
            if not actor.admin and owner != actor.owner:
                continue
            if (
                persona["persona_id"] in bound
                or (persona["persona_id"], reference) in seen
            ):
                continue
            try:
                document = from_persona(persona)
            except StudioError as exc:
                errors.append(
                    {"persona_id": persona["persona_id"], "message": str(exc)}
                )
                continue
            options = {
                **DEFAULT_OPTIONS,
                **{
                    key: copy.deepcopy(persona.get(key))
                    for key in ("tools", "skills", "custom_error_message")
                },
            }
            card = self.storage.adopt(persona, document, options, owner)
            self.studio.storage.audit(
                actor, "card.convert_persona", card["card_id"], "success"
            )
        return errors

    async def prepare_import(self, actor, raw):
        document, image = import_card(raw)
        self.validate(document, DEFAULT_OPTIONS)
        async with self.studio.lock:
            import_id = self.storage.prepare_import(actor, document, image)
        avatar_warning = None
        try:
            icon = main_icon(document)
            avatar_source = icon["uri"].split(":", 1)[0] if icon else None
        except StudioError as exc:
            avatar_source, avatar_warning = None, str(exc)
        return {
            "import_id": import_id,
            "card": document,
            "compiled": compile_card(document),
            "has_image": image is not None,
            "avatar_source": avatar_source,
            "avatar_warning": avatar_warning,
        }

    async def prepare_image(self, actor, raw, kind):
        if kind not in ("card_image", "avatar"):
            raise StudioError("invalid_payload", "未知图片类型。")
        image = await asyncio.to_thread(prepare_image, raw, avatar=kind == "avatar")
        async with self.studio.lock:
            asset_id = await asyncio.to_thread(self.storage.put_asset, image)
            upload_id = self.storage.prepare_image(actor, asset_id, kind)
        return {
            "upload_id": upload_id,
            "kind": kind,
            "data_url": CardImage(image, "image/png").data_url,
        }

    async def apply_images(self, actor, document, asset_id, patch):
        if not isinstance(patch, dict) or not patch:
            raise StudioError("invalid_payload", "images 必须为非空对象。")
        check_payload(patch, {"card_image", "avatar"})
        if "avatar" in patch:
            if patch["avatar"] == "ccdefault":
                uri = "ccdefault:"
            else:
                avatar_id = self.storage.get_image(actor, patch["avatar"], "avatar")
                raw = await self.images.original(avatar_id)
                uri = CardImage(raw, "image/png").data_url
            set_main_icon(document, uri)
            document = normalize_card(document)
        if "card_image" in patch:
            image_id = self.storage.get_image(actor, patch["card_image"], "card_image")
            image = await self.images.original(image_id)
            original = await self.images.original(asset_id)
            # Preserve resources referred to by existing __asset: declarations.
            container = await asyncio.to_thread(
                export_png, document, image, resource_image=original
            )
            asset_id = await asyncio.to_thread(self.storage.put_asset, container)
        return document, asset_id

    async def execute(self, actor, action, payload, *, event=None):
        async with self.studio.lock:
            self.reauthorize(actor, event)
            if not isinstance(action, str) or action not in CARD_ACTIONS:
                raise StudioError("invalid_action", "未知角色卡操作。")
            check_payload(payload, *CARD_ACTIONS[action])
            if action in ("lock", "delete") and event is not None:
                raise StudioError(
                    "manual_only",
                    "锁定、解锁和永久删除角色卡需要真实用户在插件页面操作。",
                )
            if action == "preview":
                document = normalize_card(payload["card"])
                origin = persona_origin(document)
                original_options = (
                    {
                        **DEFAULT_OPTIONS,
                        **{
                            key: origin["persona"].get(key)
                            for key in ("tools", "skills", "custom_error_message")
                        },
                    }
                    if origin
                    else None
                )
                options = self.options(payload.get("options", {}), original_options)
                self.validate(document, options)
                return {"document": document, "compiled": compile_card(document)}
            if action == "list":
                if type(payload.get("deleted", False)) is not bool:
                    raise StudioError("invalid_payload", "deleted 必须为布尔值。")
                offset, limit = self.studio.pagination(payload)
                conversion_errors = (
                    await self.discover_personas(actor, event)
                    if not payload.get("deleted", False)
                    else []
                )
                items = self.storage.list(actor, deleted=payload.get("deleted", False))
                details = []
                for c in items[offset : offset + limit]:
                    state = await self.state(c)
                    details.append(
                        {
                            **{
                                key: c[key]
                                for key in (
                                    "card_id",
                                    "revision",
                                    "locked",
                                    "deleted",
                                    "owner",
                                    "persona_id",
                                    "created_at",
                                    "updated_at",
                                    "asset_id",
                                )
                            },
                            "name": c["document"]["data"]["name"],
                            "description": c["document"]["data"]["description"][:240],
                            "tags": c["document"]["data"]["tags"],
                            "character_version": c["document"]["data"][
                                "character_version"
                            ],
                            "spec": c["document"]["spec"],
                            "sync_status": state["status"],
                            "persona_import": persona_origin(c["document"]) is not None,
                        }
                    )
                self.reauthorize(actor, event)
                return {
                    "items": details,
                    "total": len(items),
                    "offset": offset,
                    "limit": limit,
                    "conversion_errors": conversion_errors,
                }
            if action == "create":
                return await self.create(actor, payload, event)
            if not isinstance(payload["card_id"], str):
                raise StudioError("invalid_payload", "card_id 必须是字符串。")
            if "snapshot_id" in payload and not isinstance(payload["snapshot_id"], str):
                raise StudioError("invalid_payload", "snapshot_id 必须是字符串。")
            card = self.storage.get(payload["card_id"])
            self.authorize(
                actor,
                card,
                write=action not in ("get", "snapshots", "snapshot", "lock"),
                allow_deleted=action
                in (
                    "get",
                    "snapshots",
                    "snapshot",
                    "restore",
                    "unarchive",
                    "delete",
                    "lock",
                ),
            )
            if action in ("get", "snapshots", "snapshot"):
                if action == "snapshots":
                    return {"items": self.storage.snapshots(card["card_id"])}
                if action == "snapshot":
                    return self.storage.snapshot(
                        card["card_id"], payload["snapshot_id"]
                    )
                result = await self.detail(card)
                self.reauthorize(actor, event)
                return result
            if (
                type(payload["expected_revision"]) is not int
                or payload["expected_revision"] != card["revision"]
            ):
                raise StudioError(
                    "revision_conflict", "角色卡已被修改，请刷新后再操作。"
                )
            if action == "lock":
                if type(payload["locked"]) is not bool:
                    raise StudioError("invalid_payload", "locked 必须为布尔值。")
                self.storage.set_locked(card["card_id"], payload["locked"])
            elif action == "update":
                sources = set(payload) & {"card", "data", "import_id"}
                if len(sources) > 1:
                    raise StudioError(
                        "invalid_payload", "card、data、import_id 每次只能指定一个。"
                    )
                if not sources and not (set(payload) & {"options", "images"}):
                    raise StudioError(
                        "invalid_payload", "至少提供角色卡内容、options 或 images。"
                    )
                document = copy.deepcopy(card["document"])
                if "card" in payload:
                    document = normalize_card(payload["card"])
                elif "data" in payload:
                    if not isinstance(payload["data"], dict):
                        raise StudioError("invalid_payload", "data 必须为对象。")
                    document["data"].update(payload["data"])
                    document = normalize_card(document)
                elif "import_id" in payload:
                    imported = self.storage.get_import(actor, payload["import_id"])
                    document = imported["document"]
                    card["asset_id"] = imported["asset_id"]
                original_asset_id = card["asset_id"]
                if "images" in payload:
                    document, card["asset_id"] = await self.apply_images(
                        actor, document, card["asset_id"], payload["images"]
                    )
                options = self.options(payload.get("options", {}), card["options"])
                self.validate(document, options)
                changed_version = (
                    document["data"]["character_version"]
                    != card["document"]["data"]["character_version"]
                )
                if (
                    "import_id" not in payload
                    and (
                        document != card["document"]
                        or card["asset_id"] != original_asset_id
                    )
                    and document["spec"] == "chara_card_v3"
                    and document["data"].get("modification_date")
                    == card["document"]["data"].get("modification_date")
                ):
                    document["data"]["modification_date"] = int(time.time())
                await self.ensure_core_writable(actor, card, event)
                card.update(document=document, options=options)
                card = self.storage.save(
                    card, payload["expected_revision"], snapshot=changed_version
                )
                await self.sync(actor, card, event=event)
            elif action in ("sync", "bind"):
                if action == "bind" and card["persona_id"]:
                    raise StudioError("already_bound", "角色卡已经绑定人格。")
                if action == "bind":
                    persona_id = (
                        payload["persona_id"]
                        if "persona_id" in payload
                        else f"{card['document']['data']['name'][:230]} · {card['card_id'][:6]}"
                    )
                    validate_id(persona_id, create=True)
                    if await self.studio.current(persona_id) is not None:
                        raise StudioError(
                            "already_exists",
                            "该人格 ID 已存在；绑定只创建新人格，不覆盖已有记录。",
                        )
                    self.reauthorize(actor, event)
                    self.storage.sync_state(
                        card["card_id"], "pending", persona_id=persona_id
                    )
                    card = self.storage.save(
                        self.storage.get(card["card_id"]), card["revision"]
                    )
                if "overwrite" in payload and type(payload["overwrite"]) is not bool:
                    raise StudioError("invalid_payload", "overwrite 必须为布尔值。")
                await self.sync(
                    actor,
                    card,
                    overwrite=payload.get("overwrite", False),
                    expected_core_token=payload.get("expected_core_token"),
                    event=event,
                )
            elif action == "unbind":
                await self.ensure_core_writable(actor, card, event)
                self.storage.sync_state(card["card_id"], "unbound", unbind=True)
                self.storage.save(self.storage.get(card["card_id"]), card["revision"])
            elif action == "archive":
                await self.ensure_core_writable(actor, card, event)
                card["deleted"] = 1
                self.storage.save(card, card["revision"])
            elif action == "unarchive":
                if not card["deleted"]:
                    raise StudioError("not_archived", "角色卡尚未归档。")
                self.check_card_quota(actor)
                await self.ensure_core_writable(actor, card, event)
                card["deleted"] = 0
                card = self.storage.save(card, card["revision"])
                await self.sync(actor, card, event=event)
            elif action == "delete":
                if not card["deleted"]:
                    raise StudioError("not_archived", "请先归档角色卡，再永久删除。")
                if payload["confirm_name"] != card["document"]["data"]["name"]:
                    raise StudioError(
                        "confirmation_mismatch", "请输入完整角色名称以确认永久删除。"
                    )
                await self.ensure_core_writable(actor, card, event)
                # Image resolution uses this lock before writing the shared cache.
                async with self.images.lock:
                    self.storage.delete(actor, card, card["revision"])
                    pending = self.cleanup_assets_locked()
                return {
                    "card_id": card["card_id"],
                    "deleted": True,
                    "cleanup_pending": pending,
                }
            elif action == "restore":
                if card["deleted"]:
                    self.check_card_quota(actor)
                saved = self.storage.snapshot(card["card_id"], payload["snapshot_id"])[
                    "snapshot"
                ]
                saved["document"] = normalize_card(saved["document"])
                self.validate(
                    saved["document"], self.options(saved["options"], card["options"])
                )
                await self.ensure_core_writable(actor, card, event)
                card.update(copy.deepcopy(saved), deleted=0)
                card = self.storage.save(card, card["revision"], snapshot=False)
                await self.sync(actor, card, event=event)
            self.studio.storage.audit(
                actor, "card." + action, card["card_id"], "success"
            )
            result = await self.detail(self.storage.get(card["card_id"]))
            self.reauthorize(actor, event)
            return result

    def check_card_quota(self, actor):
        settings = self.studio.settings()
        all_cards = self.storage.list(Actor("", "", True))
        if len(all_cards) >= settings.max_personas_total or (
            not actor.admin
            and sum(c["owner"] == actor.owner for c in all_cards)
            >= settings.max_personas_per_user
        ):
            raise StudioError("limit_exceeded", "角色卡数量已达到配置上限。")

    def cleanup_assets_locked(self):
        try:
            return self.storage.cleanup_assets()
        except (OSError, StudioError, ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "Character card image cleanup deferred (%s).", type(exc).__name__
            )
            return self.storage.connection.execute(
                "SELECT COUNT(*) FROM card_asset_garbage"
            ).fetchone()[0]

    async def cleanup_assets(self):
        """Called with the studio lock held, including after a restart."""
        async with self.images.lock:
            return self.cleanup_assets_locked()

    async def create(self, actor, payload, event):
        if ("card" in payload) == ("import_id" in payload):
            raise StudioError(
                "invalid_payload", "新建时必须提供 card 或 import_id，且只能提供一个。"
            )
        if type(payload.get("bind", True)) is not bool:
            raise StudioError("invalid_payload", "bind 必须为布尔值。")
        imported = (
            self.storage.get_import(actor, payload["import_id"])
            if "import_id" in payload
            else None
        )
        if imported and imported["card_id"]:
            card = self.storage.get(imported["card_id"])
            self.authorize(actor, card)
            result = await self.detail(card)
            self.reauthorize(actor, event)
            return result
        document = normalize_card(imported["document"] if imported else payload["card"])
        options = self.options(payload.get("options", {}))
        self.validate(document, options)
        settings = self.studio.settings()
        self.check_card_quota(actor)
        if payload.get("bind", True):
            await self.studio.check_quota(actor, settings)
            self.reauthorize(actor, event)
        card = self.storage.create(
            actor,
            document,
            options=options,
            asset_id=imported["asset_id"] if imported else None,
            imported=payload.get("import_id"),
        )
        if payload.get("bind", True):
            persona_id = f"{document['data']['name'][:230]} · {card['card_id'][:6]}"
            self.storage.sync_state(card["card_id"], "pending", persona_id=persona_id)
            await self.sync(actor, self.storage.get(card["card_id"]), event=event)
        self.studio.storage.audit(actor, "card.create", card["card_id"], "success")
        result = await self.detail(self.storage.get(card["card_id"]))
        self.reauthorize(actor, event)
        return result

    async def ensure_core_writable(self, actor, card, event):
        if card["persona_id"]:
            current = await self.studio.current(card["persona_id"])
            self.reauthorize(actor, event)
            if current is not None and record_ref(current) == card["persona_ref"]:
                self.studio.authorized(actor, current, write=True)

    async def sync(
        self, actor, card, *, overwrite=False, expected_core_token=None, event=None
    ):
        if not card["persona_id"]:
            return
        state = await self.state(card)
        self.reauthorize(actor, event)
        self.authorize(actor, card, write=True)
        current = state["current"]
        if state["core_locked"]:
            raise StudioError("persona_locked", "绑定人格已被人工锁定，请先解锁。")
        if current is not None:
            if not state["same_record"]:
                self.storage.sync_state(
                    card["card_id"],
                    "conflict",
                    error="同名人格不再是原绑定记录；请解除绑定后创建新人格。",
                )
                return
            self.studio.authorized(actor, current, write=True)
            if (
                state["status"] == "conflict"
                and self.managed(current) != state["desired"]
            ):
                if not overwrite:
                    self.storage.sync_state(
                        card["card_id"],
                        "conflict",
                        error="人格包含外部修改，请查看差异后选择覆盖或解除绑定。",
                    )
                    return
                if expected_core_token != state["expected_core_token"]:
                    raise StudioError(
                        "revision_conflict", "人格内容再次改变，请重新查看差异。"
                    )
        elif card["persona_ref"] is not None:
            self.storage.sync_state(
                card["card_id"],
                "missing",
                error="绑定人格已被删除，请解除绑定后创建新人格。",
            )
            return
        compiled = compile_card(card["document"])
        options = card["options"]
        payload = {key: compiled[key] for key in ("system_prompt", "begin_dialogs")}
        last_options = (card["baseline"] or {}).get("options", {})
        payload.update(
            {
                key: options[key]
                for key in ("tools", "skills", "custom_error_message")
                if current is None or options[key] != last_options.get(key, object())
            }
        )
        self.storage.sync_state(card["card_id"], "pending")
        try:
            if current is None:
                await self.studio.check_quota(actor, self.studio.settings())
                self.reauthorize(actor, event)
                result = await self.studio.manager.create_persona(
                    card["persona_id"], **payload
                )
                current = result.model_dump(mode="json")
                self.storage.sync_state(
                    card["card_id"], "pending", persona_ref=record_ref(current)
                )
                self.studio.storage.register(
                    card["persona_id"], record_ref(current), card["owner"]
                )
            else:
                await self.studio.manager.update_persona(card["persona_id"], **payload)
                current = await self.studio.current(card["persona_id"])
            self.reauthorize(actor, event)
            if current is None or self.managed(current) != state["desired"]:
                raise StudioError(
                    "sync_failed", "核心写入后的内容与预期不同，请刷新检查。"
                )
            self.storage.sync_state(
                card["card_id"],
                "synced",
                baseline={
                    "managed": self.managed(current),
                    "options": copy.deepcopy(options),
                },
            )
        except StudioError as exc:
            self.storage.sync_state(card["card_id"], "failed", error=str(exc))
        except Exception:
            self.storage.sync_state(
                card["card_id"],
                "failed",
                error="卡片已保存，但人格同步失败；请刷新检查后手动重试。",
            )

    async def resolve_image(self, actor, card_id):
        async with self.studio.lock:
            card = self.storage.get(card_id)
            self.authorize(actor, card, allow_deleted=True)
        # Image IO must not hold the studio lock or prevent textual card edits.
        warning = None
        try:
            image = await self.images.resolve(card["document"], card["asset_id"])
        except StudioError as exc:
            image, warning = None, str(exc)
        async with self.studio.lock:
            current = self.storage.get(card_id)
            self.authorize(actor, current, allow_deleted=True)
            if current["revision"] != card["revision"]:
                raise StudioError(
                    "revision_conflict", "读取图片期间角色卡已改变，请刷新后重试。"
                )
        return card, image, warning

    async def avatar(self, actor, card_id):
        card, image, warning = await self.resolve_image(actor, card_id)
        return {
            "data_url": image.data_url if image else None,
            "warning": warning,
            "revision": card["revision"],
        }

    async def card_image(self, actor, card_id):
        async with self.studio.lock:
            card = self.storage.get(card_id)
            self.authorize(actor, card, allow_deleted=True)
        warning, image = None, None
        try:
            raw = await self.images.original(card["asset_id"])
            if raw:
                media_type, _ = await asyncio.to_thread(image_info, raw)
                image = CardImage(raw, media_type)
        except StudioError as exc:
            warning = str(exc)
        async with self.studio.lock:
            current = self.storage.get(card_id)
            self.authorize(actor, current, allow_deleted=True)
            if current["revision"] != card["revision"]:
                raise StudioError(
                    "revision_conflict", "读取图片期间角色卡已改变，请刷新。"
                )
        return {
            "data_url": image.data_url if image else None,
            "warning": warning,
            "revision": card["revision"],
        }

    async def export(self, actor, card_id, *, png=False):
        if png:
            async with self.studio.lock:
                card = self.storage.get(card_id)
                self.authorize(actor, card, allow_deleted=True)
            original = await self.images.original(card["asset_id"])
            if original:
                png_image = original
            else:
                image = await self.images.resolve(card["document"], card["asset_id"])
                png_image = await asyncio.to_thread(image_png, image) if image else None
            content = await asyncio.to_thread(
                export_png, card["document"], png_image, resource_image=original
            )
            # Export work can also yield; recheck access before returning bytes.
            async with self.studio.lock:
                current = self.storage.get(card_id)
                self.authorize(actor, current, allow_deleted=True)
                if current["revision"] != card["revision"]:
                    raise StudioError(
                        "revision_conflict", "导出期间角色卡已改变，请重试。"
                    )
            return content, "image/png", card["document"]["data"]["name"]
        async with self.studio.lock:
            card = self.storage.get(card_id)
            self.authorize(actor, card, allow_deleted=True)
            document = normalize_card(card["document"])
        from .domain import canonical

        return (
            canonical(document).encode("utf-8"),
            "application/json",
            document["data"]["name"],
        )

    async def apply_request(self, event, req):
        """Apply a request-local card view; never mutate stored conversation history."""
        if getattr(req, "_persona_studio_applied", False):
            return
        persona_id = event.get_extra("_trace_persona_id")
        if not persona_id:
            return
        async with self.studio.lock:
            card = self.storage.by_persona(persona_id)
            if not card or card["sync_status"] != "synced":
                return
            if (await self.state(card))["status"] != "synced":
                return
            compiled = compile_card(card["document"])
            # A request already in flight can carry an older core persona.
            if compiled["system_prompt"] not in (req.system_prompt or ""):
                return
            data = card["document"]["data"]
            name = event.get_sender_name() or event.get_sender_id() or "用户"

            def render(text):
                return render_macros(text, data["name"], name)

            prompt = compiled["system_prompt"]
            if compiled["literal_system_prompt"]:
                original = persona_origin(card["document"])["persona"]["system_prompt"]
                rendered_prompt = original + render(prompt[len(original) :])
            else:
                rendered_prompt = render(prompt)
            if prompt:
                req.system_prompt = req.system_prompt.replace(
                    prompt, rendered_prompt, 1
                )
            examples = []
            for block in compiled["examples"]:
                examples.extend(
                    {
                        "role": m["role"],
                        "content": render(m["content"]),
                        "_no_save": True,
                    }
                    for m in block
                )
            history = [m for m in req.contexts if not m.get("_no_save")]
            if not history:
                greetings = [data["first_mes"], *data["alternate_greetings"]]
                if not event.is_private_chat() and data.get("group_only_greetings"):
                    greetings = data["group_only_greetings"]
                index = card["options"]["greeting_index"]
                if index < len(greetings) and greetings[index]:
                    examples.append(
                        {
                            "role": "assistant",
                            "content": render(greetings[index]),
                            "_no_save": True,
                        }
                    )
            req.contexts = examples + copy.deepcopy(req.contexts)
            if data["post_history_instructions"].strip():
                # Appended to the current input, after history, only for this request.
                from astrbot.core.agent.message import TextPart

                req.extra_user_content_parts.append(
                    TextPart(
                        text="[角色补充指令]\n"
                        + render(data["post_history_instructions"])
                    ).mark_as_temp()
                )
            req._persona_studio_applied = True
