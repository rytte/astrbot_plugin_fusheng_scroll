import copy
import json
from unittest.mock import AsyncMock

import pytest
from astrbot.core.agent.message import Message, TextPart, dump_messages_with_checkpoints
from astrbot.core.provider.entities import ProviderRequest
from astrbot_plugin_persona_studio.card_service import CardStudio
from astrbot_plugin_persona_studio.cards import export_png, new_card
from astrbot_plugin_persona_studio.domain import (
    Actor,
    StudioError,
    canonical,
    digest,
    identify,
)


@pytest.fixture
async def cards(env):
    env.cards = CardStudio(env.studio)
    env.studio.cards = env.cards
    env.actor = identify(env.event, env.studio.settings())
    return env


async def create(env, **kwargs):
    doc = new_card("爱莉")
    doc["data"].update(
        character_version="1.0",
        description="初始内容",
        creation_date=123,
        modification_date=456,
    )
    return await env.cards.execute(
        env.actor, "create", {"card": doc, **kwargs}, event=env.event
    )


async def change(env, card, action="update", **kwargs):
    return await env.cards.execute(
        env.actor,
        action,
        {"card_id": card["card_id"], "expected_revision": card["revision"], **kwargs},
        event=env.event,
    )


async def test_version_milestones_restore_full_snapshot_and_never_autobump(cards):
    c = await create(cards)
    assert c["sync"]["status"] == "synced" and len(c["snapshots"]) == 1
    c = await change(cards, c, data={"description": "普通编辑"})
    assert (
        len(c["snapshots"]) == 1 and c["document"]["data"]["character_version"] == "1.0"
    )
    c = await change(
        cards,
        c,
        data={"character_version": "2.0", "description": "定版内容"},
        options={"tools": [], "skills": []},
    )
    assert len(c["snapshots"]) == 2
    milestone = c["snapshots"][0]
    snap = cards.cards.storage.snapshot(c["card_id"], milestone["snapshot_id"])[
        "snapshot"
    ]
    assert snap["document"]["data"]["description"] == "定版内容"
    assert snap["options"]["tools"] == []
    c = await change(
        cards, c, data={"description": "未定版草稿"}, options={"tools": None}
    )
    c = await change(cards, c, action="restore", snapshot_id=milestone["snapshot_id"])
    assert len(c["snapshots"]) == 2
    assert c["document"] == snap["document"] and c["options"] == snap["options"]
    listed = await cards.cards.execute(cards.actor, "list", {})
    assert listed["items"][0]["name"] == "爱莉" and "document" not in listed["items"][0]
    core = await cards.studio.current(c["persona_id"])
    assert (
        core["system_prompt"] == c["compiled"]["system_prompt"]
        and core["begin_dialogs"] == []
    )


async def test_legacy_v2_records_and_snapshots_restore_as_v3_without_new_milestone(
    cards,
):
    c = await create(cards, bind=False)
    legacy = copy.deepcopy(c["document"])
    legacy.update(spec="chara_card_v2", spec_version="2.0")
    del legacy["data"]["group_only_greetings"]
    snapshot_id = c["snapshots"][0]["snapshot_id"]
    snapshot = {"document": legacy, "options": c["options"], "asset_id": c["asset_id"]}
    connection = cards.cards.storage.connection
    with connection:
        connection.execute(
            "UPDATE cards SET document=? WHERE card_id=?",
            (canonical(legacy), c["card_id"]),
        )
        connection.execute(
            "UPDATE card_snapshots SET snapshot=?, checksum=? WHERE snapshot_id=?",
            (canonical(snapshot), digest(snapshot), snapshot_id),
        )
    before = tuple(connection.execute("SELECT * FROM card_snapshots").fetchone())
    c = await cards.cards.execute(cards.actor, "get", {"card_id": c["card_id"]})
    assert c["document"]["spec"] == "chara_card_v3"
    assert c["document"]["data"]["character_version"] == "1.0"
    assert len(c["snapshots"]) == 1
    raw, _, _ = await cards.cards.export(cards.actor, c["card_id"])
    exported = json.loads(raw)
    assert exported["spec"] == "chara_card_v3" and exported["spec_version"] == "3.0"
    c = await change(cards, c, data={"description": "新草稿"})
    stored = json.loads(connection.execute("SELECT document FROM cards").fetchone()[0])
    assert stored["spec"] == "chara_card_v3" and len(c["snapshots"]) == 1
    c = await change(cards, c, action="restore", snapshot_id=snapshot_id)
    assert c["document"]["spec"] == "chara_card_v3"
    assert c["document"]["data"] == {**legacy["data"], "group_only_greetings": []}
    assert len(c["snapshots"]) == 1
    stored = json.loads(connection.execute("SELECT document FROM cards").fetchone()[0])
    assert stored == c["document"]
    assert (
        tuple(connection.execute("SELECT * FROM card_snapshots").fetchone()) == before
    )
    assert (
        cards.cards.storage.snapshot(c["card_id"], snapshot_id)["snapshot"] == snapshot
    )


