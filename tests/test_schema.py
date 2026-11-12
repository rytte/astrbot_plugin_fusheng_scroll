import jsonschema
import pytest
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot_plugin_fusheng_scroll.core.domain import MODEL_TOOLS, Settings, StudioError
from astrbot_plugin_fusheng_scroll.personas.tools import tool_parameters

ARGUMENTS = {
    "persona_list": {},
    "persona_get": {"persona_id": "alice"},
    "persona_create": {"persona_id": "alice", "system_prompt": "prompt"},
    "persona_update": {"persona_id": "alice", "system_prompt": "new"},
    "persona_delete": {"persona_id": "alice"},
    "persona_switch": {"persona_id": "alice"},
    "persona_backup_list": {"persona_id": "alice"},
    "persona_backup_get": {"backup_id": "backup"},
    "persona_backup_restore": {"backup_id": "backup", "expected_revision": "token"},
    "persona_backup_delete": {"backup_id": "backup"},
}
FIELDS = {
    "persona_list": {"offset", "limit"},
    "persona_get": {"persona_id", "format", "content"},
    "persona_create": {
        "persona_id",
        "system_prompt",
        "begin_dialogs",
        "tools",
        "skills",
        "custom_error_message",
    },
    "persona_update": {
        "persona_id",
        "system_prompt",
        "begin_dialogs",
        "tools",
        "skills",
        "custom_error_message",
    },
    "persona_delete": {"persona_id"},
    "persona_switch": {"persona_id"},
    "persona_backup_list": {"persona_id", "offset", "limit"},
    "persona_backup_get": {"backup_id", "format", "content"},
    "persona_backup_restore": {"backup_id", "expected_revision"},
    "persona_backup_delete": {"backup_id"},
}


@pytest.mark.parametrize("name", MODEL_TOOLS)
def test_parameters_only_expose_fields_for_one_operation(name):
    schema = tool_parameters(name, Settings())
    jsonschema.Draft202012Validator.check_schema(schema)
    assert set(schema["properties"]) == FIELDS[name]
    jsonschema.validate(ARGUMENTS[name], schema)
    for field in (
        set().union(*FIELDS.values(), {"action", "payload", "owner", "include_md"})
        - FIELDS[name]
    ):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**ARGUMENTS[name], field: "unexpected"}, schema)
    for field in schema["required"]:
        arguments = {
            key: value for key, value in ARGUMENTS[name].items() if key != field
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(arguments, schema)


@pytest.mark.parametrize("name", ["persona_get", "persona_backup_get"])
def test_get_view_options_and_platform_routing(name):
    schema = tool_parameters(name, Settings())
    description = schema["properties"]["format"]["description"]
    assert "OneBot/Aiocqhttp" in description
    assert "Satori" in description and "合并转发" in description
    assert "其他适配器发送普通文本" in description
    for output_format in ("text", "image", "markdown"):
        for content in ("metadata", "prompt", "all"):
            jsonschema.validate(
                {**ARGUMENTS[name], "format": output_format, "content": content}, schema
            )
    for options in ({"format": "md"}, {"format": None}, {"content": "both"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**ARGUMENTS[name], **options}, schema)


@pytest.mark.parametrize("name", MODEL_TOOLS)
def test_provider_declarations_keep_operation_fields_and_required_parameters(name):
    schema = tool_parameters(name, Settings())
    tools = ToolSet([FunctionTool(name=name, description="Test", parameters=schema)])
    declarations = [
        tools.openai_schema()[0]["function"]["parameters"],
        tools.anthropic_schema()[0]["input_schema"],
        tools.google_schema()["function_declarations"][0]["parameters"],
    ]
    for declaration in declarations:
        assert set(declaration["properties"]) == FIELDS[name]
        assert declaration["required"] == schema["required"]
        jsonschema.validate(ARGUMENTS[name], declaration)


def test_gemini_nullable_capabilities_preserve_omission_semantics():
    schema = tool_parameters("persona_update", Settings())
    tool = FunctionTool(name="persona_update", description="Update", parameters=schema)
    declaration = ToolSet([tool]).google_schema()["function_declarations"][0]
    tools = declaration["parameters"]["properties"]["tools"]
    assert tools["anyOf"][0]["type"] == "array"
    assert tools["anyOf"][0]["items"]["type"] == "string"
    assert tools["anyOf"][1]["type"] == "null"
    for value in (None, [], ["echo"]):
        jsonschema.validate({"persona_id": "alice", "tools": value}, schema)
    assert "tools" not in schema["required"]
    assert "skills" not in schema["required"]


def test_logged_mixed_version_parameters_are_rejected_by_list_schema():
    schema = tool_parameters("persona_backup_list", Settings())
    arguments = {
        "persona_id": "night_repair_shop_cen_yao_20261010",
        "backup_id": "b6e4bb4a-4826-48f0-9c2f-3f4e7260e953",
        "content": "metadata",
        "expected_revision": "",
        "format": "text",
        "limit": 100,
        "offset": 0,
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(arguments, schema)


@pytest.mark.parametrize("name", ["persona_manage", "persona_versions"])
def test_removed_combined_tools_are_not_aliased(name):
    with pytest.raises(StudioError) as error:
        tool_parameters(name, Settings())
    assert error.value.code == "invalid_tool"
