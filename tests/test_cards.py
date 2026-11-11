import base64
import copy
import json
import zlib

import pytest
from astrbot_plugin_persona_studio.cards import (
    MAX_CARD_BYTES,
    _chunk,
    blank_png,
    compile_card,
    export_png,
    import_card,
    load_json,
    new_card,
    normalize_card,
    png_chunks,
)
from astrbot_plugin_persona_studio.domain import StudioError, canonical


def sample():
    card = new_card("爱莉")
    card["data"].update(
        character_version="1.0",
        description="{{char}} 认识 {{user}}",
        character_book={
            "entries": [
                {"keys": ["花园"], "content": "未来引擎内容", "extensions": {"x": 1}}
            ]
        },
        extensions={"unknown": {"script": "<script>不执行</script>"}},
        mes_example="<START>\n{{user}}: 你好\n{{char}}: 嗨\n<START>\n{{char}}: 独立示例",
    )
    card["data"].update(
        creation_date=123,
        modification_date=456,
        nickname="Airi",
        assets=[
            {
                "type": "icon",
                "uri": "https://invalid/avatar",
                "name": "main",
                "ext": "png",
            }
        ],
        source=["https://invalid/card"],
        group_only_greetings=["大家好"],
    )
    return card


def v2_sample():
    card = sample()
    card.update(spec="chara_card_v2", spec_version="2.0")
    for field in ("nickname", "assets", "source", "group_only_greetings"):
        del card["data"][field]
    return card


def input_png(card, key=b"chara"):
    image = blank_png()
    return (
        image[:-12]
        + _chunk(b"tEXt", key + b"\0" + base64.b64encode(canonical(card).encode()))
        + image[-12:]
    )


def test_new_card_uses_v3():
    card = new_card()
    assert card["spec"] == "chara_card_v3" and card["spec_version"] == "3.0"
    assert card["data"]["character_version"] == ""
    assert card["data"]["group_only_greetings"] == []


def test_v2_conversion_does_not_invent_author_dates():
    card = load_json(
        '{"spec":"chara_card_v2","spec_version":"2.0","data":{"name":"旧卡","character_version":"beta"}}'
    )
    assert card["spec"] == "chara_card_v3"
    assert card["data"]["character_version"] == "beta"
    assert "creation_date" not in card["data"]
    assert "modification_date" not in card["data"]


@pytest.mark.parametrize(
    "spec,version", [("chara_card_v2", "3.0"), ("chara_card_v3", "2.0")]
)
def test_conversion_validates_original_spec_version(spec, version):
    with pytest.raises(StudioError, match="spec_version"):
        normalize_card(
            {"spec": spec, "spec_version": version, "data": {"name": "旧卡"}}
        )


@pytest.mark.parametrize("png", [False, True])
def test_v2_import_becomes_v3_without_changing_source_or_author_fields(png):
    source = v2_sample()
    source["unknown_top_level"] = {"keep": True}
    source["data"]["unknown_data"] = {"nested": [1, "保留"]}
    before = copy.deepcopy(source)
    raw = input_png(source) if png else canonical(source).encode()
    parsed, image = import_card(raw)
    assert source == before
    assert parsed["spec"] == "chara_card_v3" and parsed["spec_version"] == "3.0"
    assert parsed["unknown_top_level"] == source["unknown_top_level"]
    assert parsed["data"] == {**source["data"], "group_only_greetings": []}
    assert image == (raw if png else None)


def test_v3_json_png_preserve_full_document_and_art():
    card = sample()
    card["unknown_top_level"] = {"keep": True}
    assert load_json(canonical(card)) == card
    image = export_png(card)
    parsed, original = import_card(image)
    assert parsed == card and original == image
    assert [p for k, p in png_chunks(image) if k == b"IDAT"] == [
        p for k, p in png_chunks(blank_png()) if k == b"IDAT"
    ]
    card["data"]["name"] = "新名称"
    updated = export_png(card, image)
    assert import_card(updated)[0] == card


@pytest.mark.parametrize("legacy_image", [False, True])
def test_png_export_contains_only_v3_and_replaces_old_metadata(legacy_image):
    source = v2_sample()
    original = input_png(source) if legacy_image else None
    image = export_png(source, original)
    chunks = png_chunks(image)
    texts = [p for k, p in chunks if k in (b"tEXt", b"zTXt", b"iTXt")]
    assert len(texts) == 1 and texts[0].startswith(b"ccv3\0")
    document = json.loads(base64.b64decode(texts[0].split(b"\0", 1)[1]))
    assert document["spec"] == "chara_card_v3"
    assert document["spec_version"] == "3.0"
    assert document["data"]["character_version"] == "1.0"
    assert source["spec"] == "chara_card_v2"
    assert [p for k, p in chunks if k == b"IDAT"] == [
        p for k, p in png_chunks(original or blank_png()) if k == b"IDAT"
    ]


