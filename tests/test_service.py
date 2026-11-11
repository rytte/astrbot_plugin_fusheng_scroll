import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_persona_studio.domain import Settings, StudioError
from astrbot_plugin_persona_studio.storage import Storage


async def run(env, action, payload, *, actor=None, family="manage"):
    return await env.studio.execute(actor or env.event, family, action, payload)


async def set_lock(env, locked=True, *, actor=None):
    event = actor or env.native_event(
        text=f"/ps {'lock' if locked else 'unlock'} alice"
    )
    action = "lock" if locked else "unlock"
    return await run(
        env, action, {"persona_id": "alice"}, actor=event, family="protect"
    )


@pytest.mark.parametrize("family", ["manage", "versions"])
@pytest.mark.parametrize(
    "options",
    [
        {"format": "md"},
        {"format": "TEXT"},
        {"format": None},
        {"format": False},
        {"format": {}},
        {"content": "both"},
        {"content": None},
        {"content": []},
        {"include_md": True},
        {"include_md": False},
        {"include_md": True, "format": "markdown"},
    ],
)
async def test_get_rejects_invalid_or_removed_view_options(created, family, options):
    target = {"persona_id": "alice"} if family == "manage" else {"backup_id": "unused"}
    response = await run(created, "get", {**target, **options}, family=family)
    assert response["error"]["code"] == "invalid_payload"
    if "include_md" in options:
        assert "include_md 已移除" in response["error"]["message"]
    created.event.send.assert_not_called()


@pytest.mark.parametrize("action", ["list", "create", "update", "delete", "switch"])
async def test_view_options_are_not_allowed_for_other_actions(created, action):
    payload = {"format": "markdown"}
    if action != "list":
        payload["persona_id"] = "alice"
    if action == "create":
        payload["system_prompt"] = "prompt"
    if action == "update":
        payload["system_prompt"] = "new"
    response = await run(created, action, payload)
    assert response["error"]["code"] == "invalid_payload"


@pytest.mark.parametrize("action", ["list", "restore", "delete"])
async def test_view_options_are_not_allowed_for_version_writes(created, action):
    payload = {"format": "markdown"}
    if action == "list":
        payload["persona_id"] = "alice"
    else:
        payload["backup_id"] = "unused"
    if action == "restore":
        payload["expected_revision"] = "unused"
    response = await run(created, action, payload, family="versions")
    assert response["error"]["code"] == "invalid_payload"


async def first_backup(env):
    changed = await run(
        env, "update", {"persona_id": "alice", "system_prompt": "版本2"}
    )
    assert changed["ok"], changed
    return changed["data"]["backup_id"]


@pytest.mark.parametrize(
    "event_kwargs,code",
    [
        ({"user": "outsider"}, "forbidden"),
        ({"platform": "other"}, "forbidden"),
        ({"group": True, "admin": True}, "private_only"),
    ],
)
async def test_identity_gate(env, event_kwargs, code):
    if event_kwargs.get("group"):
        env.config["allow_group_chat"] = False
    response = await run(env, "list", {}, actor=env.native_event(**event_kwargs))
    assert response["error"]["code"] == code


async def test_api_key_restriction(env):
    env.admin.set_extra("_api_key_allow_admin_role", False)
    assert (await run(env, "list", {}, actor=env.admin))["error"]["code"] == "forbidden"


async def test_visibility_and_no_claiming(created):
    env = created
    await env.manager.create_persona("external", "外部人格")
    own = await run(env, "list", {"limit": 1})
    assert own["data"]["total"] == 1
    assert own["data"]["items"][0]["persona_id"] == "alice"
    # Current core persists its built-in default persona during initialization.
    visible = (await run(env, "list", {}, actor=env.admin))["data"]
    assert {p["persona_id"] for p in visible["items"]} == {
        "alice",
        "external",
        "default",
    }
    for action in ("get", "update", "delete", "switch"):
        payload = {"persona_id": "external"}
        if action == "update":
            payload["system_prompt"] = "bad"
        assert (await run(env, action, payload))["error"][
            "code"
        ] == "not_found_or_forbidden"


