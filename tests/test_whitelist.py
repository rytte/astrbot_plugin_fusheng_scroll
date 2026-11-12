import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from astrbot.api import AstrBotConfig
from astrbot_plugin_fusheng_scroll.domain import Settings
from astrbot_plugin_fusheng_scroll.main import PersonaStudioPlugin


@pytest.fixture
def whitelist_env(created, tmp_path):
    config = AstrBotConfig(
        str(tmp_path / "plugin_config.json"), default_config=dict(created.config)
    )
    created.config = config
    created.studio.config = config
    return created


async def change(env, action, user_id, *, event=None):
    event = event or env.native_event(
        "admin", admin=True, text=f"/fs whitelist {action} {user_id}"
    )
    return await env.studio.execute(event, "whitelist", action, {"user_id": user_id})


async def test_add_is_immediate_and_persistent(whitelist_env):
    env = whitelist_env
    response = await change(env, "add", "new-user")
    assert response == {
        "ok": True,
        "data": {
            "platform_id": "platform",
            "user_id": "new-user",
            "whitelisted": True,
            "changed": True,
        },
    }
    assert env.config["whitelist"][-1] == {
        "__template_key": "user",
        "platform_id": "platform",
        "user_id": "new-user",
    }
    event = env.native_event("new-user")
    created = await env.studio.execute(
        event, "manage", "create", {"persona_id": "new", "system_prompt": "prompt"}
    )
    assert created["data"]["owner"] == ["platform", "new-user"]
    reloaded = AstrBotConfig(env.config.config_path, default_config={"whitelist": []})
    assert reloaded["whitelist"] == env.config["whitelist"]
    assert ("platform", "new-user") in Settings.parse(reloaded).whitelist


async def test_remove_preserves_locked_persona_and_backups(whitelist_env):
    env = whitelist_env
    updated = await env.studio.execute(
        env.event, "manage", "update", {"persona_id": "alice", "system_prompt": "new"}
    )
    backup_id = updated["data"]["backup_id"]
    locked = await env.studio.execute(
        env.native_event(text="/fs lock alice"),
        "protect",
        "lock",
        {"persona_id": "alice"},
    )
    assert locked["ok"]
    before = env.storage.records("alice")
    assert (await change(env, "remove", "user"))["data"]["changed"]
    denied = await env.studio.execute(
        env.event, "manage", "get", {"persona_id": "alice"}
    )
    assert denied["error"]["code"] == "forbidden"
    assert env.storage.records("alice") == before
    assert await env.studio.current("alice") is not None
    assert any(
        backup["backup_id"] == backup_id
        for backup in env.storage.backups(before[0]["chain_id"])
    )
    reloaded = AstrBotConfig(env.config.config_path, default_config={"whitelist": []})
    assert reloaded["whitelist"] == []
    assert (await change(env, "add", "user"))["ok"]
    detail = await env.studio.execute(
        env.event, "manage", "get", {"persona_id": "alice"}
    )
    assert detail["data"]["owner"] == ["platform", "user"]
    assert detail["data"]["locked"]
    backup = await env.studio.execute(
        env.event, "versions", "get", {"backup_id": backup_id}
    )
    assert backup["ok"]


@pytest.mark.parametrize("action,user_id", [("add", "user"), ("remove", "absent")])
async def test_noop_does_not_save(whitelist_env, monkeypatch, action, user_id):
    env = whitelist_env
    save = Mock(wraps=env.config.save_config)
    monkeypatch.setattr(AstrBotConfig, "save_config", save)
    response = await change(env, action, user_id)
    assert response["ok"] and not response["data"]["changed"]
    assert response["data"]["whitelisted"] == (action == "add")
    save.assert_not_called()


async def test_group_command_is_allowed_by_default(whitelist_env):
    env = whitelist_env
    event = env.native_event(
        "admin", admin=True, group=True, text="/fs whitelist add new"
    )
    response = await change(env, "add", "new", event=event)
    assert response["ok"], response
    assert ("platform", "new") in env.studio.settings().whitelist


async def test_identity_is_scoped_to_current_instance(whitelist_env):
    env = whitelist_env
    event = env.native_event(
        "admin", admin=True, platform="other-instance", text="/fs whitelist add user"
    )
    assert (await change(env, "add", "user", event=event))["ok"]
    assert set(env.studio.settings().whitelist) == {
        ("platform", "user"),
        ("other-instance", "user"),
    }
    assert (await change(env, "remove", "user"))["ok"]
    assert env.studio.settings().whitelist == (("other-instance", "user"),)
    member = env.native_event("user", platform="other-instance")
    result = await env.studio.execute(member, "manage", "get", {"persona_id": "alice"})
    assert result["error"]["code"] == "not_found_or_forbidden"


