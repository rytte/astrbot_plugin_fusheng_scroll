import asyncio
import base64
import copy
import io
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot_plugin_fusheng_scroll import card_assets
from astrbot_plugin_fusheng_scroll.card_assets import (
    CardAssets,
    PublicResolver,
    data_image,
    download_image,
    image_info,
    main_icon,
    remote_url,
)
from astrbot_plugin_fusheng_scroll.card_service import CardStudio
from astrbot_plugin_fusheng_scroll.cards import (
    MAX_UPLOAD_BYTES,
    _chunk,
    blank_png,
    export_png,
    import_card,
    new_card,
    png_chunks,
)
from astrbot_plugin_fusheng_scroll.domain import Actor, StudioError, canonical, identify
from PIL import Image


def art(fmt="PNG", color="red"):
    output = io.BytesIO()
    Image.new("RGB", (8, 6), color).save(output, fmt)
    return output.getvalue()


def icon(uri, *, name="main", **kwargs):
    return {"type": "icon", "name": name, "uri": uri, "ext": "png", **kwargs}


def card_with(uri):
    card = new_card("头像测试")
    card["data"]["assets"] = [icon(uri)]
    return card


def data_uri(raw, mime="image/png"):
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


@pytest.fixture
async def assets_env(env):
    env.cards = CardStudio(env.studio)
    env.actor = identify(env.event, env.studio.settings())
    return env


async def save(env, document, image=None):
    if image:
        prepared = await env.cards.prepare_import(
            env.actor, export_png(document, image)
        )
        payload = {"import_id": prepared["import_id"], "bind": False}
    else:
        payload = {"card": document, "bind": False}
    return await env.cards.execute(env.actor, "create", payload)


async def update(env, card, **data):
    return await env.cards.execute(
        env.actor,
        "update",
        {
            "card_id": card["card_id"],
            "expected_revision": card["revision"],
            "data": data,
        },
    )


async def test_absent_assets_defaults_to_original_but_explicit_empty_does_not(
    assets_env,
):
    env = assets_env
    card = await save(env, new_card("原图"), blank_png())
    original = env.cards.storage.asset_path(card["asset_id"]).read_bytes()
    result = await env.cards.avatar(env.actor, card["card_id"])
    assert base64.b64decode(result["data_url"].split(",", 1)[1]) == original
    assert result["warning"] is None
    card = await update(env, card, assets=[])
    result = await env.cards.avatar(env.actor, card["card_id"])
    assert result["data_url"] is None and result["warning"] is None
    assert card["asset_id"] is not None
    card = await update(env, card, assets=[{"type": "background", "uri": "ccdefault:"}])
    assert (await env.cards.avatar(env.actor, card["card_id"]))["data_url"] is None


