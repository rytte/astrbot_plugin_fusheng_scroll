import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from astrbot.api.platform import MessageType
from astrbot.core.event_bus import UNIQUE_SESSION_ID_BUILDERS
from astrbot_plugin_fusheng_scroll.domain import Settings, StudioError
from astrbot_plugin_fusheng_scroll.main import PersonaStudioPlugin


@pytest.fixture
def group_env(created):
    created.config["allow_group_chat"] = True
    created.event = created.native_event(group=True)
    created.event.message_obj.group_id = "test-group"
    created.event.session_id = "test-group"
    created.admin = created.native_event("admin", admin=True, group=True)
    created.admin.message_obj.group_id = "test-group"
    created.admin.session_id = "test-group"
    return created


def test_group_chat_config_default_and_boolean_values():
    assert Settings().allow_group_chat is True
    assert Settings.parse({}).allow_group_chat is True
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["allow_group_chat"]["default"] is True
    for enabled in (False, True):
        assert Settings.parse({"allow_group_chat": enabled}).allow_group_chat is enabled


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_group_chat_config_rejects_non_boolean_values(value):
    with pytest.raises(StudioError, match="allow_group_chat 必须为布尔值"):
        Settings.parse({"allow_group_chat": value})


@pytest.mark.parametrize(
    "family,action,payload",
    [
        ("manage", "list", {}),
        ("versions", "list", {"persona_id": "alice"}),
        ("protect", "lock", {"persona_id": "alice"}),
        ("whitelist", "add", {"user_id": "new"}),
    ],
)
async def test_groups_rejected_when_disabled(created, family, action, payload):
    created.config["allow_group_chat"] = False
    event = created.native_event("admin", admin=True, group=True)
    response = await created.studio.execute(event, family, action, payload)
    assert response["error"]["code"] == "private_only"


@pytest.mark.parametrize("family", ["manage", "versions", "protect"])
async def test_groups_enabled_by_default(created, family):
    event = created.native_event("admin", admin=True, group=True, text="/fs lock alice")
    action = "lock" if family == "protect" else "list"
    payload = {} if family == "manage" else {"persona_id": "alice"}
    response = await created.studio.execute(event, family, action, payload)
    assert response["ok"], response


@pytest.mark.parametrize("case", ["none", "cron", "system"])
async def test_opening_groups_does_not_allow_non_chat_contexts(group_env, case):
    env = group_env
    event = env.event
    if case == "none":
        event = None
    elif case == "cron":
        event.platform_meta.name = "cron"
    else:
        event.message_obj.type = MessageType.OTHER_MESSAGE
    response = await env.studio.execute(event, "manage", "list", {})
    assert response["error"]["code"] == "invalid_context"


async def test_group_manage_and_versions_follow_private_rules(group_env):
    env = group_env
    result = await env.studio.execute(env.event, "manage", "list", {})
    assert result["ok"] and result["data"]["total"] == 1
    result = await env.studio.execute(
        env.event,
        "manage",
        "create",
        {"persona_id": "group-new", "system_prompt": "original"},
    )
    assert result["data"]["owner"] == ["platform", "user"]
    private = env.native_event()
    result = await env.studio.execute(
        private, "manage", "get", {"persona_id": "group-new"}
    )
    assert result["ok"]
    updated = await env.studio.execute(
        env.event,
        "manage",
        "update",
        {
            "persona_id": "group-new",
            "system_prompt": "updated",
            "tools": ["echo"],
            "skills": [],
        },
    )
    assert updated["ok"]
    backup_id = updated["data"]["backup_id"]
    listed = await env.studio.execute(
        env.event, "versions", "list", {"persona_id": "group-new"}
    )
    assert listed["ok"] and listed["data"]["total"] == 1
    backup = await env.studio.execute(
        env.event, "versions", "get", {"backup_id": backup_id}
    )
    restored = await env.studio.execute(
        env.event,
        "versions",
        "restore",
        {
            "backup_id": backup_id,
            "expected_revision": backup["data"]["expected_revision"],
        },
    )
    assert restored["ok"] and restored["data"]["persona"]["system_prompt"] == "original"
    removed = await env.studio.execute(
        env.event, "versions", "delete", {"backup_id": backup_id}
    )
    assert removed["ok"]
    deleted = await env.studio.execute(
        env.event, "manage", "delete", {"persona_id": "group-new"}
    )
    assert deleted["ok"] and await env.studio.current("group-new") is None


async def test_group_ownership_does_not_become_group_authorization(group_env):
    env = group_env
    env.config["whitelist"].append({"platform_id": "platform", "user_id": "other"})
    other = env.native_event("other", group=True)
    other.message_obj.group_id = "test-group"
    other.session_id = "test-group"
    denied = await env.studio.execute(other, "manage", "get", {"persona_id": "alice"})
    assert denied["error"]["code"] == "not_found_or_forbidden"
    outsider = env.native_event("outsider", group=True)
    denied = await env.studio.execute(outsider, "manage", "list", {})
    assert denied["error"]["code"] == "forbidden"
    await env.manager.create_persona("external", "prompt")
    allowed = await env.studio.execute(
        env.admin, "manage", "get", {"persona_id": "external"}
    )
    assert allowed["ok"]
    env.admin.set_extra("_api_key_allow_admin_role", False)
    denied = await env.studio.execute(env.admin, "manage", "list", {})
    assert denied["error"]["code"] == "forbidden"


