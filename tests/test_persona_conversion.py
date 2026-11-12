import asyncio
import copy
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from astrbot.core.provider.entities import ProviderRequest
from astrbot_plugin_fusheng_scroll.card_service import CardStudio
from astrbot_plugin_fusheng_scroll.cards import (
    PERSONA_EXTENSION,
    compile_card,
    from_persona,
    new_card,
)
from astrbot_plugin_fusheng_scroll.domain import (
    StudioError,
    identify,
    record_ref,
)


@pytest.fixture
async def conversion(env):
    env.cards = CardStudio(env.studio)
    env.studio.cards = env.cards
    env.actor = identify(env.event, env.studio.settings())
    env.web_actor = CardStudio.web_actor("dashboard-admin")
    return env


async def legacy(env, persona_id="原有人格", *, owned=False, **patch):
    await env.manager.create_persona(
        persona_id,
        system_prompt=patch.pop("system_prompt", "原提示词 {{user}} 与 {{char}}"),
        begin_dialogs=patch.pop(
            "begin_dialogs", ["  原提问\n{{char}}: 文本内的标记  ", "原回答"]
        ),
        tools=patch.pop("tools", ["echo"]),
        skills=patch.pop("skills", []),
        custom_error_message=patch.pop("custom_error_message", "原错误回复"),
        **patch,
    )
    persona = await env.studio.current(persona_id)
    if owned:
        env.storage.register(persona_id, record_ref(persona), env.actor.owner)
    return persona


async def listing(env, actor=None):
    return await env.cards.execute(actor or env.web_actor, "list", {})


async def get_converted(env, persona_id="原有人格", actor=None):
    actor = actor or env.web_actor
    page = await listing(env, actor)
    item = next(c for c in page["items"] if c["persona_id"] == persona_id)
    return await env.cards.execute(actor, "get", {"card_id": item["card_id"]})


async def mutate(env, card, action="update", **payload):
    return await env.cards.execute(
        env.web_actor,
        action,
        {"card_id": card["card_id"], "expected_revision": card["revision"], **payload},
    )


@pytest.mark.parametrize("prompt", ["", "原文 {{char}} 与 {{user}}"])
async def test_worldbook_preserves_literal_native_prompt_and_ignores_presets(
    conversion, prompt
):
    env = conversion
    await legacy(env, system_prompt=prompt, begin_dialogs=["白塔", "预设回答"])
    card = await get_converted(env)
    card = await mutate(
        env,
        card,
        data={
            "character_book": {
                "entries": [
                    {"constant": True, "content": "{{char}} 与 {{user}} 的本轮背景"},
                    {"keys": ["白塔"], "content": "预设不应触发此条"},
                ]
            }
        },
    )
    persona = env.manager.get_persona_v3_by_id(card["persona_id"])
    req = ProviderRequest(
        prompt="你好",
        system_prompt="外部系统\n" + prompt,
        contexts=copy.deepcopy(persona["_begin_dialogs_processed"]),
    )
    env.event.set_extra("_trace_persona_id", card["persona_id"])
    await env.cards.apply_request(env.event, req)
    assert "原有人格 与 user 的本轮背景" in req.system_prompt
    assert "预设不应触发此条" not in req.system_prompt
    assert req.system_prompt.startswith("外部系统\n" + prompt)
    assert (await env.studio.current(card["persona_id"]))["system_prompt"] == prompt