@pytest.mark.parametrize("action", ["add", "remove"])
async def test_only_admin_can_change_whitelist(whitelist_env, action):
    env = whitelist_env
    event = env.native_event(text=f"/fs whitelist {action} user")
    previous = dict(env.config)
    response = await change(env, action, "user", event=event)
    assert response["error"]["code"] == "admin_only"
    assert env.config == previous


@pytest.mark.parametrize("case", ["outsider", "group", "cron", "api_restricted"])
@pytest.mark.parametrize("command", ["fs", "浮生"])
async def test_restricted_callers_cannot_change_whitelist(whitelist_env, case, command):
    env = whitelist_env
    event = env.native_event(
        "admin",
        admin=case != "outsider",
        group=case == "group",
        text=f"/{command} whitelist add new",
    )
    if case == "cron":
        event.platform_meta.name = "cron"
    elif case == "group":
        env.config["allow_group_chat"] = False
    elif case == "api_restricted":
        event.set_extra("_api_key_allow_admin_role", False)
    previous = dict(env.config)
    response = await change(env, "add", "new", event=event)
    expected = {"group": "private_only", "cron": "invalid_context"}.get(
        case, "forbidden"
    )
    assert response["error"]["code"] == expected
    assert env.config == previous


@pytest.mark.parametrize(
    "text,raw",
    [
        ("/fs whitelist add new", None),
        ("管理白名单", {}),
        ("/fs whitelist remove new", {}),
        ("/fs whitelist add different", {}),
        ("/fs whitelist add new extra", {}),
    ],
)
@pytest.mark.parametrize("command", ["fs", "浮生"])
async def test_proxy_and_mismatched_commands_are_rejected(
    whitelist_env, text, raw, command
):
    env = whitelist_env
    text = text.replace("/fs", f"/{command}", 1)
    event = env.native_event("admin", admin=True, text=text)
    event.message_obj.raw_message = raw
    previous = dict(env.config)
    response = await change(env, "add", "new", event=event)
    assert response["error"]["code"] == "real_command_only"
    assert env.config == previous


@pytest.mark.parametrize("action,user_id", [("add", "new"), ("remove", "user")])
@pytest.mark.parametrize("prefix", ["/", "!"])
async def test_removed_ps_command_cannot_change_whitelist(
    whitelist_env, action, user_id, prefix
):
    env = whitelist_env
    env.core_config["wake_prefix"] = [prefix]
    event = env.native_event(
        "admin", admin=True, text=f"{prefix}ps whitelist {action} {user_id}"
    )
    previous = dict(env.config)
    path = Path(env.config.config_path)
    original_file = await asyncio.to_thread(path.read_bytes)
    response = await change(env, action, user_id, event=event)
    assert response["error"]["code"] == "real_command_only"
    assert env.config == previous
    assert await asyncio.to_thread(path.read_bytes) == original_file


@pytest.mark.parametrize(
    "user_id", [None, False, 123, [], "", " user", "user ", "a b", "a\nb"]
)
async def test_invalid_user_id_is_rejected(whitelist_env, user_id):
    response = await change(whitelist_env, "add", user_id)
    assert response["error"]["code"] == "invalid_payload"


async def test_unknown_fields_and_actions_are_rejected(whitelist_env):
    env = whitelist_env
    event = env.native_event("admin", admin=True, text="/fs whitelist add new")
    response = await env.studio.execute(
        event, "whitelist", "add", {"user_id": "new", "platform_id": "other"}
    )
    assert response["error"]["code"] == "invalid_payload"
    response = await change(env, "clear", "new")
    assert response["error"]["code"] == "invalid_action"
    for family in ("manage", "versions"):
        response = await env.studio.execute(event, family, "add", {"user_id": "new"})
        assert response["error"]["code"] == "invalid_action"


@pytest.mark.parametrize("action,user_id", [("add", "new"), ("remove", "user")])
async def test_save_failure_restores_memory_and_disk(
    whitelist_env, monkeypatch, action, user_id
):
    env = whitelist_env
    previous = dict(env.config)
    path = Path(env.config.config_path)
    original_file = await asyncio.to_thread(path.read_bytes)
    monkeypatch.setattr(
        AstrBotConfig, "_write_config_snapshot", Mock(side_effect=OSError("read-only"))
    )
    response = await change(env, action, user_id)
    assert response["error"]["code"] == "config_save_failed"
    assert not response["error"]["details"]["plugin_written"]
    assert not response["retry_safe"]
    assert env.config == previous
    assert await asyncio.to_thread(path.read_bytes) == original_file