async def test_revocation_immediate(created):
    created.config["whitelist"] = []
    assert (await run(created, "list", {}))["error"]["code"] == "forbidden"
    assert (await set_lock(created))["error"]["code"] == "forbidden"
    assert await created.studio.current("alice") is not None


@pytest.mark.parametrize("persona_id", ["default", "[%None]", "_chatui_default_"])
async def test_reserved_ids(env, persona_id):
    result = await run(
        env, "create", {"persona_id": persona_id, "system_prompt": "prompt"}
    )
    assert result["error"]["code"] == "reserved_id"


@pytest.mark.parametrize(
    "payload",
    [
        {"persona_id": "alice", "owner": "attacker"},
        {"persona_id": "alice", "tools": "echo"},
        {"persona_id": "alice", "begin_dialogs": ["odd"]},
        {"persona_id": "alice", "system_prompt": None},
        {"persona_id": "alice", "tools": ["missing"]},
    ],
)
async def test_bad_updates(created, payload):
    assert not (await run(created, "update", payload))["ok"]
    assert (await created.studio.current("alice"))["system_prompt"] == "版本1"


@pytest.mark.parametrize("name", ["persona_manage", "persona_versions"])
async def test_removed_tools_are_rejected_without_capability_fallback(created, name):
    before = await created.studio.current("alice")
    result = await run(created, "update", {"persona_id": "alice", "tools": [name]})
    assert result["error"]["code"] == "invalid_capability"
    assert name in result["error"]["message"]
    assert not result["error"]["details"]["mutation_started"]
    assert await created.studio.current("alice") == before


@pytest.mark.parametrize(
    ("action", "required"),
    [
        ("list", ["persona_id"]),
        ("get", ["backup_id"]),
        ("restore", ["backup_id", "expected_revision"]),
        ("delete", ["backup_id"]),
    ],
)
async def test_missing_version_fields_return_contract_without_writing(
    created, action, required
):
    response = await run(created, action, {}, family="versions")
    assert response["error"]["code"] == "invalid_payload"
    details = response["error"]["details"]
    assert details["required_fields"] == details["missing_fields"] == required
    assert details["unexpected_fields"] == []
    assert not details["core_written"] and not details["plugin_written"]
    assert not details["mutation_started"]


async def test_capability_semantics_and_omission(created):
    assert (
        await run(
            created, "update", {"persona_id": "alice", "tools": None, "skills": None}
        )
    )["ok"]
    assert (
        await run(created, "update", {"persona_id": "alice", "system_prompt": "next"})
    )["ok"]
    current = await created.studio.current("alice")
    assert current["tools"] is None and current["skills"] is None
    assert current["begin_dialogs"] == ["你好", "你好呀"]
    assert (
        await run(
            created, "update", {"persona_id": "alice", "tools": [], "begin_dialogs": []}
        )
    )["ok"]
    assert (await created.studio.current("alice"))["tools"] == []
    assert (
        await run(created, "create", {"persona_id": "new", "system_prompt": "new"})
    )["data"]["persona"]["tools"] is None


async def test_lock_blocks_admin_and_versions(created):
    backup_id = await first_backup(created)
    assert (await set_lock(created))["ok"]
    for actor in (created.event, created.admin):
        for action in ("update", "delete"):
            payload = {
                "persona_id": "alice",
                **({"tools": []} if action == "update" else {}),
            }
            assert (await run(created, action, payload, actor=actor))["error"][
                "code"
            ] == "persona_locked"
        for action in ("restore", "delete"):
            payload = {
                "backup_id": backup_id,
                **({"expected_revision": "anything"} if action == "restore" else {}),
            }
            assert (
                await run(created, action, payload, actor=actor, family="versions")
            )["error"]["code"] == "persona_locked"
        assert (await run(created, "get", {"persona_id": "alice"}, actor=actor))["ok"]


async def test_white_user_can_unlock_admin_lock(created):
    event = created.native_event("admin", admin=True, text="/ps lock alice")
    assert (await set_lock(created, actor=event))["ok"]
    assert (await set_lock(created, False))["data"]["locked"] is False