@pytest.mark.parametrize(
    "dialogs",
    [None, [], ["单条"], ["  首尾空白 \n{{char}}: 内嵌标记", "<START>\n原回答\n"]],
)
def test_conversion_preserves_native_prompt_dialogs_and_source(dialogs):
    persona = {
        "persona_id": "原有人格",
        "system_prompt": "  原文 {{char}} {{user}} {{original}}\n",
        "begin_dialogs": dialogs,
        "created_at": "2026-01-01T01:02:03",
        "updated_at": "2026-01-02T01:02:03",
        "folder_id": "folder",
        "sort_order": 7,
    }
    before = copy.deepcopy(persona)
    document = from_persona(persona)
    data = document["data"]
    assert document["spec"] == "chara_card_v3" and document["spec_version"] == "3.0"
    assert data["name"] == persona["persona_id"] and data["character_version"] == ""
    assert data["description"] == data["personality"] == data["scenario"] == ""
    assert "creation_date" not in data and "modification_date" not in data
    assert data["extensions"][PERSONA_EXTENSION]["persona_import"]["persona"] == persona
    compiled = compile_card(document)
    assert compiled["system_prompt"] == persona["system_prompt"]
    assert compiled["begin_dialogs"] == (dialogs or []) and compiled["examples"] == []
    assert persona == before
    if dialogs:
        document["data"]["mes_example"] = "{{user}}: 新示例\n{{char}}: 新回答"
        edited = compile_card(document)
        assert edited["begin_dialogs"] == []
        assert [m["content"] for m in edited["examples"][0]] == ["新示例", "新回答"]


async def test_listing_converts_existing_records_once_without_core_writes(
    conversion, monkeypatch
):
    env = conversion
    persona = await legacy(env)
    conversations = env.context.conversation_manager
    cid = await conversations.new_conversation(
        env.event.unified_msg_origin,
        persona_id=persona["persona_id"],
        content=[{"role": "user", "content": "原会话历史"}],
    )
    conversation = await conversations.get_conversation(
        env.event.unified_msg_origin, cid
    )
    before = [p.model_dump(mode="json") for p in await env.manager.get_all_personas()]
    update = AsyncMock(wraps=env.manager.update_persona)
    create = AsyncMock(wraps=env.manager.create_persona)
    monkeypatch.setattr(env.manager, "update_persona", update)
    monkeypatch.setattr(env.manager, "create_persona", create)
    first, second = await asyncio.gather(listing(env), listing(env))
    assert {c["persona_id"] for c in first["items"]} == {"default", "原有人格"}
    assert {c["card_id"] for c in first["items"]} == {
        c["card_id"] for c in second["items"]
    }
    card = await get_converted(env)
    assert card["persona_id"] == persona["persona_id"]
    assert card["persona_ref"] == record_ref(persona)
    assert card["owner"] is None and card["sync"]["status"] == "synced"
    assert card["sync"]["desired"] == env.cards.managed(persona)
    assert card["options"] == {
        "tools": ["echo"],
        "skills": [],
        "custom_error_message": "原错误回复",
        "greeting_index": 0,
    }
    assert len(card["snapshots"]) == 1 and card["snapshots"][0]["reason"] == "persona"
    for png in (False, True):
        await env.cards.export(env.web_actor, card["card_id"], png=png)
    assert before == [
        p.model_dump(mode="json") for p in await env.manager.get_all_personas()
    ]
    update.assert_not_awaited()
    create.assert_not_awaited()
    after = await conversations.get_conversation(env.event.unified_msg_origin, cid)
    assert (
        after.persona_id == conversation.persona_id
        and after.history == conversation.history
    )


async def test_conversion_preserves_ownership_and_restricts_foreign_personas(
    conversion,
):
    env = conversion
    own = await legacy(env, "自己的", owned=True)
    other = await legacy(env, "他人的")
    env.storage.register(other["persona_id"], record_ref(other), ["platform", "other"])
    page = await listing(env, env.actor)
    assert (
        len(page["items"]) == 1 and page["items"][0]["persona_id"] == own["persona_id"]
    )
    assert page["items"][0]["owner"] == env.actor.owner
    assert env.cards.storage.known_personas()[0] == {own["persona_id"]}
    all_cards = await listing(env)
    foreign = next(
        c for c in all_cards["items"] if c["persona_id"] == other["persona_id"]
    )
    assert foreign["owner"] == ["platform", "other"]
    with pytest.raises(StudioError) as exc:
        await env.cards.execute(env.actor, "get", {"card_id": foreign["card_id"]})
    assert exc.value.code == "not_found_or_forbidden"
    assert env.cards.storage.get(foreign["card_id"])["owner"] == ["platform", "other"]