def test_v2_import_recovers_legacy_v3_fields_and_preserves_conflicting_extensions():
    source = v2_sample()
    namespace = "astrbot_persona_studio_v3"
    fields = {"nickname": "Airi", "group_only_greetings": ["大家好"], "assets": []}
    source["data"]["extensions"][namespace] = fields
    normalized = normalize_card(source)
    assert normalized["data"]["nickname"] == "Airi"
    assert normalized["data"]["group_only_greetings"] == ["大家好"]
    assert normalized["data"]["assets"] == []
    assert namespace not in normalized["data"]["extensions"]
    assert source["data"]["extensions"][namespace] == fields
    source["data"]["nickname"] = "另一名称"
    normalized = normalize_card(source)
    assert normalized["data"]["nickname"] == "另一名称"
    assert normalized["data"]["extensions"][namespace] == fields
    source["data"]["extensions"][namespace] = {"unknown": True}
    assert normalize_card(source)["data"]["extensions"][namespace] == {"unknown": True}


@pytest.mark.parametrize("kind", [b"tEXt", b"zTXt", b"iTXt"])
def test_png_supported_text_chunks_and_v3_precedence(kind):
    encoded = base64.b64encode(canonical(sample()).encode())
    if kind == b"tEXt":
        payload = b"ccv3\0" + encoded
    elif kind == b"zTXt":
        payload = b"ccv3\0\0" + zlib.compress(encoded)
    else:
        payload = b"ccv3\0\1\0\0\0" + zlib.compress(encoded)
    raw = input_png(v2_sample())
    raw = raw[:-12] + _chunk(kind, payload) + raw[-12:]
    assert import_card(raw)[0] == sample()


@pytest.mark.parametrize("kind", [b"tEXt", b"zTXt", b"iTXt"])
def test_v2_import_supported_text_chunks(kind):
    source = v2_sample()
    encoded = base64.b64encode(canonical(source).encode())
    if kind == b"tEXt":
        payload = b"chara\0" + encoded
    elif kind == b"zTXt":
        payload = b"chara\0\0" + zlib.compress(encoded)
    else:
        payload = b"chara\0\1\0\0\0" + zlib.compress(encoded)
    image = blank_png()
    parsed, original = import_card(image[:-12] + _chunk(kind, payload) + image[-12:])
    assert parsed == {
        **source,
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {**source["data"], "group_only_greetings": []},
    }
    assert original is not None


@pytest.mark.parametrize(
    "mutation", ["crc", "truncate", "duplicate", "zipbomb", "no-card"]
)
def test_bad_png_rejected(mutation):
    raw = export_png(sample())
    if mutation == "crc":
        raw = raw[:20] + bytes([raw[20] ^ 1]) + raw[21:]
    elif mutation == "truncate":
        raw = raw[:-3]
    elif mutation == "duplicate":
        raw = raw[:-12] + _chunk(b"tEXt", b"ccv3\0e30=") + raw[-12:]
    elif mutation == "zipbomb":
        raw = blank_png()
        raw = (
            raw[:-12]
            + _chunk(
                b"zTXt", b"chara\0\0" + zlib.compress(b"A" * (MAX_CARD_BYTES * 2 + 1))
            )
            + raw[-12:]
        )
    else:
        raw = blank_png()
    with pytest.raises(StudioError):
        import_card(raw)


@pytest.mark.parametrize("raw", [b"[]", b'{"spec": NaN}', b"{}", b"\xff", b"[" * 2000])
def test_invalid_json_rejected(raw):
    with pytest.raises(StudioError):
        load_json(raw)


def test_compilation_keeps_metadata_out_and_examples_separate():
    card = sample()
    card["data"].update(
        creator_notes="private editor notes",
        system_prompt="{{original}}\n再指令",
        post_history_instructions="最后要求",
    )
    original = copy.deepcopy(card)
    result = compile_card(card)
    assert card == original
    assert "private editor notes" not in result["system_prompt"]
    assert "未来引擎内容" not in result["system_prompt"]
    assert result["begin_dialogs"] == []
    assert [len(b) for b in result["examples"]] == [2, 1]
    assert "{{original}}" not in result["system_prompt"]
    assert len(result["warnings"]) >= 3
    card["data"]["mes_example"] = "非标准对白"
    result = compile_card(card)
    assert not result["examples"] and "非标准对白" in result["system_prompt"]


def test_validation_preserves_unknown_fields_but_rejects_bad_shapes():
    card = sample()
    card["data"]["custom"] = {"a": ["b"]}
    assert normalize_card(card)["data"]["custom"] == {"a": ["b"]}
    card["data"]["alternate_greetings"] = None
    with pytest.raises(StudioError):
        normalize_card(card)