@pytest.mark.parametrize("isolated", [False, True])
async def test_group_switch_respects_core_current_session(group_env, isolated):
    env = group_env
    env.event.platform_meta.name = "aiocqhttp"
    if isolated:
        env.event.session_id = UNIQUE_SESSION_ID_BUILDERS["aiocqhttp"](env.event)
    member = env.native_event("other", group=True)
    member.platform_meta.name = "aiocqhttp"
    member.message_obj.group_id = "test-group"
    member.session_id = (
        UNIQUE_SESSION_ID_BUILDERS["aiocqhttp"](member) if isolated else "test-group"
    )
    manager = env.context.conversation_manager
    history = [{"role": "user", "content": "group history"}]
    current_id = await manager.new_conversation(
        env.event.unified_msg_origin, content=history
    )
    private = env.native_event()
    private_id = await manager.new_conversation(private.unified_msg_origin)
    other_group = env.native_event(group=True)
    other_group.session_id = "other-group"
    other_id = await manager.new_conversation(other_group.unified_msg_origin)
    untouched = [private_id, other_id]
    if isolated:
        untouched.append(await manager.new_conversation(member.unified_msg_origin))
    before = {
        conversation_id: await env.db.get_conversation_by_id(conversation_id)
        for conversation_id in untouched
    }
    core_config = dict(env.core_config)
    records = env.storage.records("alice")
    response = await env.studio.execute(
        env.event, "manage", "switch", {"persona_id": "alice"}
    )
    assert (
        response["ok"] and response["data"]["effective"] == "next_conversation_request"
    )
    current = await env.db.get_conversation_by_id(current_id)
    assert current.persona_id == "alice" and current.content == history
    assert env.storage.records("alice") == records
    assert env.core_config == core_config
    for conversation_id, previous in before.items():
        after = await env.db.get_conversation_by_id(conversation_id)
        assert (
            after.persona_id == previous.persona_id
            and after.content == previous.content
        )
    member_id = await manager.get_curr_conversation_id(member.unified_msg_origin)
    assert (member_id == current_id) is (not isolated)
    env.config["allow_group_chat"] = False
    denied = await env.studio.execute(env.event, "manage", "list", {})
    assert denied["error"]["code"] == "private_only"
    assert (await env.db.get_conversation_by_id(current_id)).persona_id == "alice"


async def test_group_switch_does_not_create_or_override_forced_conversations(
    group_env, monkeypatch
):
    env = group_env
    result = await env.studio.execute(
        env.event, "manage", "switch", {"persona_id": "alice"}
    )
    assert result["error"]["code"] == "no_current_conversation"
    monkeypatch.setattr(
        "astrbot_plugin_fusheng_scroll.service.sp.get_async",
        AsyncMock(return_value={"persona_id": "forced"}),
    )
    result = await env.studio.execute(
        env.event, "manage", "switch", {"persona_id": "alice"}
    )
    assert result["error"]["code"] == "forced_persona"


@pytest.mark.parametrize("command", ["fs", "浮生"])
async def test_group_native_lock_commands_keep_write_protection(
    group_env, command, persona_command_filter
):
    env = group_env
    plugin = PersonaStudioPlugin(env.context, env.config)
    plugin.studio = env.studio
    command_filter = persona_command_filter
    for action in ("lock", "unlock"):
        event = env.native_event(group=True, text=f"/{command} {action} alice")
        event.message_str = event.message_obj.message_str[1:]
        event.is_at_or_wake_command = True
        assert command_filter.filter(event, env.core_config)
        replies = [
            reply
            async for reply in plugin.persona(event, **event.get_extra("parsed_params"))
        ]
        assert len(replies) == 1
        assert env.storage.records("alice")[0]["locked"] == int(action == "lock")
        result = await env.studio.execute(
            env.admin,
            "manage",
            "update",
            {"persona_id": "alice", "system_prompt": "changed"},
        )
        if action == "lock":
            assert result["error"]["code"] == "persona_locked"
        else:
            assert result["ok"]


async def test_group_commands_still_require_direct_user_messages(group_env):
    env = group_env
    event = env.native_event(group=True, text="/fs lock alice")
    event.message_obj.raw_message = None
    result = await env.studio.execute(event, "protect", "lock", {"persona_id": "alice"})
    assert result["error"]["code"] == "real_command_only"


async def test_closing_group_access_during_operation_prevents_write(
    group_env, monkeypatch
):
    env = group_env
    original = env.studio.current
    before = env.storage.backups(env.storage.records("alice")[0]["chain_id"])

    async def close(persona_id):
        persona = await original(persona_id)
        env.config["allow_group_chat"] = False
        return persona

    monkeypatch.setattr(env.studio, "current", close)
    result = await env.studio.execute(
        env.event,
        "manage",
        "update",
        {"persona_id": "alice", "system_prompt": "denied"},
    )
    assert result["error"]["code"] == "private_only"
    assert (await original("alice"))["system_prompt"] == "版本1"
    assert env.storage.backups(env.storage.records("alice")[0]["chain_id"]) == before
