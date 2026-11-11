from __future__ import annotations

import asyncio
import time
from collections import Counter

from astrbot.api import sp
from astrbot.core.db.po import Persona
from astrbot.core.skills.skill_manager import SkillManager

from .domain import (
    ACTION_PAYLOADS,
    CONTENT_FIELDS,
    MANAGEMENT_TOOLS,
    Settings,
    StudioError,
    check_payload,
    digest,
    identify,
    record_ref,
    validate_content,
    validate_id,
    view_options,
)


class PersonaStudio:
    def __init__(self, context, config, storage):
        self.context = context
        self.config = config
        self.storage = storage
        self.manager = context.persona_manager
        self.lock = asyncio.Lock()
        self.cards = None

    def settings(self) -> Settings:
        return Settings.parse(self.config)

    async def current(self, persona_id: str) -> dict | None:
        personas = await self.manager.get_all_personas()
        persona = next(
            (item for item in personas if item.persona_id == persona_id), None
        )
        return persona.model_dump(mode="json") if persona is not None else None

    def authorized(self, actor, persona: dict, *, write: bool = False) -> dict | None:
        record = self.storage.find(persona["persona_id"], record_ref(persona))
        if not actor.admin and (record is None or record["owner"] != actor.owner):
            raise StudioError("not_found_or_forbidden", "人格不存在或无权访问。")
        if write and record is not None and record["locked"]:
            raise StudioError(
                "persona_locked",
                "人格已锁定，请真实用户先发送 /ps unlock <人格ID>。",
            )
        return record

    def ensure_record(self, persona: dict, record: dict | None) -> dict:
        return (
            record
            if record is not None
            else self.storage.register(persona["persona_id"], record_ref(persona), None)
        )

    def state(self, record: dict | None, persona: dict | None) -> dict:
        conflict = bool(
            persona is not None
            and record is not None
            and record_ref(persona) != record["persona_ref"]
        )
        current_record = (
            self.storage.find(persona["persona_id"], record_ref(persona))
            if persona
            else None
        )
        return {
            "exists": persona is not None and not conflict,
            "record_conflict": conflict,
            "revision": record["revision"] if record is not None else 1,
            "expected_revision": digest(
                {
                    "chain_id": record["chain_id"] if record is not None else None,
                    "revision": record["revision"] if record is not None else 1,
                    "current": persona,
                }
            ),
            "locked": bool(
                (record and record["locked"])
                or (current_record and current_record["locked"])
            ),
            "chain_id": record["chain_id"] if record is not None else None,
        }

    def prune(self, record: dict, settings: Settings) -> None:
        removed = self.storage.prune(
            record, settings.backup_keep_count, settings.backup_keep_days
        )
        if removed:
            self.storage.audit(
                None,
                "versions.prune",
                record["persona_id"],
                "success",
                changed_fields=removed,
            )

    async def maintenance(self):
        async with self.lock:
            settings = self.settings()
            personas = {
                item.persona_id: item.model_dump(mode="json")
                for item in await self.manager.get_all_personas()
            }
            for record in self.storage.records():
                if not self.state(record, personas.get(record["persona_id"]))["locked"]:
                    self.prune(record, settings)

    def validate_capabilities(self, data: dict):
        if data.get("tools"):
            manager = self.context.get_llm_tool_manager()
            missing = [name for name in data["tools"] if manager.get_func(name) is None]
            if missing:
                raise StudioError(
                    "invalid_capability", f"工具未注册：{', '.join(missing)}"
                )
        if data.get("skills"):
            manager = SkillManager()
            known = {
                item.name
                for runtime in ("local", "sandbox")
                for item in manager.list_skills(runtime=runtime)
            }
            missing = set(data["skills"]) - known
            if missing:
                raise StudioError(
                    "invalid_capability", f"技能未注册：{', '.join(sorted(missing))}"
                )

    async def check_quota(self, actor, settings: Settings):
        personas = await self.manager.get_all_personas()
        if len(personas) >= settings.max_personas_total:
            raise StudioError(
                "limit_exceeded", f"全局人格数量已达到 {settings.max_personas_total}。"
            )
        if not actor.admin:
            owned = 0
            for persona in personas:
                data = persona.model_dump(mode="json")
                record = self.storage.find(data["persona_id"], record_ref(data))
                owned += bool(record and record["owner"] == actor.owner)
            if owned >= settings.max_personas_per_user:
                raise StudioError(
                    "limit_exceeded",
                    f"个人新建人格数量已达到 {settings.max_personas_per_user}。",
                )

    async def references(self, persona_id: str) -> list[dict]:
        references = []

        def scan(value, location, kind):
            if isinstance(value, dict):
                for key, child in value.items():
                    path = f"{location}.{key}"
                    reference = child
                    if kind == "subagent" and key == "persona_id" and child is not None:
                        reference = str(child).strip() or None
                    if (
                        key in {"persona_id", "default_personality"}
                        and reference == persona_id
                    ):
                        references.append({"type": kind, "location": path})
                    elif isinstance(child, dict | list):
                        scan(
                            child,
                            path,
                            "subagent" if key == "subagent_orchestrator" else kind,
                        )
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    scan(child, f"{location}[{index}]", kind)

        try:
            for config_id, config in self.context.astrbot_config_mgr.confs.items():
                scan(config, config_id, "config")
            conversations = await self.context.conversation_manager.get_conversations()
            for conversation in conversations:
                if conversation.persona_id == persona_id:
                    references.append(
                        {"type": "conversation", "location": conversation.cid}
                    )
            preferences = await self.manager.db.get_preferences(
                scope="umo", key="session_service_config"
            )
            for preference in preferences:
                scan(preference.value, preference.scope_id, "session_rule")
            for job in await self.manager.db.list_cron_jobs():
                scan(job.payload, job.job_id, "cron_job")
        except Exception as exc:
            raise StudioError(
                "reference_scan_failed",
                "无法完成默认配置、会话、子代理或任务的引用检查，已拒绝删除。",
            ) from exc
        return references

    @staticmethod
    def pagination(payload: dict) -> tuple[int, int]:
        offset, limit = payload.get("offset", 0), payload.get("limit", 20)
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise StudioError(
                "invalid_payload", "offset 必须为非负整数，limit 必须为 1～100。"
            )
        return offset, limit

    async def execute(self, event, family: str, action: str, payload: dict) -> dict:
        actor = None
        target = None
        progress = {
            "core_written": False,
            "plugin_written": False,
            "mutation_started": False,
        }
        try:
            settings = self.settings()
            actor = identify(event, settings)
            if family not in {
                "manage",
                "versions",
                "protect",
                "whitelist",
            } or not isinstance(action, str):
                raise StudioError("invalid_action", "无效操作。")
            if not isinstance(payload, dict):
                raise StudioError("invalid_payload", "payload 必须是 JSON 对象。")
            target = payload.get(
                "persona_id", payload.get("backup_id", payload.get("user_id"))
            )
            async with self.lock:
                settings = self.settings()
                actor = identify(event, settings)
                if family == "manage":
                    result = await self.manage(
                        event, actor, action, payload, settings, progress
                    )
                elif family == "versions":
                    result = await self.versions(
                        event, actor, action, payload, settings, progress
                    )
                elif family == "protect":
                    result = await self.protect(
                        event, actor, action, payload, settings, progress
                    )
                else:
                    result = await self.whitelist(
                        event, actor, action, payload, settings, progress
                    )
                self.storage.audit(
                    actor,
                    f"{family}.{action}",
                    target,
                    "success",
                    changed_fields=({"whitelist"} if result["changed"] else set())
                    if family == "whitelist"
                    else set(payload) & CONTENT_FIELDS,
                )
                return {"ok": True, "data": result}
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, StudioError)
                else StudioError(
                    "operation_failed",
                    "操作失败；若已开始写入，可能已产生部分效果，请勿自动重试。",
                )
            )
            changed = progress["core_written"] or progress["plugin_written"]
            details = {**error.details, **progress}
            if changed:
                details["cause"] = error.code
            try:
                self.storage.audit(
                    actor,
                    f"{family}.{action}",
                    target if isinstance(target, str) else None,
                    "partial_failure" if changed else "denied",
                    reason=error.code,
                )
            except Exception:
                details["audit_failed"] = True
            return {
                "ok": False,
                "error": {
                    "code": "partial_failure" if changed else error.code,
                    "message": "核心或插件数据已改变，但操作未全部完成，请勿自动重试。"
                    if changed
                    else str(error),
                    "details": details,
                },
                "retry_safe": False,
            }

    async def manage(
        self,
        event,
        actor,
        action: str,
        payload: dict,
        settings: Settings,
        progress: dict,
    ) -> dict:
        if action not in ACTION_PAYLOADS["manage"]:
            raise StudioError(
                "invalid_action",
                "人格操作仅支持 list/get/create/update/delete/switch。",
            )
        if action == "get":
            view_options(payload)
        check_payload(payload, *ACTION_PAYLOADS["manage"][action])
        if action == "list":
            offset, limit = self.pagination(payload)
            visible = []
            personas = await self.manager.get_all_personas()
            actor = identify(event, self.settings())
            for item in personas:
                persona = item.model_dump(mode="json")
                record = self.storage.find(persona["persona_id"], record_ref(persona))
                if actor.admin or (record and record["owner"] == actor.owner):
                    visible.append(
                        {
                            "persona_id": persona["persona_id"],
                            "owner": record["owner"] if record else None,
                            **self.state(record, persona),
                        }
                    )
            visible.sort(key=lambda item: item["persona_id"])
            return {
                "items": visible[offset : offset + limit],
                "total": len(visible),
                "offset": offset,
                "limit": limit,
            }
        if action == "create":
            persona_id = validate_id(payload["persona_id"], create=True)
            validate_content(payload, settings, create=True)
            self.validate_capabilities(payload)
            if await self.current(persona_id) is not None:
                raise StudioError(
                    "already_exists", "同名人格已存在，不会覆盖或自动改名。"
                )
            await self.check_quota(actor, settings)
            actor = identify(event, self.settings())
            self.storage.audit(
                actor,
                "manage.create",
                persona_id,
                "pending",
                changed_fields=set(payload) & CONTENT_FIELDS,
            )
            progress["mutation_started"] = True
            persona = await self.manager.create_persona(**payload)
            progress["core_written"] = True
            data = persona.model_dump(mode="json")
            record = self.storage.register(persona_id, record_ref(data), actor.owner)
            return {"persona": data, "owner": actor.owner, **self.state(record, data)}
        persona_id = validate_id(payload["persona_id"])
        persona = await self.current(persona_id)
        if persona is None:
            raise StudioError("not_found_or_forbidden", "人格不存在或无权访问。")
        actor = identify(event, self.settings())
        record = self.authorized(actor, persona, write=action in {"update", "delete"})
        if action == "get":
            backups = self.storage.backups(record["chain_id"]) if record else []
            return {
                "persona": persona,
                "owner": record["owner"] if record else None,
                **self.state(record, persona),
                "backup_summary": {
                    "count": len(backups),
                    "latest": backups[0] if backups else None,
                },
            }
        if action == "switch":
            forced = await sp.get_async(
                scope="umo",
                scope_id=event.unified_msg_origin,
                key="session_service_config",
                default={},
            )
            if forced and forced.get("persona_id"):
                raise StudioError(
                    "forced_persona", "当前会话规则强制指定了人格，不能通过本工具切换。"
                )
            manager = self.context.conversation_manager
            conversation_id = await manager.get_curr_conversation_id(
                event.unified_msg_origin
            )
            conversation = (
                await manager.get_conversation(
                    event.unified_msg_origin, conversation_id
                )
                if conversation_id
                else None
            )
            if conversation is None:
                raise StudioError(
                    "no_current_conversation",
                    "当前会话没有可更新的对话，不会自动新建或重置。",
                )
            if conversation.user_id != event.unified_msg_origin:
                raise StudioError("conversation_forbidden", "当前对话不属于此会话。")
            if (
                await manager.get_curr_conversation_id(event.unified_msg_origin)
                != conversation_id
            ):
                raise StudioError(
                    "conversation_changed", "当前对话已改变，请重新查询后再切换。"
                )
            actor = identify(event, self.settings())
            self.authorized(actor, persona)
            progress["mutation_started"] = True
            await manager.update_conversation(
                event.unified_msg_origin,
                conversation_id=conversation_id,
                persona_id=persona_id,
            )
            progress["core_written"] = True
            tools = persona.get("tools")
            missing = sorted(MANAGEMENT_TOOLS - set(tools)) if tools is not None else []
            return {
                "persona_id": persona_id,
                "effective": "next_conversation_request",
                "history_preserved": True,
                "warning": f"下一次新建的聊天请求生效，当前工具调用链不变；目标未启用 {', '.join(missing)}，可能无法继续模型管理。"
                if missing
                else "下一次新建的聊天请求生效；当前请求与整个工具调用链的人格和工具集不变。",
            }
        changes = {
            name: value for name, value in payload.items() if name in CONTENT_FIELDS
        }
        if self.cards is not None and self.cards.storage.by_persona(persona_id):
            raise StudioError(
                "card_managed",
                "该人格由角色卡管理，请使用 character_update 或插件页面编辑；删除请先解除绑定。",
            )
        if action == "update":
            if not changes:
                raise StudioError("invalid_payload", "更新至少需要一个内容或能力字段。")
            validate_content(changes, settings)
            self.validate_capabilities(changes)
        else:
            references = await self.references(persona_id)
            if references:
                details = {"counts": dict(Counter(item["type"] for item in references))}
                if actor.admin:
                    details["references"] = references
                raise StudioError(
                    "persona_in_use",
                    "人格仍被使用，请先通过有权限入口解除引用，再重试删除。",
                    details,
                )
        actor = identify(event, self.settings())
        record = self.ensure_record(
            persona, self.authorized(actor, persona, write=True)
        )
        backup_id = self.storage.backup(record, persona, action)
        progress["backup_id"] = backup_id
        self.storage.audit(
            actor, f"manage.{action}", persona_id, "pending", changed_fields=changes
        )
        progress["mutation_started"] = True
        if action == "update":
            await self.manager.update_persona(persona_id, **changes)
        else:
            await self.manager.delete_persona(persona_id)
        progress["core_written"] = True
        record["revision"] += 1
        record["deleted"] = int(action == "delete")
        self.storage.save_record(record)
        self.prune(record, settings)
        current = await self.current(persona_id)
        return {
            "persona_id": persona_id,
            "backup_id": backup_id,
            "persona": current,
            **self.state(record, current),
        }

    async def versions(
        self,
        event,
        actor,
        action: str,
        payload: dict,
        settings: Settings,
        progress: dict,
    ) -> dict:
        if action not in ACTION_PAYLOADS["versions"]:
            raise StudioError(
                "invalid_action", "备份操作仅支持 list/get/restore/delete。"
            )
        if action == "get":
            view_options(payload)
        check_payload(payload, *ACTION_PAYLOADS["versions"][action])
        if action == "list":
            persona_id = validate_id(payload["persona_id"])
            offset, limit = self.pagination(payload)
            current = await self.current(persona_id)
            actor = identify(event, self.settings())
            records = [
                record
                for record in self.storage.records(persona_id)
                if actor.admin or record["owner"] == actor.owner
            ]
            if not actor.admin and not records:
                raise StudioError(
                    "not_found_or_forbidden", "人格或备份不存在或无权访问。"
                )
            items, states = [], []
            for record in records:
                state = self.state(record, current)
                states.append(state)
                for backup in self.storage.backups(record["chain_id"]):
                    items.append({**backup, **state})
            items.sort(
                key=lambda item: (item["created_at"], item["backup_id"]), reverse=True
            )
            return {
                "persona_id": persona_id,
                "items": items[offset : offset + limit],
                "total": len(items),
                "states": states,
                "offset": offset,
                "limit": limit,
            }
        if not isinstance(payload["backup_id"], str) or not payload["backup_id"]:
            raise StudioError("invalid_payload", "backup_id 必须为非空字符串。")
        record, snapshot = self.storage.get_backup(payload["backup_id"], actor)
        if not actor.admin and record["owner"] != actor.owner:
            raise StudioError("not_found_or_forbidden", "备份不存在或无权访问。")
        current = await self.current(record["persona_id"])
        actor = identify(event, self.settings())
        if not actor.admin and record["owner"] != actor.owner:
            raise StudioError("not_found_or_forbidden", "备份不存在或无权访问。")
        state = self.state(record, current)
        current_record = (
            self.storage.find(current["persona_id"], record_ref(current))
            if current
            else None
        )
        locked = record["locked"] or bool(current_record and current_record["locked"])
        if (
            not locked
            and snapshot["created_at"] < time.time() - settings.backup_keep_days * 86400
        ):
            raise StudioError("backup_expired", "备份已超过保留期限，不能使用。")
        if action == "get":
            return {
                "snapshot": snapshot,
                "persona": snapshot["persona"],
                "owner": record["owner"],
                **state,
            }
        if locked:
            raise StudioError("persona_locked", "所属人格已锁定，不能恢复或删除备份。")
        if action == "delete":
            self.storage.remove_backup(payload["backup_id"])
            progress["plugin_written"] = True
            return {
                "backup_id": payload["backup_id"],
                "deleted": True,
                "undoable": False,
                "current_persona_unchanged": True,
            }
        if self.cards is not None and self.cards.storage.by_persona(
            record["persona_id"]
        ):
            raise StudioError(
                "card_managed",
                "该人格由角色卡管理，请使用角色卡快照恢复，或先解除绑定。",
            )
        if state["record_conflict"]:
            raise StudioError(
                "record_conflict", "同名人格已属于其他核心记录，不能用旧备份覆盖。"
            )
        if (
            not isinstance(payload["expected_revision"], str)
            or payload["expected_revision"] != state["expected_revision"]
        ):
            raise StudioError(
                "revision_conflict",
                "当前人格或不存在状态已改变，请重新查询版本后再决定是否恢复。",
            )
        if set(snapshot["persona"]) - set(Persona.model_fields):
            raise StudioError(
                "invalid_snapshot", "快照包含当前核心不支持的人格字段，拒绝兼容恢复。"
            )
        data = {name: snapshot["persona"][name] for name in CONTENT_FIELDS}
        validate_content(data, settings, create=True)
        self.validate_capabilities(data)
        if current is None:
            validate_id(record["persona_id"], create=True)
            await self.check_quota(actor, settings)
        else:
            self.authorized(actor, current, write=True)
            progress["backup_id"] = self.storage.backup(record, current, "restore")
        self.storage.audit(
            actor,
            "versions.restore",
            record["persona_id"],
            "pending",
            changed_fields=CONTENT_FIELDS,
        )
        actor = identify(event, self.settings())
        if not actor.admin and record["owner"] != actor.owner:
            raise StudioError("not_found_or_forbidden", "备份不存在或无权访问。")
        progress["mutation_started"] = True
        if current is None:
            persona = await self.manager.create_persona(record["persona_id"], **data)
            progress["core_written"] = True
            restored = persona.model_dump(mode="json")
            record["persona_ref"] = record_ref(restored)
        else:
            await self.manager.update_persona(record["persona_id"], **data)
            progress["core_written"] = True
            restored = await self.current(record["persona_id"])
        record["revision"] += 1
        record["deleted"] = 0
        self.storage.save_record(record)
        self.prune(record, settings)
        return {
            "persona": restored,
            "restored_from": payload["backup_id"],
            "backup_id": progress.get("backup_id"),
            "owner": record["owner"],
            **self.state(record, restored),
        }

    async def whitelist(
        self,
        event,
        actor,
        action: str,
        payload: dict,
        settings: Settings,
        progress: dict,
    ) -> dict:
        if not actor.admin:
            raise StudioError("admin_only", "仅管理员可以添加或移除白名单用户。")
        if action not in {"add", "remove"}:
            raise StudioError("invalid_action", "白名单命令仅支持 add/remove。")
        check_payload(payload, {"user_id"}, {"user_id"})
        user_id = payload["user_id"]
        if (
            not isinstance(user_id, str)
            or not user_id
            or any(character.isspace() for character in user_id)
        ):
            raise StudioError(
                "invalid_payload", "用户 ID 必须为非空字符串，不能包含空白。"
            )
        original = event.message_obj.message_str.strip()
        prefixes = self.context.get_config(event.unified_msg_origin).get(
            "wake_prefix", ["/"]
        )
        if event.message_obj.raw_message is None or not any(
            original.startswith(prefix)
            and original[len(prefix) :].split() == ["ps", "whitelist", action, user_id]
            for prefix in prefixes
        ):
            raise StudioError(
                "real_command_only",
                "白名单变更只接受管理员直接发送的命令，模型及命令代理不能执行。",
            )
        identity = (actor.platform_id, user_id)
        whitelisted = action == "add"
        changed = (identity in settings.whitelist) != whitelisted
        if changed:
            entries = list(self.config.get("whitelist", []))
            if whitelisted:
                entries.append(
                    {
                        "__template_key": "user",
                        "platform_id": actor.platform_id,
                        "user_id": user_id,
                    }
                )
            else:
                entries = [
                    entry
                    for entry in entries
                    if (entry["platform_id"], entry["user_id"]) != identity
                ]
            previous = dict(self.config)
            progress["mutation_started"] = True
            try:
                self.config.save_config({"whitelist": entries})
            except Exception as exc:
                self.config.clear()
                self.config.update(previous)
                raise StudioError(
                    "config_save_failed",
                    "保存白名单失败，本次修改未生效；请检查配置文件权限，不要自动重试。",
                ) from exc
            progress["plugin_written"] = True
        return {
            "platform_id": actor.platform_id,
            "user_id": user_id,
            "whitelisted": whitelisted,
            "changed": changed,
        }

    async def protect(
        self,
        event,
        actor,
        action: str,
        payload: dict,
        settings: Settings,
        progress: dict,
    ) -> dict:
        if action not in {"lock", "unlock"}:
            raise StudioError("invalid_action", "保护命令仅支持 lock/unlock。")
        check_payload(payload, {"persona_id"}, {"persona_id"})
        persona_id = validate_id(payload["persona_id"])
        original = event.message_obj.message_str.strip()
        prefixes = self.context.get_config(event.unified_msg_origin).get(
            "wake_prefix", ["/"]
        )
        if event.message_obj.raw_message is None or not any(
            original.startswith(prefix)
            and original[len(prefix) :].split(maxsplit=2) == ["ps", action, persona_id]
            for prefix in prefixes
        ):
            raise StudioError(
                "real_command_only",
                "锁定和解锁只接受真实用户直接发送的命令，模型及命令代理不能执行。",
            )
        persona = await self.current(persona_id)
        if persona is None:
            raise StudioError("not_found_or_forbidden", "人格不存在或无权访问。")
        actor = identify(event, self.settings())
        record = self.ensure_record(persona, self.authorized(actor, persona))
        locked = int(action == "lock")
        changed = record["locked"] != locked
        record["locked"] = locked
        self.storage.save_record(record)
        progress["plugin_written"] = changed
        if not locked:
            self.prune(record, settings)
        return {"persona_id": persona_id, "locked": bool(locked), "changed": changed}