async def test_revision_locks_ownership_and_revocation(cards):
    c = await create(cards)
    c2 = await change(cards, c, data={"description": "编辑"})
    with pytest.raises(StudioError, match="已被修改"):
        await change(cards, c, data={"description": "过期"})
    other = Actor("platform", "other", False)
    with pytest.raises(StudioError):
        await cards.cards.execute(other, "get", {"card_id": c["card_id"]})
    web = CardStudio.web_actor("dashboard-admin")
    c2 = await cards.cards.execute(
        web,
        "lock",
        {"card_id": c["card_id"], "expected_revision": c2["revision"], "locked": True},
    )
    with pytest.raises(StudioError) as exc:
        await change(cards, c2, data={"description": "锁定"})
    assert exc.value.code == "card_locked"
    with pytest.raises(StudioError) as exc:
        await change(cards, c2, action="lock", locked=False)
    assert exc.value.code == "manual_only"
    cards.config["whitelist"] = []
    with pytest.raises(StudioError) as exc:
        await cards.cards.execute(
            cards.actor, "get", {"card_id": c["card_id"]}, event=cards.event
        )
    assert exc.value.code == "forbidden"


async def test_external_core_changes_require_current_overwrite_token(cards):
    c = await create(cards)
    await cards.manager.update_persona(
        c["persona_id"], system_prompt="外部编辑", tools=["echo"]
    )
    c = await change(cards, c, data={"description": "卡片编辑"})
    assert (
        c["sync"]["status"] == "conflict"
        and c["document"]["data"]["description"] == "卡片编辑"
    )
    token = c["sync"]["expected_core_token"]
    await cards.manager.update_persona(c["persona_id"], system_prompt="外部再次编辑")
    with pytest.raises(StudioError) as exc:
        await change(cards, c, action="sync", overwrite=True, expected_core_token=token)
    assert exc.value.code == "revision_conflict"
    c = await cards.cards.execute(cards.actor, "get", {"card_id": c["card_id"]})
    c = await change(
        cards,
        c,
        action="sync",
        overwrite=True,
        expected_core_token=c["sync"]["expected_core_token"],
    )
    assert c["sync"]["status"] == "synced"
    assert (await cards.studio.current(c["persona_id"]))["tools"] == ["echo"]


async def test_core_failure_retains_card_and_recovers_without_snapshot(
    cards, monkeypatch
):
    c = await create(cards)
    original = cards.manager.update_persona
    monkeypatch.setattr(
        cards.manager,
        "update_persona",
        AsyncMock(side_effect=RuntimeError("disk error")),
    )
    c = await change(cards, c, data={"description": "已保存在卡片"})
    assert c["sync"]["status"] == "failed"
    assert c["document"]["data"]["description"] == "已保存在卡片"
    monkeypatch.setattr(cards.manager, "update_persona", original)
    c = await change(cards, c, action="sync")
    assert c["sync"]["status"] == "synced" and len(c["snapshots"]) == 1


async def test_deleted_recreated_core_is_never_adopted(cards):
    c = await create(cards)
    await cards.manager.delete_persona(c["persona_id"])
    c = await change(cards, c, action="sync")
    assert c["sync"]["status"] == "missing"
    await cards.manager.create_persona(c["persona_id"], system_prompt="另一个人")
    c = await change(
        cards,
        c,
        action="sync",
        overwrite=True,
        expected_core_token=c["sync"]["expected_core_token"],
    )
    assert c["sync"]["status"] == "conflict"
    assert (await cards.studio.current(c["persona_id"]))["system_prompt"] == "另一个人"


