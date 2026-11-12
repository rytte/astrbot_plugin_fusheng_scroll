from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields

from astrbot.api.platform import MessageType


class StudioError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


CONTENT_FIELDS = {
    "system_prompt",
    "begin_dialogs",
    "tools",
    "skills",
    "custom_error_message",
}
RESERVED_IDS = {"default", "[%None]", "_chatui_default_"}
MODEL_TOOLS = {
    "persona_list": ("manage", "list"),
    "persona_get": ("manage", "get"),
    "persona_create": ("manage", "create"),
    "persona_update": ("manage", "update"),
    "persona_delete": ("manage", "delete"),
    "persona_switch": ("manage", "switch"),
    "persona_backup_list": ("versions", "list"),
    "persona_backup_get": ("versions", "get"),
    "persona_backup_restore": ("versions", "restore"),
    "persona_backup_delete": ("versions", "delete"),
}
MANAGEMENT_TOOLS = set(MODEL_TOOLS)
ACTION_PAYLOADS = {
    "manage": {
        "list": ({"offset", "limit"}, set()),
        "get": ({"persona_id", "format", "content"}, {"persona_id"}),
        "create": (CONTENT_FIELDS | {"persona_id"}, {"persona_id", "system_prompt"}),
        "update": (CONTENT_FIELDS | {"persona_id"}, {"persona_id"}),
        "delete": ({"persona_id"}, {"persona_id"}),
        "switch": ({"persona_id"}, {"persona_id"}),
    },
    "versions": {
        "list": ({"persona_id", "offset", "limit"}, {"persona_id"}),
        "get": ({"backup_id", "format", "content"}, {"backup_id"}),
        "restore": (
            {"backup_id", "expected_revision"},
            {"backup_id", "expected_revision"},
        ),
        "delete": ({"backup_id"}, {"backup_id"}),
    },
}


@dataclass(frozen=True)
class Actor:
    platform_id: str
    user_id: str
    admin: bool

    @property
    def owner(self) -> list[str]:
        return [self.platform_id, self.user_id]


@dataclass(frozen=True)
class Settings:
    allow_group_chat: bool = True
    whitelist: tuple[tuple[str, str], ...] = ()
    max_personas_per_user: int = 20
    max_personas_total: int = 200
    max_prompt_chars: int = 50000
    max_dialog_chars: int = 50000
    max_error_chars: int = 5000
    backup_keep_count: int = 50
    backup_keep_days: int = 90
    render_width: int = 1000
    render_page_height: int = 1500
    render_max_pages: int = 20
    render_max_pixels: int = 2000000
    render_timeout: int = 30

    @classmethod
    def parse(cls, config: dict) -> Settings:
        unknown = set(config) - {field.name for field in fields(cls)}
        if unknown:
            raise StudioError(
                "invalid_config", f"未知配置字段：{', '.join(sorted(unknown))}"
            )
        raw = dict(config)
        allow_group_chat = raw.pop("allow_group_chat", True)
        if type(allow_group_chat) is not bool:
            raise StudioError("invalid_config", "allow_group_chat 必须为布尔值。")
        whitelist = raw.pop("whitelist", [])
        if not isinstance(whitelist, list):
            raise StudioError("invalid_config", "whitelist 必须为用户模板列表。")
        identities = []
        for entry in whitelist:
            if not isinstance(entry, dict) or set(entry) - {
                "platform_id",
                "user_id",
                "__template_key",
            }:
                raise StudioError(
                    "invalid_config",
                    "白名单条目仅接受 platform_id、user_id 和模板标识。",
                )
            if "__template_key" in entry and entry["__template_key"] != "user":
                raise StudioError("invalid_config", "白名单模板必须为 user。")
            identity = (entry.get("platform_id"), entry.get("user_id"))
            if any(
                not isinstance(value, str) or not value or value.strip() != value
                for value in identity
            ):
                raise StudioError(
                    "invalid_config",
                    "白名单的平台 ID、用户 ID 必须为非空字符串，不能带首尾空白。",
                )
            if identity in identities:
                raise StudioError("invalid_config", "白名单存在重复用户。")
            identities.append(identity)
        for name, value in raw.items():
            if type(value) is not int or value < 1:
                raise StudioError("invalid_config", f"{name} 必须为正整数。")
        settings = cls(
            allow_group_chat=allow_group_chat, whitelist=tuple(identities), **raw
        )
        if (
            not 480 <= settings.render_width <= 1600
            or not 600 <= settings.render_page_height <= 2400
        ):
            raise StudioError(
                "invalid_config", "渲染宽度需为 480～1600，高度需为 600～2400。"
            )
        if (
            settings.render_width * settings.render_page_height
            > settings.render_max_pixels
        ):
            raise StudioError("invalid_config", "渲染单页像素超过 render_max_pixels。")
        if settings.render_max_pages > 100 or settings.render_timeout > 120:
            raise StudioError(
                "invalid_config", "渲染页数最多 100 页，超时最多 120 秒。"
            )
        return settings