async def test_proxy_or_non_command_cannot_unlock(created):
    assert (await set_lock(created))["ok"]
    proxy = created.native_event(text="/ps unlock alice")
    proxy.message_obj.raw_message = None
    assert (await set_lock(created, False, actor=proxy))["error"][
        "code"
    ] == "real_command_only"
    assert (await set_lock(created, False, actor=created.event))["error"][
        "code"
    ] == "real_command_only"


async def test_old_command_cannot_unlock(created):
    assert (await set_lock(created))["ok"]
    event = created.native_event(text="/persona unlock alice")
    response = await set_lock(created, False, actor=event)
    assert response["error"]["code"] == "real_command_only"
    assert created.storage.records("alice")[0]["locked"] == 1


async def test_multi_step_restore_and_undo_restore(created):
    first = await first_backup(created)
    assert (
        await run(created, "update", {"persona_id": "alice", "system_prompt": "版本3"})
    )["ok"]
    detail = await run(created, "get", {"backup_id": first}, family="versions")
    restored = await run(
        created,
        "restore",
        {"backup_id": first, "expected_revision": detail["data"]["expected_revision"]},
        family="versions",
    )
    assert restored["ok"], restored
    assert restored["data"]["revision"] == 4
    assert restored["data"]["persona"]["system_prompt"] == "版本1"
    undo = await run(
        created,
        "restore",
        {
            "backup_id": restored["data"]["backup_id"],
            "expected_revision": restored["data"]["expected_revision"],
        },
        family="versions",
    )
    assert undo["data"]["persona"]["system_prompt"] == "版本3"


async def test_webui_change_invalidates_revision(created):
    backup_id = await first_backup(created)
    listed = await run(created, "get", {"backup_id": backup_id}, family="versions")
    await created.manager.update_persona("alice", system_prompt="WebUI change")
    restored = await run(
        created,
        "restore",
        {
            "backup_id": backup_id,
            "expected_revision": listed["data"]["expected_revision"],
        },
        family="versions",
    )
    assert restored["error"]["code"] == "revision_conflict"
    assert (await created.studio.current("alice"))["system_prompt"] == "WebUI change"


async def test_delete_restore_retains_ownership_and_chain(created):
    original = (await created.studio.current("alice"))["id"]
    deleted = await run(created, "delete", {"persona_id": "alice"})
    assert deleted["ok"], deleted
    backup_id = deleted["data"]["backup_id"]
    detail = await run(created, "get", {"backup_id": backup_id}, family="versions")
    restored = await run(
        created,
        "restore",
        {
            "backup_id": backup_id,
            "expected_revision": detail["data"]["expected_revision"],
        },
        family="versions",
    )
    assert restored["ok"], restored
    assert restored["data"]["owner"] == ["platform", "user"]
    assert restored["data"]["chain_id"] == deleted["data"]["chain_id"]
    assert (
        restored["data"]["persona"]["created_at"]
        != detail["data"]["persona"]["created_at"]
        or restored["data"]["persona"]["id"] != original
    )


async def test_name_reuse_does_not_inherit_ownership(created):
    backup_id = await first_backup(created)
    await created.manager.delete_persona("alice")
    await created.manager.create_persona("alice", "new record")
    assert (await run(created, "get", {"persona_id": "alice"}))["error"][
        "code"
    ] == "not_found_or_forbidden"
    detail = await run(created, "get", {"backup_id": backup_id}, family="versions")
    assert detail["data"]["record_conflict"]
    restored = await run(
        created,
        "restore",
        {
            "backup_id": backup_id,
            "expected_revision": detail["data"]["expected_revision"],
        },
        family="versions",
    )
    assert restored["error"]["code"] == "record_conflict"


async def test_delete_backup_does_not_change_persona(created):
    backup_id = await first_backup(created)
    before = await created.studio.current("alice")
    deleted = await run(created, "delete", {"backup_id": backup_id}, family="versions")
    assert deleted["data"]["undoable"] is False
    assert await created.studio.current("alice") == before
    assert (await run(created, "get", {"backup_id": backup_id}, family="versions"))[
        "error"
    ]["code"] == "not_found_or_forbidden"


