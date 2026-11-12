import asyncio
import base64
import copy
import sqlite3
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_fusheng_scroll.characters import builtin
from astrbot_plugin_fusheng_scroll.characters.codec import (
    DEFAULT_OPTIONS,
    export_png,
    import_card,
    new_card,
)
from astrbot_plugin_fusheng_scroll.characters.service import CardStudio
from astrbot_plugin_fusheng_scroll.characters.storage import CardStorage
from astrbot_plugin_fusheng_scroll.core.domain import Actor, StudioError
from astrbot_plugin_fusheng_scroll.core.storage import Storage
from astrbot_plugin_fusheng_scroll.main import PersonaStudioPlugin


@pytest.fixture
async def builtin_env(env, tmp_path, monkeypatch):
    env.cards = CardStudio(env.studio)
    env.studio.cards = env.cards
    document = new_card("知绘")
    document["data"].update(description="内置设定", character_version="1.0")
    bundle = tmp_path / "zhihui.png"
    bundle.write_bytes(export_png(document))
    monkeypatch.setattr(builtin, "BUILTIN_CARD_PATH", bundle)
    return env


async def test_bundled_png_preserves_cover_avatar_and_character(env):
    raw = builtin.BUILTIN_CARD_PATH.read_bytes()
    document, image = import_card(raw)
    assert document["spec"] == "chara_card_v3"
    assert document["data"]["name"] == "知绘"
    assert image == raw
    env.cards = CardStudio(env.studio)
    card = await builtin.install_builtin_character(env.cards)
    assert card["document"] == document
    assert card["owner"] is None
    assert card["sync_status"] == "synced"
    assert env.cards.storage.asset_path(card["asset_id"]).read_bytes() == raw
    icon = next(a for a in document["data"]["assets"] if a["name"] == "main")
    avatar = await env.cards.avatar(builtin.SYSTEM_ACTOR, card["card_id"])
    assert avatar["warning"] is None
    assert avatar["data_url"] == icon["uri"]
    assert base64.b64decode(icon["uri"].split(",", 1)[1]).startswith(b"\x89PNG")
    core = await env.studio.current(card["persona_id"])
    detail = await env.cards.detail(card)
    assert core["system_prompt"] == detail["compiled"]["system_prompt"]
    snapshots = env.cards.storage.snapshots(card["card_id"])
    assert len(snapshots) == 1 and snapshots[0]["reason"] == "builtin"
    snapshot = env.cards.storage.snapshot(card["card_id"], snapshots[0]["snapshot_id"])
    assert snapshot["snapshot"]["asset_id"] == card["asset_id"]
    assert snapshot["snapshot"]["document"] == document


async def test_seed_keeps_existing_names_sessions_and_permissions(builtin_env):
    env = builtin_env
    existing = await env.manager.create_persona("知绘", system_prompt="原有人格")
    conv = await env.context.conversation_manager.new_conversation(
        env.event.unified_msg_origin, persona_id="知绘"
    )
    before_config = copy.deepcopy(env.core_config)
    before_sessions = dict(env.context.conversation_manager.session_conversations)
    card = await builtin.install_builtin_character(env.cards)
    assert card["persona_id"] != "知绘"
    assert (await env.studio.current("知绘")) == existing.model_dump(mode="json")
    assert env.core_config == before_config
    assert env.context.conversation_manager.session_conversations == before_sessions
    conversation = await env.context.conversation_manager.get_conversation(
        env.event.unified_msg_origin, conv
    )
    assert conversation.persona_id == "知绘"
    with pytest.raises(StudioError) as exc:
        await env.cards.execute(
            Actor("platform", "user", False), "get", {"card_id": card["card_id"]}
        )
    assert exc.value.code == "not_found_or_forbidden"
    assert env.cards.storage.list(builtin.SYSTEM_ACTOR) == [card]
    assert len(await env.manager.get_all_personas()) == 3