async def test_import_deduplicates_submission_preserves_dates_and_restores_image(cards):
    document = new_card("导入")
    document["data"].update(
        creation_date=100, modification_date=200, character_version="v1"
    )
    image = export_png(document)
    preview = await cards.cards.prepare_import(cards.actor, image)
    first = await cards.cards.execute(
        cards.actor, "create", {"import_id": preview["import_id"]}
    )
    repeat = await cards.cards.execute(
        cards.actor, "create", {"import_id": preview["import_id"]}
    )
    assert first["card_id"] == repeat["card_id"]
    assert first["document"] == document
    with pytest.raises(StudioError):
        await cards.cards.execute(
            Actor("p", "other", False), "create", {"import_id": preview["import_id"]}
        )
    raw, _, _ = await cards.cards.export(cards.actor, first["card_id"], png=True)
    from astrbot_plugin_persona_studio.cards import import_card

    assert import_card(raw)[0] == document
    second = await create(cards, bind=False)
    second = await change(cards, second, import_id=preview["import_id"])
    assert second["document"] == document and second["asset_id"] == first["asset_id"]
    initial = second["snapshots"][-1]
    second = await change(
        cards, second, action="restore", snapshot_id=initial["snapshot_id"]
    )
    assert second["asset_id"] is None


async def test_archive_keeps_runtime_copy_and_restore_unarchives(cards):
    c = await create(cards)
    c = await change(cards, c, action="archive")
    assert c["deleted"] and cards.cards.storage.by_persona(c["persona_id"]) is None
    assert await cards.studio.current(c["persona_id"])
    c = await change(
        cards, c, action="restore", snapshot_id=c["snapshots"][0]["snapshot_id"]
    )
    assert not c["deleted"] and c["sync"]["status"] == "synced"


async def test_core_lock_blocks_card_update_and_legacy_writes(cards):
    c = await create(cards)
    legacy = await cards.studio.execute(
        cards.event,
        "manage",
        "update",
        {"persona_id": c["persona_id"], "system_prompt": "绕过"},
    )
    assert legacy["error"]["code"] == "card_managed"
    event = cards.native_event(text=f"/ps lock {c['persona_id']}")
    locked = await cards.studio.execute(
        event, "protect", "lock", {"persona_id": c["persona_id"]}
    )
    assert locked["ok"], locked
    with pytest.raises(StudioError) as exc:
        await change(cards, c, data={"description": "绕过锁定"})
    assert exc.value.code == "persona_locked"
    assert cards.cards.storage.get(c["card_id"])["revision"] == c["revision"]


async def test_request_macros_examples_greeting_are_temporary_and_idempotent(cards):
    c = await create(cards)
    c = await change(
        cards,
        c,
        data={
            "first_mes": "你好 {{user}}",
            "alternate_greetings": ["备用"],
            "post_history_instructions": "{{char}} 保持角色",
            "mes_example": "<START>\n{{user}}: 示例\n{{char}}: 回答",
        },
    )
    cards.event.set_extra("_trace_persona_id", c["persona_id"])
    req = ProviderRequest(
        prompt="真实提问",
        system_prompt="# Persona Instructions\n" + c["compiled"]["system_prompt"],
        contexts=[],
    )
    await cards.cards.apply_request(cards.event, req)
    assert req.prompt == "真实提问" and "爱莉" in req.system_prompt
    assert [m["content"] for m in req.contexts] == ["示例", "回答", "你好 user"]
    assert all(m["_no_save"] for m in req.contexts)
    assert req.extra_user_content_parts[0]._no_save
    before = copy.deepcopy(req)
    await cards.cards.apply_request(cards.event, req)
    assert req.contexts == before.contexts and len(req.extra_user_content_parts) == 1
    dumped = dump_messages_with_checkpoints(
        [
            Message(
                role="user",
                content=[TextPart(text=req.prompt), *req.extra_user_content_parts],
            )
        ]
    )
    assert dumped[0]["content"] == [{"type": "text", "text": "真实提问"}]
    req2 = ProviderRequest(
        prompt="继续",
        system_prompt=c["compiled"]["system_prompt"],
        contexts=[{"role": "user", "content": "之前"}],
    )
    original = copy.deepcopy(req2.contexts)
    await cards.cards.apply_request(cards.event, req2)
    assert len(req2.contexts) == 3 and req2.contexts[-1] == original[0]
    await cards.manager.update_persona(c["persona_id"], begin_dialogs=["外部", "变更"])
    req3 = ProviderRequest(system_prompt=c["compiled"]["system_prompt"], contexts=[])
    await cards.cards.apply_request(cards.event, req3)
    assert req3.contexts == []