async def test_existing_card_archive_and_unbind_are_not_converted_again(conversion):
    env = conversion
    existing = await env.cards.execute(
        env.web_actor, "create", {"card": new_card("原卡")}
    )
    await legacy(env)
    card = await get_converted(env)
    await mutate(env, card, "archive")
    page = await listing(env)
    assert card["card_id"] not in {c["card_id"] for c in page["items"]}
    assert sum(c["persona_id"] == existing["persona_id"] for c in page["items"]) == 1
    original_persona_id = existing["persona_id"]
    existing = await mutate(env, existing, "unbind")
    page = await listing(env)
    assert sum(c["card_id"] == existing["card_id"] for c in page["items"]) == 1
    assert not any(c["persona_id"] == original_persona_id for c in page["items"])
    assert len(env.cards.storage.list(env.web_actor, deleted=True)) == 1
    archived = env.cards.storage.get(card["card_id"])
    assert len(env.cards.storage.snapshots(archived["card_id"])) == 1


async def test_deleted_recreated_persona_stays_conflicted_until_explicit_unbind(
    conversion,
):
    env = conversion
    await legacy(env)
    card = await get_converted(env)
    await env.manager.delete_persona(card["persona_id"])
    await env.manager.create_persona(card["persona_id"], system_prompt="同名新记录")
    page = await listing(env)
    matches = [c for c in page["items"] if c["persona_id"] == card["persona_id"]]
    assert len(matches) == 1 and matches[0]["card_id"] == card["card_id"]
    assert matches[0]["sync_status"] == "conflict"
    assert (await env.studio.current(card["persona_id"]))[
        "system_prompt"
    ] == "同名新记录"


async def test_conversion_respects_core_locks_and_reuses_original_identity(conversion):
    env = conversion
    persona = await legacy(env, owned=True)
    record = env.storage.find(persona["persona_id"], record_ref(persona))
    record["locked"] = 1
    env.storage.save_record(record)
    card = await get_converted(env)
    assert card["sync"]["core_locked"] and not card["locked"]
    with pytest.raises(StudioError) as exc:
        await mutate(env, card, data={"creator_notes": "不能绕过锁定"})
    assert exc.value.code == "persona_locked"
    record["locked"] = 0
    env.storage.save_record(record)
    card = await mutate(env, card, data={"creator_notes": "管理备注"})
    assert card["sync"]["status"] == "synced" and len(card["snapshots"]) == 1
    current = await env.studio.current(persona["persona_id"])
    assert record_ref(current) == record_ref(persona)
    assert current["system_prompt"] == persona["system_prompt"]
    assert current["begin_dialogs"] == persona["begin_dialogs"]


async def test_native_request_is_unchanged_then_edited_examples_are_injected_once(
    conversion,
):
    env = conversion
    persona = await legacy(env)
    card = await get_converted(env)
    env.event.set_extra("_trace_persona_id", persona["persona_id"])
    contexts = env.manager.get_persona_v3_by_id(persona["persona_id"])[
        "_begin_dialogs_processed"
    ]
    request = ProviderRequest(
        system_prompt=persona["system_prompt"], contexts=copy.deepcopy(contexts)
    )
    await env.cards.apply_request(env.event, request)
    assert request.system_prompt == persona["system_prompt"]
    assert request.contexts == contexts
    card = await mutate(
        env,
        card,
        data={
            "mes_example": "{{user}}: 新示例\n{{char}}: 新回答",
            "character_version": "v1",
        },
    )
    current = await env.studio.current(persona["persona_id"])
    assert current["begin_dialogs"] == []
    request = ProviderRequest(system_prompt=current["system_prompt"], contexts=[])
    await env.cards.apply_request(env.event, request)
    assert [m["content"] for m in request.contexts] == ["新示例", "新回答"]
    await env.cards.apply_request(env.event, request)
    assert len(request.contexts) == 2
    assert len(card["snapshots"]) == 2
    card = await mutate(
        env, card, "restore", snapshot_id=card["snapshots"][-1]["snapshot_id"]
    )
    restored = await env.studio.current(persona["persona_id"])
    assert restored["begin_dialogs"] == persona["begin_dialogs"]
    assert restored["system_prompt"] == persona["system_prompt"]
    assert (
        card["document"]["data"]["character_version"] == ""
        and len(card["snapshots"]) == 2
    )