@pytest.mark.parametrize(
    "operation",
    [
        "edit",
        "archive",
        "delete_card",
        "delete_both",
        "unbind",
        "lock",
        "core_edit",
        "core_delete",
    ],
)
async def test_restart_preserves_user_choices(builtin_env, monkeypatch, operation):
    env = builtin_env
    card = await builtin.install_builtin_character(env.cards)
    payload = {"card_id": card["card_id"], "expected_revision": card["revision"]}
    if operation == "edit":
        await env.cards.execute(
            builtin.SYSTEM_ACTOR,
            "update",
            {
                **payload,
                "data": {"name": "我的知绘", "description": "我改的", "assets": []},
            },
        )
    elif operation in ("archive", "delete_card", "delete_both"):
        archived = await env.cards.execute(builtin.SYSTEM_ACTOR, "archive", payload)
        if operation != "archive":
            await env.cards.execute(
                builtin.SYSTEM_ACTOR,
                "delete",
                {
                    **payload,
                    "expected_revision": archived["revision"],
                    "confirm_name": "知绘",
                    "delete_persona": operation == "delete_both",
                },
            )
    elif operation == "unbind":
        await env.cards.execute(builtin.SYSTEM_ACTOR, "unbind", payload)
    elif operation == "lock":
        await env.cards.execute(
            builtin.SYSTEM_ACTOR, "lock", {**payload, "locked": True}
        )
    elif operation == "core_edit":
        await env.manager.update_persona(
            card["persona_id"], system_prompt="核心外部修改"
        )
    else:
        await env.manager.delete_persona(card["persona_id"])

    # The persisted marker survives reopening, independent of names and card deletion.
    path = env.storage.path
    env.storage.close()
    reopened = Storage(path)
    env.studio.storage = reopened
    env.cards = CardStudio(env.studio)
    env.studio.cards = env.cards
    try:
        before = list(reopened.connection.iterdump())
        personas = [
            p.model_dump(mode="json") for p in await env.manager.get_all_personas()
        ]
        monkeypatch.setattr(builtin, "BUILTIN_CARD_PATH", path.parent / "missing.png")
        assert await builtin.install_builtin_character(env.cards) is None
        assert list(reopened.connection.iterdump()) == before
        assert [
            p.model_dump(mode="json") for p in await env.manager.get_all_personas()
        ] == personas
        assert env.cards.storage.builtin_card_id(builtin.BUILTIN_ID) == card["card_id"]
    finally:
        reopened.close()


async def test_new_bundle_version_never_overwrites_installed_copy(builtin_env):
    env = builtin_env
    card = await builtin.install_builtin_character(env.cards)
    replacement = new_card("新版知绘")
    replacement["data"]["character_version"] = "2.0"
    builtin.BUILTIN_CARD_PATH.write_bytes(export_png(replacement))
    assert await builtin.install_builtin_character(env.cards) is None
    assert env.cards.storage.get(card["card_id"]) == card
    assert len(env.cards.storage.snapshots(card["card_id"])) == 1


async def test_failed_core_sync_keeps_one_card_for_manual_retry(
    builtin_env, monkeypatch
):
    env = builtin_env
    real_create = env.manager.create_persona
    failing_create = AsyncMock(side_effect=RuntimeError("core unavailable"))
    monkeypatch.setattr(env.manager, "create_persona", failing_create)
    card = await builtin.install_builtin_character(env.cards)
    assert card["sync_status"] == "failed"
    assert card["asset_id"]
    assert await builtin.install_builtin_character(env.cards) is None
    failing_create.assert_awaited_once()
    monkeypatch.setattr(env.manager, "create_persona", real_create)
    synced = await env.cards.execute(
        builtin.SYSTEM_ACTOR,
        "sync",
        {"card_id": card["card_id"], "expected_revision": card["revision"]},
    )
    assert synced["sync"]["status"] == "synced"
    assert len(env.cards.storage.list(builtin.SYSTEM_ACTOR)) == 1


