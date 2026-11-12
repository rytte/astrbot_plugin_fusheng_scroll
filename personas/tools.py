from copy import deepcopy

from ..core.domain import ACTION_PAYLOADS, MODEL_TOOLS, StudioError


def tool_parameters(name: str, settings) -> dict:
    if name not in MODEL_TOOLS:
        raise StudioError("invalid_tool", f"未知模型工具：{name}。")
    family, action = MODEL_TOOLS[name]
    common = {
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "list 的分页起始位置，默认 0",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "list 每页数量，默认 20",
        },
        "format": {
            "type": "string",
            "enum": ["text", "image", "markdown"],
            "description": "仅 get：text 在 OneBot/Aiocqhttp 与 Satori 使用合并转发，其他适配器发送普通文本；image 为图片（默认），markdown 为 MD 文件；每次仅发送一种形式",
        },
        "content": {
            "type": "string",
            "enum": ["metadata", "prompt", "all"],
            "description": "仅 get：metadata 为版本权限、人格信息及工具/技能和自定义错误回复；prompt 为系统提示词与预设对话（默认）；all 为两者",
        },
    }
    if family == "manage":
        names = {
            "anyOf": [
                {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 200,
                    "uniqueItems": True,
                },
                {"type": "null"},
            ],
            "description": "null 表示全部，[] 禁用，名称列表指定范围；省略保持不变",
        }
        properties = {
            **common,
            "persona_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "人格 ID，必须使用实际查询到或用户指定的完整 ID",
            },
            "system_prompt": {
                "type": "string",
                "minLength": 1,
                "maxLength": settings.max_prompt_chars,
                "description": "完整系统提示词；create 必填",
            },
            "begin_dialogs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "偶数条预设对话，用户、助手交替；[] 清空",
            },
            "tools": deepcopy(names),
            "skills": deepcopy(names),
            "custom_error_message": {
                "anyOf": [
                    {"type": "string", "maxLength": settings.max_error_chars},
                    {"type": "null"},
                ],
                "description": "自定义错误回复，null 清除",
            },
        }
    else:
        properties = {
            **common,
            "persona_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "查询备份的人格 ID；人格已删除时仍可查询其备份",
            },
            "backup_id": {
                "type": "string",
                "minLength": 1,
                "description": "备份 ID，只能使用查询或人格写操作返回的备份 ID，不是人格 ID",
            },
            "expected_revision": {
                "type": "string",
                "description": "restore 时必填，必须是最新查询返回的完整状态令牌，不是整数版本号",
            },
        }
    allowed, required = ACTION_PAYLOADS[family][action]
    return {
        "type": "object",
        "properties": {
            field: schema for field, schema in properties.items() if field in allowed
        },
        "required": sorted(required),
        "additionalProperties": False,
    }
