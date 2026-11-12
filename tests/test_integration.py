import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api import sp
from astrbot.core import astr_agent_tool_exec, astr_main_agent
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.db.po import Conversation
from astrbot.core.provider.entities import ProviderRequest
from astrbot_plugin_fusheng_scroll.core.domain import MODEL_TOOLS, Settings
from astrbot_plugin_fusheng_scroll.main import PersonaStudioPlugin
from astrbot_plugin_fusheng_scroll.personas.tools import tool_parameters


@pytest.mark.parametrize("path", ["main", "handoff", "full"])
async def test_core_permission_checks_depend_on_tool_selection(
    created, monkeypatch, path
):
    monkeypatch.setattr(
        sp, "global_get", AsyncMock(return_value={"_default": {"echo": "admin"}})
    )
    monkeypatch.setattr(
        astr_main_agent,
        "SkillManager",
        lambda: SimpleNamespace(list_skills=lambda **kwargs: []),
    )
    run_context = ContextWrapper(
        context=SimpleNamespace(context=created.context, event=created.event)
    )
    if path == "full":
        toolset = created.tools.get_full_tool_set()
    elif path == "main":
        request = ProviderRequest(
            conversation=Conversation(
                platform_id="platform",
                user_id=created.event.unified_msg_origin,
                cid="conversation",
                persona_id="alice",
            )
        )
        await astr_main_agent._ensure_persona_and_skills(
            request, {"computer_use_runtime": "none"}, created.context, created.event
        )
        toolset = request.func_tool
    else:
        monkeypatch.setattr(astr_agent_tool_exec, "llm_tools", created.tools)
        toolset = FunctionToolExecutor._build_handoff_toolset(run_context, ["echo"])
    tool = toolset.get_tool("echo")
    results = [
        result async for result in FunctionToolExecutor.execute(tool, run_context)
    ]
    if path == "full":
        assert "Permission denied" in results[-1].content[0].text
        created.handler.assert_not_awaited()
    else:
        assert results[-1].content[0].text == "echo-result"
        created.handler.assert_awaited_once()
    created.handler.reset_mock()
    created.event.role = "admin"
    results = [
        result async for result in FunctionToolExecutor.execute(tool, run_context)
    ]
    assert results[-1].content[0].text == "echo-result"
    created.handler.assert_awaited_once()