@pytest.mark.parametrize(
    "fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
@pytest.mark.parametrize("has_cover", [False, True])
async def test_main_asset_is_independent_and_export_prefers_card_image(
    assets_env, fmt, mime, has_cover
):
    env = assets_env
    raw = art(fmt)
    document = card_with(data_uri(raw))  # Deliberately inaccurate MIME and ext.
    document["data"]["assets"].insert(
        0, icon(data_uri(art(color="blue")), name="secondary")
    )
    before = copy.deepcopy(document)
    card = await save(env, document, blank_png() if has_cover else None)
    result = await env.cards.avatar(env.actor, card["card_id"])
    assert result["data_url"].startswith(f"data:{mime};base64,")
    assert base64.b64decode(result["data_url"].split(",", 1)[1]) == raw
    assert card["document"] == before
    exported, _, _ = await env.cards.export(env.actor, card["card_id"], png=True)
    assert import_card(exported)[0] == before
    with Image.open(io.BytesIO(exported)) as image:
        assert image.size == ((128, 128) if has_cover else (8, 6))
        if not has_cover:
            assert image.convert("RGB").getpixel((0, 0))[0] > 240
    assert env.cards.storage.get(card["card_id"])["asset_id"] == card["asset_id"]


@pytest.mark.parametrize("kind", [b"tEXt", b"zTXt", b"iTXt"])
async def test_png_embedded_primary_icon_survives_export_with_independent_container(
    assets_env, kind
):
    env = assets_env
    raw = art()
    encoded = base64.b64encode(raw)
    key = b"chara-ext-asset_:portrait.png\0"
    payload = key + encoded
    if kind == b"zTXt":
        payload = key + b"\0" + zlib.compress(encoded)
    elif kind == b"iTXt":
        payload = key + b"\x01\0\0\0" + zlib.compress(encoded)
    original = blank_png()
    original = original[:-12] + _chunk(kind, payload) + original[-12:]
    card = await save(env, card_with("__asset:portrait.png"), original)
    avatar = await env.cards.avatar(env.actor, card["card_id"])
    assert avatar["warning"] is None
    assert base64.b64decode(avatar["data_url"].split(",", 1)[1]) == raw
    exported, _, _ = await env.cards.export(env.actor, card["card_id"], png=True)
    assert sum(p == payload for _, p in png_chunks(exported)) == 1
    document, container = import_card(exported)
    exported_id = env.cards.storage.put_asset(container)
    assert (
        await CardAssets(env.cards.storage).resolve(document, exported_id)
    ).raw == raw
    with Image.open(io.BytesIO(exported)) as image:
        assert image.size == (128, 128)


async def test_remote_success_is_persistent_and_snapshots_reuse_cached_art(
    assets_env, monkeypatch
):
    env = assets_env
    red, blue = art(), art(color="blue")
    fetch = AsyncMock(side_effect=[red, blue])
    monkeypatch.setattr(card_assets, "download_image", fetch)
    document = card_with("https://cards.example/one.png")
    document["data"]["character_version"] = "1"
    card = await save(env, document)
    snapshot = card["snapshots"][0]["snapshot_id"]
    snapshot_before = env.cards.storage.snapshot(card["card_id"], snapshot)
    first = await env.cards.avatar(env.actor, card["card_id"])
    card = await update(
        env, card, assets=[icon("https://cards.example/two.png")], character_version="2"
    )
    second = await env.cards.avatar(env.actor, card["card_id"])
    assert first["data_url"] != second["data_url"]
    card = await env.cards.execute(
        env.actor,
        "restore",
        {
            "card_id": card["card_id"],
            "expected_revision": card["revision"],
            "snapshot_id": snapshot,
        },
    )
    env.cards = CardStudio(env.studio)  # No in-memory cache survives.
    restored = await env.cards.avatar(env.actor, card["card_id"])
    assert restored["data_url"] == first["data_url"]
    assert fetch.await_count == 2
    assert env.cards.storage.snapshot(card["card_id"], snapshot) == snapshot_before
    assert len(card["snapshots"]) == 2
    assert card["document"]["data"]["assets"] == document["data"]["assets"]


@pytest.mark.parametrize(
    "uri",
    [
        "file:///etc/passwd",
        "http://example.org/icon.png",
        "embeded://assets/a.png",
        "__asset:missing",
        "data:image/png;base64,!!!",
    ],
)
async def test_unavailable_resources_preserve_text_and_report_avatar_warning(
    assets_env, uri
):
    env = assets_env
    card = await save(env, card_with(uri), blank_png())
    avatar = await env.cards.avatar(env.actor, card["card_id"])
    assert avatar["data_url"] is None and avatar["warning"]
    raw, _, _ = await env.cards.export(env.actor, card["card_id"])
    assert raw == canonical(card["document"]).encode()
    card = await update(env, card, description="仍能正常编辑")
    assert card["document"]["data"]["description"] == "仍能正常编辑"
    exported, _, _ = await env.cards.export(env.actor, card["card_id"], png=True)
    assert import_card(exported)[0] == card["document"]


async def test_remote_failure_can_retry_without_poisoning_cache(
    assets_env, monkeypatch
):
    env = assets_env
    fetch = AsyncMock(side_effect=[StudioError("invalid_asset", "下载失败"), art()])
    monkeypatch.setattr(card_assets, "download_image", fetch)
    card = await save(env, card_with("https://cards.example/icon.png"))
    assert (await env.cards.avatar(env.actor, card["card_id"]))["warning"]
    assert (await env.cards.avatar(env.actor, card["card_id"]))["data_url"]
    assert fetch.await_count == 2


async def test_authorization_precedes_network_and_card_edits_are_not_blocked(
    assets_env, monkeypatch
):
    env = assets_env
    started, finish = asyncio.Event(), asyncio.Event()

    async def fetch(uri):
        started.set()
        await finish.wait()
        return art()

    download = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(card_assets, "download_image", download)
    card = await save(env, card_with("https://cards.example/icon.png"))
    with pytest.raises(StudioError):
        await env.cards.avatar(Actor("platform", "stranger", False), card["card_id"])
    download.assert_not_awaited()
    task = asyncio.create_task(env.cards.avatar(env.actor, card["card_id"]))
    try:
        await asyncio.wait_for(started.wait(), 2)
        card = await asyncio.wait_for(update(env, card, description="下载期间编辑"), 2)
    finally:
        finish.set()
    with pytest.raises(StudioError) as exc:
        await task
    assert exc.value.code == "revision_conflict"
    assert (await env.cards.avatar(env.actor, card["card_id"]))["revision"] == card[
        "revision"
    ]


@pytest.mark.parametrize(
    "assets",
    [
        [icon("ccdefault:"), icon("ccdefault:")],
        [icon("ccdefault:", name="a"), icon("ccdefault:", name="b")],
        [{"type": "icon", "name": "main"}],
    ],
)
def test_ambiguous_or_invalid_icons_are_not_guessed(assets):
    document = new_card("测试")
    document["data"]["assets"] = assets
    with pytest.raises(StudioError):
        main_icon(document)


def test_single_icon_compatibility_and_unsupported_assets_are_preserved():
    document = card_with("ccdefault:")
    document["data"]["assets"][0]["name"] = "portrait"
    assert main_icon(document)["name"] == "portrait"
    document["data"]["assets"].append({"type": "unknown", "custom": True})
    assert main_icon(document)["name"] == "portrait"


@pytest.mark.parametrize(
    "raw", [b"not an image", art()[:-30], art("GIF"), b"x" * (MAX_UPLOAD_BYTES + 1)]
)
def test_invalid_truncated_or_oversized_image_rejected(raw):
    with pytest.raises(StudioError):
        image_info(raw)


def test_dimension_limit_and_data_url_validation():
    output = io.BytesIO()
    Image.new("RGB", (8193, 1)).save(output, "PNG")
    with pytest.raises(StudioError, match="尺寸"):
        image_info(output.getvalue())
    for uri in (
        "data:image/svg+xml;base64,AAAA",
        "data:image/png,raw",
        "data:image/png;base64,not base64",
    ):
        with pytest.raises(StudioError):
            data_image(uri)


@pytest.mark.parametrize(
    "uri",
    [
        "http://example.com/a.png",
        "https://user:pass@example.com/a.png",
        "https://example.com:8443/a.png",
        "https://127.0.0.1/a.png",
        "https://169.254.169.254/latest",
        "https://10.0.0.1/a.png",
        "https://[::1]/a.png",
        "https://[::ffff:127.0.0.1]/a.png",
        "https://[fe80::1%25eth0]/a.png",
        "https://224.0.0.1/a.png",
    ],
)
def test_unsafe_remote_urls_are_rejected(uri):
    with pytest.raises(StudioError):
        remote_url(uri)


@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["8.8.8.8", "192.168.0.1"], ["::1"], []]
)
async def test_dns_resolution_rejects_all_mixed_and_private_results(addresses):
    resolver = PublicResolver()
    resolver.resolver = SimpleNamespace(
        resolve=AsyncMock(return_value=[{"host": addr} for addr in addresses]),
        close=AsyncMock(),
    )
    try:
        with pytest.raises(StudioError):
            await resolver.resolve("cards.example", 443)
    finally:
        await resolver.close()


