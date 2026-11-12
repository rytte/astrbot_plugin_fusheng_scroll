import sqlite3

import pytest
from astrbot_plugin_fusheng_scroll.characters.codec import new_card
from astrbot_plugin_fusheng_scroll.characters.storage import CardStorage
from astrbot_plugin_fusheng_scroll.core.domain import Actor, StudioError, canonical
from astrbot_plugin_fusheng_scroll.core.storage import Storage


def test_v1_migration_preserves_owners_locks_and_backups(tmp_path):
    path = tmp_path / "studio.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE records (chain_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL,
          persona_ref TEXT NOT NULL, owner TEXT, locked INTEGER NOT NULL,
          revision INTEGER NOT NULL, deleted INTEGER NOT NULL, UNIQUE(persona_id,persona_ref));
        CREATE TABLE backups (backup_id TEXT PRIMARY KEY, chain_id TEXT NOT NULL REFERENCES records(chain_id),
          created_at REAL NOT NULL, action TEXT NOT NULL, snapshot TEXT NOT NULL, checksum TEXT NOT NULL);
        CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, entry TEXT NOT NULL);
        PRAGMA user_version=1;
    """)
    reference = canonical({"id": 1, "created_at": "2026-01-01T00:00:00"})
    connection.execute(
        "INSERT INTO records VALUES (?, ?, ?, ?, 1, 7, 0)",
        (
            "12345678-1234-5678-1234-567812345678",
            "alice",
            reference,
            canonical(["platform", "user"]),
        ),
    )
    connection.execute(
        "INSERT INTO backups VALUES (?,?,?,?,?,?)",
        (
            "backup",
            "12345678-1234-5678-1234-567812345678",
            123,
            "update",
            "original-snapshot",
            "original-checksum",
        ),
    )
    connection.commit()
    before = connection.execute("SELECT * FROM backups").fetchall()
    connection.close()
    storage = Storage(path)
    try:
        assert storage.records()[0]["owner"] == ["platform", "user"]
        assert (
            storage.records()[0]["locked"] == 1
            and storage.records()[0]["revision"] == 7
        )
        assert [
            tuple(r) for r in storage.connection.execute("SELECT * FROM backups")
        ] == before
        assert storage.connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert CardStorage(storage).list(Actor("", "", True)) == []
    finally:
        storage.close()
    reopened = Storage(path)
    reopened.close()


def test_v6_migration_adds_seed_registry_without_changing_user_data(tmp_path):
    path = tmp_path / "studio.sqlite3"
    storage = Storage(path)
    cards = CardStorage(storage)
    card = cards.create(Actor("p", "u", False), new_card("已有知绘"))
    before = list(storage.connection.execute("SELECT * FROM cards"))
    snapshots = list(storage.connection.execute("SELECT * FROM card_snapshots"))
    storage.connection.executescript(
        "DROP TABLE card_builtin_seeds; PRAGMA user_version=6;"
    )
    storage.close()
    migrated = Storage(path)
    try:
        assert migrated.connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert list(migrated.connection.execute("SELECT * FROM cards")) == before
        assert (
            list(migrated.connection.execute("SELECT * FROM card_snapshots"))
            == snapshots
        )
        assert (
            CardStorage(migrated).get(card["card_id"])["document"] == card["document"]
        )
        assert CardStorage(migrated).builtin_card_id("builtin_zhihui") is None
        assert migrated.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migrated.close()


def test_card_update_and_snapshot_rollback_as_one_transaction(tmp_path):
    storage = Storage(tmp_path / "studio.sqlite3")
    try:
        cards = CardStorage(storage)
        c = cards.create(Actor("p", "u", False), new_card("角色"))
        storage.connection.execute(
            "CREATE TRIGGER reject_snapshot BEFORE INSERT ON card_snapshots BEGIN SELECT RAISE(ABORT, 'write failure'); END"
        )
        c["document"]["data"]["character_version"] = "2"
        with pytest.raises(sqlite3.IntegrityError):
            cards.save(c, c["revision"], snapshot=True)
        saved = cards.get(c["card_id"])
        assert (
            saved["revision"] == 1
            and saved["document"]["data"]["character_version"] == ""
        )
        assert len(cards.snapshots(c["card_id"])) == 1
        snapshot_id = cards.snapshots(c["card_id"])[0]["snapshot_id"]
        with storage.connection:
            storage.connection.execute("UPDATE card_snapshots SET checksum='tampered'")
        with pytest.raises(StudioError) as exc:
            cards.snapshot(c["card_id"], snapshot_id)
        assert exc.value.code == "snapshot_corrupt"
    finally:
        storage.close()


def test_v2_migration_preserves_cards_and_remembers_existing_bindings(tmp_path):
    path = tmp_path / "studio.sqlite3"
    storage = Storage(path)
    cards = CardStorage(storage)
    card = cards.create(Actor("p", "u", False), new_card("原卡"))
    reference = canonical({"id": 4, "created_at": "2026-01-01T00:00:00"})
    cards.sync_state(
        card["card_id"], "synced", persona_id="已有绑定", persona_ref=reference
    )
    before = tuple(storage.connection.execute("SELECT * FROM cards").fetchone())
    snapshot = tuple(
        storage.connection.execute("SELECT * FROM card_snapshots").fetchone()
    )
    storage.connection.executescript(
        "DROP TABLE card_builtin_seeds; DROP TABLE card_asset_garbage; DROP TABLE card_deleted_personas; DROP TABLE card_image_uploads; DROP TABLE card_asset_cache; DROP TABLE card_persona_sources; PRAGMA user_version=2;"
    )
    storage.close()
    migrated = Storage(path)
    try:
        assert (
            tuple(migrated.connection.execute("SELECT * FROM cards").fetchone())
            == before
        )
        assert (
            tuple(
                migrated.connection.execute("SELECT * FROM card_snapshots").fetchone()
            )
            == snapshot
        )
        assert CardStorage(migrated).known_personas() == (
            {"已有绑定"},
            {("已有绑定", reference)},
        )
    finally:
        migrated.close()


def test_v3_migration_adds_cache_without_changing_old_snapshots_or_assets(tmp_path):
    path = tmp_path / "studio.sqlite3"
    storage = Storage(path)
    cards = CardStorage(storage)
    card = cards.create(Actor("p", "u", False), new_card("旧快照"))
    before = cards.snapshot(
        card["card_id"], cards.snapshots(card["card_id"])[0]["snapshot_id"]
    )
    storage.connection.executescript(
        "DROP TABLE card_builtin_seeds; DROP TABLE card_asset_garbage; DROP TABLE card_deleted_personas; DROP TABLE card_image_uploads; DROP TABLE card_asset_cache; PRAGMA user_version=3;"
    )
    storage.close()
    migrated = Storage(path)
    try:
        assert migrated.connection.execute("PRAGMA user_version").fetchone()[0] == 7
        cards = CardStorage(migrated)
        assert cards.snapshot(card["card_id"], before["snapshot_id"]) == before
        assert cards.cached_asset("nonexistent") is None
    finally:
        migrated.close()


def test_v4_migration_preserves_cached_images_and_existing_snapshots(tmp_path):
    path = tmp_path / "studio.sqlite3"
    storage = Storage(path)
    cards = CardStorage(storage)
    card = cards.create(Actor("p", "u", False), new_card("升级"))
    cards.cache_asset("existing-key", "a" * 64 + ".png", "image/png")
    before = cards.snapshot(
        card["card_id"], cards.snapshots(card["card_id"])[0]["snapshot_id"]
    )
    storage.connection.executescript(
        "DROP TABLE card_builtin_seeds; DROP TABLE card_asset_garbage; DROP TABLE card_deleted_personas; DROP TABLE card_image_uploads; PRAGMA user_version=4;"
    )
    storage.close()
    migrated = Storage(path)
    try:
        assert migrated.connection.execute("PRAGMA user_version").fetchone()[0] == 7
        cards = CardStorage(migrated)
        assert cards.snapshot(card["card_id"], before["snapshot_id"]) == before
        assert cards.cached_asset("existing-key")["asset_id"] == "a" * 64 + ".png"
        assert (
            migrated.connection.execute(
                "SELECT COUNT(*) FROM card_image_uploads"
            ).fetchone()[0]
            == 0
        )
    finally:
        migrated.close()


def test_v5_migration_preserves_cards_sources_and_assets(tmp_path):
    path = tmp_path / "studio.sqlite3"
    storage = Storage(path)
    cards = CardStorage(storage)
    card = cards.create(Actor("p", "u", False), new_card("旧角色"))
    cards.sync_state(card["card_id"], "synced", persona_id="旧人格", persona_ref="ref")
    before = cards.get(card["card_id"])
    storage.connection.executescript(
        "DROP TABLE card_builtin_seeds; DROP TABLE card_deleted_personas; DROP TABLE card_asset_garbage; PRAGMA user_version=5;"
    )
    storage.close()
    storage = Storage(path)
    try:
        assert storage.connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert CardStorage(storage).get(card["card_id"]) == before
        assert CardStorage(storage).known_personas() == (
            {"旧人格"},
            {("旧人格", "ref")},
        )
        assert storage.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        storage.close()


@pytest.mark.parametrize("failure", ["card", "audit"])
def test_permanent_delete_transaction_rolls_back_all_related_changes(tmp_path, failure):
    storage = Storage(tmp_path / "studio.sqlite3")
    try:
        cards, actor = CardStorage(storage), Actor("p", "u", False)
        imported = cards.prepare_import(actor, new_card("角色"), b"image")
        card = cards.create(
            actor,
            new_card("角色"),
            asset_id=cards.get_import(actor, imported)["asset_id"],
            imported=imported,
        )
        cards.sync_state(
            card["card_id"], "synced", persona_id="persona", persona_ref="reference"
        )
        card = cards.get(card["card_id"])
        card["deleted"] = 1
        card = cards.save(card, card["revision"])
        operation = "DELETE ON cards" if failure == "card" else "INSERT ON audit"
        storage.connection.execute(
            f"CREATE TRIGGER reject_delete BEFORE {operation} BEGIN SELECT RAISE(ABORT, 'write failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            cards.delete(actor, card, card["revision"])
        assert cards.get(card["card_id"]) == card
        assert cards.snapshots(card["card_id"])
        assert cards.get_import(actor, imported)["card_id"] == card["card_id"]
        assert cards.known_personas() == ({"persona"}, {("persona", "reference")})
        for table in ("card_deleted_personas", "card_asset_garbage"):
            assert storage.connection.execute(f"SELECT * FROM {table}").fetchall() == []
        assert cards.asset_path(card["asset_id"]).exists()
    finally:
        storage.close()


@pytest.mark.parametrize("reference", ["card", "snapshot", "import", "upload", "cache"])
def test_delete_reclaims_only_unreferenced_images(tmp_path, reference):
    storage = Storage(tmp_path / "studio.sqlite3")
    try:
        cards, actor = CardStorage(storage), Actor("p", "u", False)
        asset = cards.put_asset(b"shared-image")
        obsolete = cards.put_asset(b"old-image")
        document = new_card("删除的角色")
        document["data"]["assets"] = [
            {
                "type": "icon",
                "name": "main",
                "uri": "https://example.com/avatar.png",
                "ext": "png",
            }
        ]
        key = next(iter(cards.resource_refs([(document, None)])[1]))
        cards.cache_asset(key, asset, "image/png")
        card = cards.create(actor, document, asset_id=obsolete)
        card["asset_id"] = asset
        card["deleted"] = 1
        card = cards.save(card, card["revision"])
        other = cards.create(
            actor,
            document if reference == "cache" else new_card("保留的角色"),
            asset_id=asset if reference in ("card", "snapshot") else None,
        )
        if reference == "snapshot":
            other["asset_id"] = None
            cards.save(other, other["revision"])
        elif reference == "import":
            cards.prepare_import(actor, new_card("待导入"), b"shared-image")
        elif reference == "upload":
            cards.prepare_image(actor, asset, "card_image")
        cards.delete(actor, card, card["revision"])
        assert cards.cleanup_assets() == 0
        assert cards.asset_path(asset).exists()
        assert not cards.asset_path(obsolete).exists()
        with storage.connection:
            storage.connection.execute(
                "DELETE FROM card_snapshots WHERE card_id=?", (other["card_id"],)
            )
            storage.connection.execute(
                "DELETE FROM cards WHERE card_id=?", (other["card_id"],)
            )
            storage.connection.execute("DELETE FROM card_imports")
            storage.connection.execute("DELETE FROM card_image_uploads")
        assert cards.cleanup_assets() == 0
        assert not cards.asset_path(asset).exists()
        assert cards.cached_asset(key) is None
        assert (
            storage.connection.execute("SELECT * FROM card_asset_garbage").fetchall()
            == []
        )
    finally:
        storage.close()


def test_failed_image_cleanup_is_retryable_after_restart(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "studio.sqlite3"
    storage = Storage(path)
    cards, actor = CardStorage(storage), Actor("p", "u", False)
    asset = cards.put_asset(b"image")
    card = cards.create(actor, new_card("角色"), asset_id=asset)
    card["deleted"] = 1
    card = cards.save(card, card["revision"])
    cards.delete(actor, card, card["revision"])
    with monkeypatch.context() as patch:

        def denied(*args, **kwargs):
            raise PermissionError("busy")

        patch.setattr(Path, "unlink", denied)
        assert cards.cleanup_assets() == 1
        assert cards.asset_path(asset).exists()
    storage.close()
    storage = Storage(path)
    try:
        cards = CardStorage(storage)
        assert cards.cleanup_assets() == 0
        assert not cards.asset_path(asset).exists()
    finally:
        storage.close()