async def test_webui_write_can_be_overwritten_during_restore(created, monkeypatch):
    first = await created.studio.execute(
        created.event,
        "manage",
        "update",
        {"persona_id": "alice", "system_prompt": "版本2"},
    )
    backup_id = first["data"]["backup_id"]
    version = await created.studio.execute(
        created.event, "versions", "get", {"backup_id": backup_id}
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original = created.db.update_persona

    async def pause(persona_id, *args, **kwargs):
        if kwargs.get("system_prompt") == "版本1" or (args and args[0] == "版本1"):
            entered.set()
            await release.wait()
        return await original(persona_id, *args, **kwargs)

    monkeypatch.setattr(created.db, "update_persona", pause)
    restore = asyncio.create_task(
        created.studio.execute(
            created.event,
            "versions",
            "restore",
            {
                "backup_id": backup_id,
                "expected_revision": version["data"]["expected_revision"],
            },
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    try:
        await asyncio.wait_for(
            created.manager.update_persona("alice", system_prompt="later WebUI change"),
            5,
        )
        assert (await created.studio.current("alice"))[
            "system_prompt"
        ] == "later WebUI change"
    finally:
        release.set()
        restored = await asyncio.wait_for(restore, 5)
    assert restored["ok"] and restored["data"]["persona"]["system_prompt"] == "版本1"
    assert (await created.studio.current("alice"))["system_prompt"] == "版本1"


async def test_revocation_during_quota_check_prevents_create(env, monkeypatch):
    original = env.studio.check_quota

    async def revoke(actor, settings):
        await original(actor, settings)
        env.config["whitelist"] = []

    monkeypatch.setattr(env.studio, "check_quota", revoke)
    response = await env.studio.execute(
        env.event,
        "manage",
        "create",
        {"persona_id": "denied", "system_prompt": "prompt"},
    )
    assert response["error"]["code"] == "forbidden"
    assert await env.studio.current("denied") is None


@pytest.mark.parametrize(
    "directory", ["astrbot_plugin_fusheng_scroll", "astrbot_plugin_persona_studio"]
)
def test_plugin_packages_load_under_astrbot_namespace(tmp_path, directory):
    """Import a standalone installation, without the test package alias."""
    import os
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = Path(os.environ.get("ASTRBOT_SOURCE", root.parent / "AstrBot"))
    plugins = tmp_path / "data" / "plugins"
    plugins.mkdir(parents=True)
    (tmp_path / "data" / "__init__.py").touch()
    (plugins / "__init__.py").touch()
    shutil.copytree(
        root,
        plugins / directory,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "tests",
            "docs",
        ),
    )
    script = """
import importlib
import sys
from pathlib import Path

from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.core.star.star_manager import PluginManager

directory = sys.argv[1]
prefix = f"data.plugins.{directory}"
discovered = PluginManager._get_modules("data/plugins")
assert any(item["pname"] == directory and item["module"] == "main" for item in discovered)
entry = importlib.import_module(prefix + ".main")
assert entry.PersonaStudioPlugin.__module__ == prefix + ".main"
assert star_map[entry.__name__].star_cls_type is entry.PersonaStudioPlugin
for name in (*entry.MODEL_TOOLS, *entry.CARD_MODEL_TOOLS):
    handler = getattr(entry.PersonaStudioPlugin, name)
    metadata = star_handlers_registry.get_handler_by_full_name(f"{handler.__module__}_{handler.__name__}")
    assert metadata is not None and metadata.handler_module_path == entry.__name__, name
related = set(PluginManager._get_plugin_related_modules(directory))
for module in (
    "core.branding", "core.domain", "core.storage",
    "characters.assets", "characters.codec", "characters.service", "characters.storage",
    "characters.tools", "characters.web", "characters.worldbook",
    "personas.service", "personas.tools", "presentation.rendering", "presentation.templates",
):
    name = f"{prefix}.{module}"
    loaded = importlib.import_module(name)
    assert Path(loaded.__file__).resolve().is_relative_to(Path("data/plugins", directory).resolve())
    assert name in related, name
"""
    result = subprocess.run(
        [sys.executable, "-c", script, directory],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(tmp_path), str(source))),
            "ASTRBOT_ROOT": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_plugin_initializes_without_core_patch(env, tmp_path, monkeypatch):
    from astrbot.api.star import StarTools

    data_dirs = []

    def data_dir(name):
        data_dirs.append(name)
        return tmp_path / "plugin"

    monkeypatch.setattr(StarTools, "get_data_dir", data_dir)
    plugin = PersonaStudioPlugin(env.context, env.config)
    await plugin.initialize()
    try:
        assert plugin.studio.manager is env.manager
        assert data_dirs == ["astrbot_plugin_persona_studio"]
        assert plugin.maintenance_task is not None
        for name in MODEL_TOOLS:
            tool = env.tools.get_func(name)
            assert "action" not in tool.parameters["properties"]
            assert "payload" not in tool.parameters["properties"]
            assert "失败不要自动重试" in tool.description
        assert env.tools.get_func("persona_manage") is None
        assert env.tools.get_func("persona_versions") is None
    finally:
        await plugin.terminate()


async def test_plugin_tools_and_command_dispatch(created):
    plugin = PersonaStudioPlugin(created.context, created.config)
    plugin.studio = created.studio
    assert '"ok": true' in await plugin.persona_list(created.event)
    message = created.native_event(text="/fs lock alice")
    replies = [reply async for reply in plugin.persona(message, "lock alice")]
    assert len(replies) == 1
    assert (
        await created.studio.execute(
            created.event,
            "manage",
            "update",
            {"persona_id": "alice", "system_prompt": "blocked"},
        )
    )["error"]["code"] == "persona_locked"


@pytest.mark.parametrize("name", MODEL_TOOLS)
async def test_individual_tools_dispatch_fixed_operations(created, name):
    plugin = PersonaStudioPlugin(created.context, created.config)
    plugin.dispatch = AsyncMock(return_value='{"ok": true}')
    parameters = tool_parameters(name, Settings())
    arguments = {field: "value" for field in parameters["required"]}
    if name == "persona_update":
        arguments["system_prompt"] = "new prompt"
    tool = FunctionTool(
        name=name,
        description="Test",
        parameters=parameters,
        handler=getattr(plugin, name),
        handler_module_path="astrbot_plugin_fusheng_scroll.main",
    )
    run_context = ContextWrapper(
        context=SimpleNamespace(context=created.context, event=created.event)
    )
    results = [
        result
        async for result in FunctionToolExecutor.execute(tool, run_context, **arguments)
    ]
    assert results[-1].content[0].text == '{"ok": true}'
    family, action = MODEL_TOOLS[name]
    plugin.dispatch.assert_awaited_once_with(created.event, family, action, arguments)


async def test_delete_persona_then_delete_its_backup_without_mixed_parameters(created):
    plugin = PersonaStudioPlugin(created.context, created.config)
    plugin.studio = created.studio
    deleted = json.loads(await plugin.persona_delete(created.event, persona_id="alice"))
    assert deleted["ok"], deleted
    backup_id = deleted["data"]["backup_id"]
    arguments = {
        "persona_id": "alice",
        "backup_id": backup_id,
        "content": "metadata",
        "expected_revision": "",
        "format": "text",
        "limit": 100,
        "offset": 0,
    }
    for revision in ("", "x", deleted["data"]["expected_revision"]):
        invalid = json.loads(
            await plugin.persona_backup_list(
                created.event, **{**arguments, "expected_revision": revision}
            )
        )
        assert invalid["error"]["code"] == "invalid_payload"
        details = invalid["error"]["details"]
        assert details["allowed_fields"] == ["limit", "offset", "persona_id"]
        assert details["unexpected_fields"] == [
            "backup_id",
            "content",
            "expected_revision",
            "format",
        ]
        assert not details["core_written"] and not details["plugin_written"]
        assert not details["mutation_started"]
        assert "不要仅修改其值" in invalid["error"]["message"]
    listed = json.loads(
        await plugin.persona_backup_list(created.event, persona_id="alice")
    )
    assert [item["backup_id"] for item in listed["data"]["items"]] == [backup_id]
    removed = json.loads(
        await plugin.persona_backup_delete(created.event, backup_id=backup_id)
    )
    assert removed["ok"], removed
    assert removed["data"]["deleted"] and not removed["data"]["undoable"]
    assert await created.studio.current("alice") is None
    listed = json.loads(
        await plugin.persona_backup_list(created.event, persona_id="alice")
    )
    assert listed["data"]["items"] == []
    created.event.send.assert_not_called()


async def test_explicit_builtin_tool_stays_available(created, monkeypatch):
    from astrbot.core.tools.message_tools import SendMessageToUserTool

    tool = created.tools.get_builtin_tool(SendMessageToUserTool)
    await created.manager.update_persona("alice", tools=[tool.name])
    monkeypatch.setattr(
        astr_main_agent,
        "SkillManager",
        lambda: SimpleNamespace(list_skills=lambda **kwargs: []),
    )
    request = ProviderRequest(
        conversation=Conversation(
            platform_id="platform",
            user_id=created.event.unified_msg_origin,
            cid="conversation",
            persona_id="alice",
        )
    )
    await astr_main_agent._ensure_persona_and_skills(
        request, {"computer_use_runtime": "none"}, created.context, created.event
    )
    assert isinstance(request.func_tool.get_tool(tool.name), SendMessageToUserTool)


async def test_plugin_lifecycle_persists_metadata(env, tmp_path, monkeypatch):
    from astrbot.api.star import StarTools

    monkeypatch.setattr(StarTools, "get_data_dir", lambda name: tmp_path / "plugin")
    plugin = PersonaStudioPlugin(env.context, env.config)
    await plugin.initialize()
    assert plugin.maintenance_task is not None
    response = await plugin.studio.execute(
        env.event,
        "manage",
        "create",
        {"persona_id": "persistent", "system_prompt": "prompt"},
    )
    assert response["ok"]
    await plugin.terminate()
    await plugin.initialize()
    try:
        response = await plugin.studio.execute(
            env.event, "manage", "get", {"persona_id": "persistent"}
        )
        assert response["data"]["owner"] == ["platform", "user"]
    finally:
        await plugin.terminate()


@pytest.mark.parametrize("command", ["fs", "浮生"])
@pytest.mark.parametrize("arguments", ["lock alice", ""])
async def test_native_command_filter_preserves_greedy_arguments(
    created, command, arguments, persona_command_filter
):
    plugin = PersonaStudioPlugin(created.context, created.config)
    plugin.studio = created.studio
    command_filter = persona_command_filter
    text = f"/{command} {arguments}".rstrip()
    event = created.native_event(text=text)
    event.message_str = text[1:]
    event.is_at_or_wake_command = True
    assert command_filter.filter(event, created.core_config)
    parsed = event.get_extra("parsed_params")
    assert parsed == {"arguments": arguments}
    replies = [reply async for reply in plugin.persona(event, **parsed)]
    assert len(replies) == 1
    record = created.storage.records("alice")[0]
    assert record["locked"] == int(bool(arguments))
    if not arguments:
        assert "浮生绘卷" in replies[0].chain[0].text
        assert "/fs lock" in replies[0].chain[0].text
        assert "/浮生" in replies[0].chain[0].text
        assert "/ps" not in replies[0].chain[0].text


@pytest.mark.parametrize("command", ["fs", "浮生"])
@pytest.mark.parametrize("prefix", ["/", "!"])
async def test_native_command_can_lock_id_with_repeated_spaces(
    created, command, prefix, persona_command_filter
):
    result = await created.studio.execute(
        created.event,
        "manage",
        "create",
        {"persona_id": "with  spaces", "system_prompt": "prompt"},
    )
    assert result["ok"]
    plugin = PersonaStudioPlugin(created.context, created.config)
    plugin.studio = created.studio
    created.core_config["wake_prefix"] = [prefix]
    event = created.native_event(text=f"{prefix}{command} lock with  spaces")
    event.message_str = f"{command} lock with  spaces"
    event.is_at_or_wake_command = True
    command_filter = persona_command_filter
    assert command_filter.filter(event, created.core_config)
    replies = [
        reply
        async for reply in plugin.persona(event, **event.get_extra("parsed_params"))
    ]
    assert len(replies) == 1
    assert created.storage.records("with  spaces")[0]["locked"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "/persona lock alice",
        "/persona unlock alice",
        "/fsx lock alice",
        "/浮生绘卷 lock alice",
        "/ps",
        "/ps lock alice",
        "/ps unlock alice",
        "/ps whitelist add user",
        "/ps whitelist remove user",
    ],
)
async def test_other_commands_are_not_recognized(created, text, persona_command_filter):
    event = created.native_event(text=text)
    event.message_str = text[1:]
    event.is_at_or_wake_command = True
    command_filter = persona_command_filter
    assert not command_filter.filter(event, created.core_config)