async def test_backup_failure_prevents_core_write(created, monkeypatch):
    monkeypatch.setattr(
        created.storage, "backup", lambda *args: (_ for _ in ()).throw(OSError("disk"))
    )
    response = await run(
        created, "update", {"persona_id": "alice", "system_prompt": "bad"}
    )
    assert not response["ok"] and not response["error"]["details"]["mutation_started"]
    assert (await created.studio.current("alice"))["system_prompt"] == "版本1"


async def test_metadata_failure_never_reports_success(created, monkeypatch):
    monkeypatch.setattr(
        created.storage,
        "save_record",
        lambda *args: (_ for _ in ()).throw(OSError("disk")),
    )
    response = await run(
        created, "update", {"persona_id": "alice", "system_prompt": "written"}
    )
    assert response["error"]["code"] == "partial_failure"
    assert response["error"]["details"]["core_written"] and not response["retry_safe"]
    assert (await created.studio.current("alice"))["system_prompt"] == "written"


async def test_corrupt_metadata_fails_closed(created):
    with created.storage.connection:
        created.storage.connection.execute("UPDATE records SET owner='broken'")
    assert (await run(created, "get", {"persona_id": "alice"}, actor=created.admin))[
        "error"
    ]["code"] == "storage_error"


async def test_corrupt_backup_rejected(created):
    backup_id = await first_backup(created)
    with created.storage.connection:
        created.storage.connection.execute(
            "UPDATE backups SET snapshot='{}' WHERE backup_id=?", (backup_id,)
        )
    assert (await run(created, "get", {"backup_id": backup_id}, family="versions"))[
        "error"
    ]["code"] == "backup_corrupt"


async def test_quota_and_length_limits(created):
    created.config["max_personas_per_user"] = 1
    assert (
        await run(
            created, "create", {"persona_id": "second", "system_prompt": "prompt"}
        )
    )["error"]["code"] == "limit_exceeded"
    created.config["max_prompt_chars"] = 3
    assert (
        await run(
            created, "update", {"persona_id": "alice", "system_prompt": "long prompt"}
        )
    )["error"]["code"] == "limit_exceeded"


@pytest.mark.parametrize(
    "reference_type",
    ["default", "subagent", "subagent_trimmed", "conversation", "session_rule", "cron"],
)
async def test_in_use_persona_cannot_be_deleted(created, reference_type):
    if reference_type == "default":
        created.core_config["provider_settings"]["default_personality"] = "alice"
    elif reference_type.startswith("subagent"):
        created.core_config["subagent_orchestrator"] = {
            "agents": [
                {
                    "name": "helper",
                    "persona_id": " alice "
                    if reference_type == "subagent_trimmed"
                    else "alice",
                    "enabled": False,
                }
            ]
        }
    elif reference_type == "conversation":
        await created.db.create_conversation(
            "other:FriendMessage:private", "other", persona_id="alice"
        )
    elif reference_type == "session_rule":
        await created.db.insert_preference_or_update(
            "umo",
            "private-session",
            "session_service_config",
            {"val": {"persona_id": "alice"}},
        )
    else:
        await created.db.create_cron_job(
            job_id="job",
            name="job",
            job_type="active_agent",
            cron_expression="* * * * *",
            payload={"persona_id": "alice"},
        )
    denied = await run(created, "delete", {"persona_id": "alice"})
    assert denied["error"]["code"] == "persona_in_use", denied
    assert "references" not in denied["error"]["details"]
    admin = await run(created, "delete", {"persona_id": "alice"}, actor=created.admin)
    assert admin["error"]["details"]["references"]
    assert await created.studio.current("alice") is not None


async def test_subagent_numeric_persona_reference_blocks_delete(created):
    assert (
        await run(created, "create", {"persona_id": "123", "system_prompt": "prompt"})
    )["ok"]
    created.core_config["subagent_orchestrator"] = {
        "agents": [{"name": "helper", "persona_id": 123}]
    }
    result = await run(created, "delete", {"persona_id": "123"})
    assert result["error"]["code"] == "persona_in_use"
    assert await created.studio.current("123") is not None