async def test_request_injection_is_excluded_by_actual_core_history_writer(cards):
    from types import SimpleNamespace

    from astrbot.core.agent.message import bind_checkpoint_messages
    from astrbot.core.pipeline.process_stage.method.agent_sub_stages.internal import (
        InternalAgentSubStage,
    )
    from astrbot.core.provider.entities import LLMResponse

    c = await create(cards)
    c = await change(
        cards,
        c,
        data={
            "first_mes": "临时开场",
            "mes_example": "{{user}}: 示例\n{{char}}: 示例答",
            "post_history_instructions": "临时指令",
        },
    )
    cards.event.set_extra("_trace_persona_id", c["persona_id"])
    conv_mgr = cards.context.conversation_manager
    cid = await conv_mgr.new_conversation(cards.event.unified_msg_origin)
    conversation = await conv_mgr.get_conversation(cards.event.unified_msg_origin, cid)
    req = ProviderRequest(
        prompt="真实输入",
        system_prompt=c["compiled"]["system_prompt"],
        contexts=[],
        conversation=conversation,
    )
    await cards.cards.apply_request(cards.event, req)
    messages = [
        Message(role="system", content=req.system_prompt),
        *bind_checkpoint_messages(req.contexts),
        Message(
            role="user",
            content=[TextPart(text=req.prompt), *req.extra_user_content_parts],
        ),
        Message(role="assistant", content="真实回答"),
    ]
    stage = SimpleNamespace(conv_manager=conv_mgr)
    await InternalAgentSubStage._save_to_history(
        stage,
        cards.event,
        req,
        LLMResponse(role="assistant", completion_text="真实回答"),
        messages,
        None,
    )
    saved = await conv_mgr.get_conversation(cards.event.unified_msg_origin, cid)
    import json

    history = json.loads(saved.history)
    assert [m["role"] for m in history] == ["user", "assistant"]
    decoded = json.dumps(history, ensure_ascii=False)
    assert "临时" not in decoded and "示例" not in decoded
    assert "真实输入" in decoded and "真实回答" in decoded


async def test_revoked_actor_cannot_create_during_core_quota_await(cards, monkeypatch):
    original = cards.studio.check_quota

    async def revoke(actor, settings):
        await original(actor, settings)
        cards.config["whitelist"] = []

    monkeypatch.setattr(cards.studio, "check_quota", revoke)
    with pytest.raises(StudioError):
        await create(cards)
    assert cards.cards.storage.list(CardStudio.web_actor("admin")) == []


async def test_legacy_backup_restore_cannot_override_card_runtime(cards):
    c = await create(cards)
    current = await cards.studio.current(c["persona_id"])
    from astrbot_plugin_persona_studio.domain import record_ref

    record = cards.storage.find(c["persona_id"], record_ref(current))
    backup = cards.storage.backup(record, current, "test")
    info = await cards.studio.execute(
        cards.event, "versions", "get", {"backup_id": backup}
    )
    result = await cards.studio.execute(
        cards.event,
        "versions",
        "restore",
        {"backup_id": backup, "expected_revision": info["data"]["expected_revision"]},
    )
    assert result["error"]["code"] == "card_managed"


async def test_first_core_creation_failure_can_retry_without_reimport(
    cards, monkeypatch
):
    original = cards.manager.create_persona
    monkeypatch.setattr(
        cards.manager, "create_persona", AsyncMock(side_effect=RuntimeError("failure"))
    )
    c = await create(cards)
    assert c["sync"]["status"] == "failed" and c["persona_ref"] is None
    assert len(c["snapshots"]) == 1
    monkeypatch.setattr(cards.manager, "create_persona", original)
    c = await change(cards, c, action="sync")
    assert c["sync"]["status"] == "synced" and c["persona_ref"] is not None
    assert len(cards.cards.storage.list(cards.actor)) == 1
    assert len(c["snapshots"]) == 1