async def test_interruption_after_card_commit_never_creates_duplicate(
    builtin_env, monkeypatch
):
    env = builtin_env
    real_sync = env.cards.sync
    monkeypatch.setattr(
        env.cards, "sync", AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await builtin.install_builtin_character(env.cards)
    card_id = env.cards.storage.builtin_card_id(builtin.BUILTIN_ID)
    card = env.cards.storage.get(card_id)
    assert card["sync_status"] == "pending" and card["persona_id"]
    monkeypatch.setattr(env.cards, "sync", real_sync)
    assert await builtin.install_builtin_character(env.cards) is None
    synced = await env.cards.execute(
        builtin.SYSTEM_ACTOR,
        "sync",
        {"card_id": card_id, "expected_revision": card["revision"]},
    )
    assert synced["sync"]["status"] == "synced"
    assert len(env.cards.storage.list(builtin.SYSTEM_ACTOR)) == 1


async def test_quota_failure_does_not_consume_install_marker(builtin_env):
    env = builtin_env
    env.config["max_personas_total"] = 1
    with pytest.raises(StudioError) as exc:
        await builtin.install_builtin_character(env.cards)
    assert exc.value.code == "limit_exceeded"
    assert env.cards.storage.builtin_card_id(builtin.BUILTIN_ID) is None
    assert env.cards.storage.list(builtin.SYSTEM_ACTOR) == []
    env.config["max_personas_total"] = 200
    assert (await builtin.install_builtin_character(env.cards))[
        "sync_status"
    ] == "synced"


@pytest.mark.parametrize("bundle_error", ["missing", "invalid"])
async def test_startup_survives_bad_bundle_and_retries_before_creation(
    builtin_env, monkeypatch, tmp_path, bundle_error
):
    from astrbot.api.star import StarTools

    good_bundle = builtin.BUILTIN_CARD_PATH
    broken = tmp_path / "broken.png"
    if bundle_error == "invalid":
        broken.write_bytes(b"invalid png")
    monkeypatch.setattr(builtin, "BUILTIN_CARD_PATH", broken)
    monkeypatch.setattr(StarTools, "get_data_dir", lambda name: tmp_path / "plugin")
    plugin = PersonaStudioPlugin(builtin_env.context, builtin_env.config)
    await plugin.initialize()
    try:
        assert plugin.maintenance_task is not None and plugin.web is not None
        assert plugin.cards.storage.builtin_card_id(builtin.BUILTIN_ID) is None
    finally:
        await plugin.terminate()

    monkeypatch.setattr(builtin, "BUILTIN_CARD_PATH", good_bundle)
    await plugin.initialize()
    try:
        card_id = plugin.cards.storage.builtin_card_id(builtin.BUILTIN_ID)
        assert plugin.cards.storage.get(card_id)["sync_status"] == "synced"
    finally:
        await plugin.terminate()
    await plugin.initialize()
    try:
        assert plugin.cards.storage.builtin_card_id(builtin.BUILTIN_ID) == card_id
        assert len(plugin.cards.storage.list(builtin.SYSTEM_ACTOR)) == 1
    finally:
        await plugin.terminate()


def test_seed_marker_snapshot_and_pending_binding_commit_atomically(tmp_path):
    storage = Storage(tmp_path / "studio.sqlite3")
    try:
        cards = CardStorage(storage)
        storage.connection.execute(
            "CREATE TRIGGER reject_seed BEFORE INSERT ON card_builtin_seeds "
            "BEGIN SELECT RAISE(ABORT, 'marker failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError, match="marker failure"):
            cards.create_builtin(
                "mascot", new_card("知绘"), options=DEFAULT_OPTIONS, asset_id=None
            )
        assert cards.list(builtin.SYSTEM_ACTOR) == []
        assert cards.builtin_card_id("mascot") is None
        assert (
            storage.connection.execute(
                "SELECT COUNT(*) FROM card_snapshots"
            ).fetchone()[0]
            == 0
        )
        storage.connection.execute("DROP TRIGGER reject_seed")
        card = cards.create_builtin(
            "mascot", new_card("知绘"), options=DEFAULT_OPTIONS, asset_id=None
        )
        assert card["owner"] is None
        assert card["sync_status"] == "pending" and card["persona_id"]
        with pytest.raises(sqlite3.IntegrityError):
            cards.create_builtin(
                "mascot",
                new_card("同名也不重复"),
                options=DEFAULT_OPTIONS,
                asset_id=None,
            )
        assert cards.list(builtin.SYSTEM_ACTOR) == [card]
        assert len(cards.snapshots(card["card_id"])) == 1
    finally:
        storage.close()