async def test_switch_preserves_history_and_lock(created):
    await set_lock(created)
    conversation_id = await created.context.conversation_manager.new_conversation(
        created.event.unified_msg_origin,
        content=[{"role": "user", "content": "history"}],
    )
    before = await created.db.get_conversation_by_id(conversation_id)
    result = await run(created, "switch", {"persona_id": "alice"})
    assert result["ok"], result
    after = await created.db.get_conversation_by_id(conversation_id)
    assert after.persona_id == "alice" and after.content == before.content
    assert (
        result["data"]["effective"] == "next_conversation_request"
        and result["data"]["warning"]
    )


async def test_switch_no_conversation_or_forced_rule(created, monkeypatch):
    assert (await run(created, "switch", {"persona_id": "alice"}))["error"][
        "code"
    ] == "no_current_conversation"
    monkeypatch.setattr(
        "astrbot_plugin_persona_studio.service.sp.get_async",
        AsyncMock(return_value={"persona_id": "forced"}),
    )
    assert (await run(created, "switch", {"persona_id": "alice"}))["error"][
        "code"
    ] == "forced_persona"


async def test_retention_deferred_while_locked(created):
    await first_backup(created)
    await set_lock(created)
    created.config["backup_keep_count"] = 1
    record = created.storage.records("alice")[0]
    created.storage.backup(record, await created.studio.current("alice"), "test")
    await created.studio.maintenance()
    assert len(created.storage.backups(record["chain_id"])) == 2
    await set_lock(created, False)
    assert len(created.storage.backups(record["chain_id"])) == 1


async def test_plugin_lock_serializes_plugin_calls_but_not_webui(created):
    entered = asyncio.Event()

    async def writer():
        entered.set()
        return await run(
            created, "update", {"persona_id": "alice", "system_prompt": "plugin"}
        )

    async with created.studio.lock:
        task = asyncio.create_task(writer())
        await entered.wait()
        assert not task.done()
        await created.manager.update_persona("alice", system_prompt="WebUI")
        assert (await created.studio.current("alice"))["system_prompt"] == "WebUI"
    assert (await task)["ok"]
    assert (await created.studio.current("alice"))["system_prompt"] == "plugin"


async def test_audit_has_no_prompt(created):
    await first_backup(created)
    entries = [
        json.loads(row[0])
        for row in created.storage.connection.execute("SELECT entry FROM audit")
    ]
    assert entries and all(
        "版本1" not in json.dumps(entry, ensure_ascii=False) for entry in entries
    )


@pytest.mark.parametrize(
    "config",
    [
        {"old_whitelist": []},
        {"whitelist": ["user"]},
        {"render_width": 0},
        {"render_page_height": 599},
        {"whitelist": [{"platform_id": "p", "user_id": "u", "__template_key": "old"}]},
    ],
)
def test_invalid_config_fails_fast(config):
    with pytest.raises(StudioError):
        Settings.parse(config)


def test_missing_initialized_database_is_not_recreated(tmp_path):
    path = tmp_path / "studio.sqlite3"
    storage = Storage(path)
    storage.close()
    path.unlink()
    with pytest.raises(StudioError, match="拒绝重建"):
        Storage(path)
    assert not path.exists()


async def test_incomplete_snapshot_never_defaults_to_all_tools(created):
    from astrbot_plugin_persona_studio.domain import canonical, digest

    backup_id = await first_backup(created)
    row = created.storage.connection.execute(
        "SELECT snapshot FROM backups WHERE backup_id=?", (backup_id,)
    ).fetchone()
    snapshot = json.loads(row[0])
    del snapshot["persona"]["tools"]
    with created.storage.connection:
        created.storage.connection.execute(
            "UPDATE backups SET snapshot=?, checksum=? WHERE backup_id=?",
            (canonical(snapshot), digest(snapshot), backup_id),
        )
    response = await run(created, "get", {"backup_id": backup_id}, family="versions")
    assert response["error"]["code"] == "backup_corrupt"