def identify(event, settings: Settings) -> Actor:
    if (
        not event
        or event.get_platform_name() == "cron"
        or event.get_message_type()
        not in {MessageType.FRIEND_MESSAGE, MessageType.GROUP_MESSAGE}
    ):
        raise StudioError(
            "invalid_context", "人格管理仅接受真实用户的当前私聊或群聊上下文。"
        )
    if not event.is_private_chat() and not settings.allow_group_chat:
        raise StudioError(
            "private_only",
            "群聊使用未开放，请在私聊中操作，或由管理员开启 allow_group_chat。",
        )
    platform_id, user_id = event.get_platform_id(), event.get_sender_id()
    if any(
        not isinstance(value, str) or not value or value.strip() != value
        for value in (platform_id, user_id)
    ):
        raise StudioError("invalid_identity", "缺少有效的平台 ID 或用户 ID。")
    admin = (
        event.is_admin() and event.get_extra("_api_key_allow_admin_role") is not False
    )
    actor = Actor(platform_id, user_id, admin)
    if not actor.admin and (platform_id, user_id) not in settings.whitelist:
        raise StudioError("forbidden", "仅管理员或白名单用户可使用人格管理。")
    return actor


def record_ref(persona: dict) -> str:
    if type(persona.get("id")) is not int or not isinstance(
        persona.get("created_at"), str
    ):
        raise StudioError("invalid_record", "核心人格缺少内部 ID 或创建时间。")
    return canonical({"id": persona["id"], "created_at": persona["created_at"]})


def check_payload(payload, allowed: set[str], required: set[str] = frozenset()) -> None:
    if not isinstance(payload, dict):
        raise StudioError("invalid_payload", "payload 必须是 JSON 对象。")
    unexpected = sorted(set(payload) - allowed)
    missing = sorted(required - set(payload))
    details = {
        "allowed_fields": sorted(allowed),
        "required_fields": sorted(required),
        "unexpected_fields": unexpected,
        "missing_fields": missing,
    }
    if unexpected:
        raise StudioError(
            "invalid_payload",
            f"未知参数：{', '.join(unexpected)}；请删除这些字段，不要仅修改其值。"
            f"允许参数：{', '.join(sorted(allowed))}。",
            details,
        )
    if missing:
        raise StudioError("invalid_payload", f"缺少参数：{', '.join(missing)}", details)


def view_options(payload: dict) -> tuple[str, str]:
    if "include_md" in payload:
        raise StudioError(
            "invalid_payload",
            "include_md 已移除；请使用 format=markdown 发送文件，并用 content 选择内容。",
        )
    output_format = payload.get("format", "image")
    content = payload.get("content", "prompt")
    if not isinstance(output_format, str) or output_format not in (
        "text",
        "image",
        "markdown",
    ):
        raise StudioError("invalid_payload", "format 仅支持 text/image/markdown。")
    if not isinstance(content, str) or content not in ("metadata", "prompt", "all"):
        raise StudioError("invalid_payload", "content 仅支持 metadata/prompt/all。")
    return output_format, content


def validate_id(value, *, create: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value.strip() != value
        or any(ord(char) < 32 for char in value)
    ):
        raise StudioError(
            "invalid_payload",
            "人格 ID 必须为 1～255 字符，不能包含控制字符或首尾空白。",
        )
    if create and value in RESERVED_IDS:
        raise StudioError("reserved_id", "不能创建核心保留 ID 的人格。")
    return value


def validate_content(data: dict, settings: Settings, *, create: bool = False) -> None:
    if create and "system_prompt" not in data:
        raise StudioError("invalid_payload", "创建人格必须提供 system_prompt。")
    for name, limit in (
        ("system_prompt", settings.max_prompt_chars),
        ("custom_error_message", settings.max_error_chars),
    ):
        if name not in data:
            continue
        value = data[name]
        if name == "custom_error_message" and value is None:
            continue
        if not isinstance(value, str) or (
            name == "system_prompt" and not value.strip()
        ):
            raise StudioError(
                "invalid_payload",
                f"{name} 必须为字符串；仅 custom_error_message 可以为 null。",
            )
        if len(value) > limit:
            raise StudioError("limit_exceeded", f"{name} 超过字符上限 {limit}。")
    if "begin_dialogs" in data:
        dialogs = data["begin_dialogs"]
        if (
            not isinstance(dialogs, list)
            or any(not isinstance(value, str) for value in dialogs)
            or len(dialogs) % 2
        ):
            raise StudioError(
                "invalid_payload",
                "begin_dialogs 必须为偶数条字符串，依次为用户、助手对话；清空请传 []。",
            )
        if sum(map(len, dialogs)) > settings.max_dialog_chars:
            raise StudioError(
                "limit_exceeded",
                f"预设对话总长度超过 {settings.max_dialog_chars} 字符。",
            )
    for name in ("tools", "skills"):
        if name not in data or data[name] is None:
            continue
        value = data[name]
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item or item.strip() != item
            for item in value
        ):
            raise StudioError("invalid_payload", f"{name} 必须为 null 或名称列表。")
        if len(value) > 200 or len(value) != len(set(value)):
            raise StudioError(
                "invalid_payload", f"{name} 最多 200 项，且不能包含重复名称。"
            )