class FakeResponse:
    def __init__(self, *, status=200, headers=None, blocks=(), size=None):
        self.status, self.headers, self.blocks, self.content_length = (
            status,
            headers or {},
            blocks,
            size,
        )
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for block in self.blocks:
            yield block


@pytest.fixture
def mock_http(monkeypatch):
    responses, requests, settings = [], [], {}

    class Session:
        def __init__(self, **kwargs):
            settings.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await settings["connector"].close()

        def get(self, url, **kwargs):
            requests.append((str(url), kwargs))
            return responses.pop(0)

    monkeypatch.setattr(card_assets.aiohttp, "ClientSession", Session)
    return responses, requests, settings


async def test_remote_download_redirects_are_validated_and_credentials_are_not_sent(
    mock_http,
):
    responses, requests, settings = mock_http
    responses.extend(
        [
            FakeResponse(status=302, headers={"Location": "/icon.png"}),
            FakeResponse(blocks=[art()]),
        ]
    )
    assert await download_image("https://cards.example/start") == art()
    assert [r[0] for r in requests] == [
        "https://cards.example/start",
        "https://cards.example/icon.png",
    ]
    assert all(r[1] == {"allow_redirects": False} for r in requests)
    assert settings["trust_env"] is False and settings["auto_decompress"] is False
    assert isinstance(settings["cookie_jar"], card_assets.aiohttp.DummyCookieJar)