async def test_permission_is_rechecked_inside_lock(whitelist_env):
    env = whitelist_env
    event = env.native_event("admin", admin=True, text="/fs whitelist add new")
    await env.studio.lock.acquire()
    pending = asyncio.create_task(change(env, "add", "new", event=event))
    try:
        await asyncio.sleep(0)
        event.role = "member"
    finally:
        env.studio.lock.release()
    response = await asyncio.wait_for(pending, 5)
    assert response["error"]["code"] == "forbidden"
    assert ("platform", "new") not in env.studio.settings().whitelist


async def test_concurrent_changes_preserve_all_entries(whitelist_env):
    env = whitelist_env
    results = await asyncio.gather(
        *(
            change(env, "add", target)
            for target in ("first", "second", "third", "first")
        )
    )
    assert all(result["ok"] for result in results)
    assert sum(result["data"]["changed"] for result in results) == 3
    assert set(env.studio.settings().whitelist) == {
        ("platform", "user"),
        ("platform", "first"),
        ("platform", "second"),
        ("platform", "third"),
    }


async def test_removal_does_not_revoke_admin_role(whitelist_env):
    env = whitelist_env
    assert (await change(env, "add", "admin"))["ok"]
    assert (await change(env, "remove", "admin"))["ok"]
    response = await env.studio.execute(env.admin, "manage", "list", {})
    assert response["ok"]


async def test_change_audits_target_and_fields(whitelist_env):
    env = whitelist_env
    assert (await change(env, "add", "new"))["ok"]
    entry = env.storage.connection.execute(
        "SELECT entry FROM audit ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    assert json.loads(entry) == {
        "actor": ["platform", "admin"],
        "action": "whitelist.add",
        "target": "new",
        "fields": ["whitelist"],
        "result": "success",
        "reason": None,
    }


async def test_audit_failure_reports_persisted_change(whitelist_env, monkeypatch):
    env = whitelist_env
    monkeypatch.setattr(env.storage, "audit", Mock(side_effect=OSError("audit failed")))
    response = await change(env, "add", "new")
    assert response["error"]["code"] == "partial_failure"
    assert response["error"]["details"]["plugin_written"]
    assert response["error"]["details"]["audit_failed"]
    assert ("platform", "new") in env.studio.settings().whitelist
    saved = json.loads(
        await asyncio.to_thread(
            Path(env.config.config_path).read_text, encoding="utf-8-sig"
        )
    )
    assert saved["whitelist"] == env.config["whitelist"]


@pytest.mark.parametrize("action,user_id", [("add", "new"), ("remove", "user")])
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("command", ["fs", "浮生"])
async def test_native_command_filter_dispatch(
    whitelist_env, action, user_id, group, command, persona_command_filter
):
    env = whitelist_env
    env.config["allow_group_chat"] = group
    plugin = PersonaStudioPlugin(env.context, env.config)
    plugin.studio = env.studio
    event = env.native_event(
        "admin",
        admin=True,
        group=group,
        text=f"/{command} whitelist {action} {user_id}",
    )
    event.message_str = event.message_obj.message_str[1:]
    event.is_at_or_wake_command = True
    command_filter = persona_command_filter
    assert command_filter.filter(event, env.core_config)
    replies = [
        reply
        async for reply in plugin.persona(event, **event.get_extra("parsed_params"))
    ]
    assert len(replies) == 1 and "并保存" in replies[0].chain[0].text
    assert (("platform", user_id) in env.studio.settings().whitelist) == (
        action == "add"
    )


@pytest.mark.parametrize(
    "arguments",
    ["whitelist", "whitelist add", "whitelist add new extra", "whitelist clear new"],
)
async def test_bad_command_syntax_does_not_mutate(whitelist_env, arguments):
    env = whitelist_env
    plugin = PersonaStudioPlugin(env.context, env.config)
    plugin.studio = env.studio
    previous = dict(env.config)
    event = env.native_event("admin", admin=True, text=f"/fs {arguments}")
    replies = [reply async for reply in plugin.persona(event, arguments)]
    assert len(replies) == 1 and "用法" in replies[0].chain[0].text
    assert env.config == previous


@pytest.mark.parametrize("command", ["fs", "浮生"])
async def test_custom_prefix_and_original_arguments(whitelist_env, command):
    env = whitelist_env
    env.core_config["wake_prefix"] = ["!"]
    plugin = PersonaStudioPlugin(env.context, env.config)
    plugin.studio = env.studio
    event = env.native_event(
        "admin", admin=True, text=f"!{command} whitelist add actual-user"
    )
    replies = [
        reply async for reply in plugin.persona(event, "whitelist add substituted-user")
    ]
    assert "actual-user" in replies[0].chain[0].text
    assert ("platform", "actual-user") in env.studio.settings().whitelist
    assert ("platform", "substituted-user") not in env.studio.settings().whitelist
