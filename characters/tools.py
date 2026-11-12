from __future__ import annotations

from .service import CARD_ACTIONS

CARD_MODEL_TOOLS = {
    "character_list": "list",
    "character_get": "get",
    "character_create": "create",
    "character_update": "update",
    "character_archive": "archive",
    "character_snapshot_list": "snapshots",
    "character_snapshot_get": "snapshot",
    "character_snapshot_restore": "restore",
}


def card_tool_parameters(name):
    action = CARD_MODEL_TOOLS[name]
    properties = {
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "deleted": {
            "type": "boolean",
            "description": "是否查询已归档角色卡，默认 false",
        },
        "card_id": {
            "type": "string",
            "description": "查询返回的角色卡稳定 ID，不是角色名称或人格 ID",
        },
        "snapshot_id": {"type": "string", "description": "查询返回的定版快照 ID"},
        "expected_revision": {
            "type": "integer",
            "minimum": 1,
            "description": "最新查询返回的内部 revision，仅用于并发检查，不是角色版本",
        },
        "card": {
            "type": "object",
            "description": "完整 Character Card V3 对象，spec 为 chara_card_v3、spec_version 为 3.0，包含 data；旧 V2 输入会转换为 V3",
        },
        "data": {
            "type": "object",
            "description": "仅更新这些 data 字段，未提供字段保持不变。改变 character_version 时才生成快照。",
        },
        "options": {
            "type": "object",
            "description": "AstrBot 配置：tools/skills 为 null、[] 或名称列表，custom_error_message 为字符串或 null，greeting_index 为非负整数",
        },
        "bind": {
            "type": "boolean",
            "description": "新建后是否创建并绑定 AstrBot 人格，默认 true",
        },
    }
    allowed, required = CARD_ACTIONS[action]
    allowed = allowed - {"import_id"}
    if action == "create":
        required = {"card"}
    return {
        "type": "object",
        "properties": {
            key: value for key, value in properties.items() if key in allowed
        },
        "required": sorted(required),
        "additionalProperties": False,
    }