async def test_converted_persona_can_keep_unavailable_tools_and_empty_prompt(
    conversion,
):
    env = conversion
    await legacy(
        env, system_prompt="", begin_dialogs=["单条预设"], tools=["暂不可用的工具"]
    )
    card = await get_converted(env)
    preview = await env.cards.execute(
        env.web_actor, "preview", {"card": card["document"], "options": card["options"]}
    )
    assert preview["compiled"]["system_prompt"] == ""
    card = await mutate(env, card, data={"creator_notes": "仅备注"})
    assert card["sync"]["status"] == "synced"
    assert card["sync"]["current"]["tools"] == ["暂不可用的工具"]
    assert card["sync"]["current"]["begin_dialogs"] == ["单条预设"]
    with pytest.raises(StudioError):
        await mutate(env, card, options={"tools": ["另一个不存在的工具"]})


async def test_conversion_does_not_hide_existing_personas_above_creation_quota(
    conversion,
):
    env = conversion
    await legacy(env)
    env.config["max_personas_total"] = 1
    assert len((await listing(env))["items"]) == 2
    with pytest.raises(StudioError) as exc:
        await env.cards.execute(env.web_actor, "create", {"card": new_card("超额")})
    assert exc.value.code == "limit_exceeded"


async def test_bad_existing_record_does_not_hide_valid_personas(conversion):
    env = conversion
    await legacy(env)
    await env.db.insert_persona(
        persona_id="损坏的", system_prompt="原文", begin_dialogs=[{"bad": "shape"}]
    )
    page = await listing(env)
    assert {c["persona_id"] for c in page["items"]} == {"default", "原有人格"}
    assert page["conversion_errors"][0]["persona_id"] == "损坏的"


async def test_discovery_rechecks_revoked_permissions_before_writing(
    conversion, monkeypatch
):
    env = conversion
    await legacy(env, owned=True)
    original = env.manager.get_all_personas

    async def revoke():
        result = await original()
        env.config["whitelist"] = []
        return result

    monkeypatch.setattr(env.manager, "get_all_personas", revoke)
    with pytest.raises(StudioError):
        await env.cards.execute(env.actor, "list", {}, event=env.event)
    assert env.cards.storage.list(env.web_actor) == []


async def test_conversion_and_initial_snapshot_roll_back_together(conversion):
    env = conversion
    await legacy(env)
    env.storage.connection.execute(
        "CREATE TRIGGER reject_conversion BEFORE INSERT ON card_snapshots BEGIN SELECT RAISE(ABORT, 'failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        await listing(env)
    assert env.cards.storage.list(env.web_actor) == []
    assert env.cards.storage.known_personas() == (set(), set())
    env.storage.connection.execute("DROP TRIGGER reject_conversion")
    assert len((await listing(env))["items"]) == 2


async def test_exported_conversion_keeps_v3_and_original_persona_data(conversion):
    env = conversion
    persona = await legacy(env)
    card = await get_converted(env)
    raw, _, _ = await env.cards.export(env.web_actor, card["card_id"])
    document = json.loads(raw)
    assert document["spec"] == "chara_card_v3" and document["spec_version"] == "3.0"
    assert document["data"]["system_prompt"] == persona["system_prompt"]
    assert (
        document["data"]["extensions"][PERSONA_EXTENSION]["persona_import"]["persona"]
        == persona
    )