@pytest.mark.parametrize(
    "target",
    [
        "https://127.0.0.1/private",
        "http://cards.example/icon.png",
        "https://user:pass@cards.example/icon.png",
    ],
)
async def test_unsafe_redirect_is_rejected_before_second_request(mock_http, target):
    responses, requests, _ = mock_http
    responses.append(FakeResponse(status=302, headers={"Location": target}))
    with pytest.raises(StudioError):
        await download_image("https://cards.example/start")
    assert len(requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(status=404),
        FakeResponse(size=MAX_UPLOAD_BYTES + 1),
        FakeResponse(blocks=[b"x" * (MAX_UPLOAD_BYTES + 1)]),
        FakeResponse(status=302),
    ],
)
async def test_http_errors_and_declared_or_streamed_size_limits(mock_http, response):
    responses, _, _ = mock_http
    responses.append(response)
    with pytest.raises(StudioError):
        await download_image("https://cards.example/icon.png")


@pytest.mark.parametrize(
    "host", ["127.1", "2130706433", "0177.0.0.1", "[::127.0.0.1]", "[64:ff9b::7f00:1]"]
)
def test_obfuscated_and_tunnel_localhost_addresses_are_rejected(host):
    with pytest.raises(StudioError):
        remote_url(f"https://{host}/a.png")


async def test_corrupt_cached_image_is_reported_instead_of_changing_snapshot_art(
    assets_env,
):
    env = assets_env
    document = card_with(data_uri(art()))
    card = await save(env, document)
    assert (await env.cards.avatar(env.actor, card["card_id"]))["data_url"]
    cached = env.cards.storage.connection.execute(
        "SELECT asset_id FROM card_asset_cache"
    ).fetchone()
    env.cards.storage.asset_path(cached["asset_id"]).write_bytes(art(color="blue"))
    result = await env.cards.avatar(env.actor, card["card_id"])
    assert result["data_url"] is None and "校验" in result["warning"]


async def test_download_timeout_returns_resource_warning(monkeypatch):
    monkeypatch.setattr(
        card_assets, "_download", AsyncMock(side_effect=asyncio.TimeoutError)
    )
    with pytest.raises(StudioError, match="暂时无法读取"):
        await download_image("https://cards.example/icon.png")


def test_large_compressed_embedded_resource_uses_image_bound_not_card_text_bound():
    raw = b"x" * (5 * 1024 * 1024)
    encoded = base64.b64encode(raw)
    original = blank_png()
    original = (
        original[:-12]
        + _chunk(b"zTXt", b"chara-ext-asset_:large.png\0\0" + zlib.compress(encoded))
        + original[-12:]
    )
    assert CardAssets.embedded(original, "large.png") == raw
    # Large text resources must not prevent validating the original PNG portrait.
    assert image_info(original) == ("image/png", "png")
