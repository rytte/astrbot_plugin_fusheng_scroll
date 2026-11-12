"""Character Card V3 documents with V2 import compatibility.

The document is the source of truth. Compilation never rewrites its text.
World books run separately against each request; scripts and regex never execute.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import struct
import zlib

from ..core.domain import StudioError, canonical
from .worldbook import book_warnings

MAX_CARD_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
STRING_FIELDS = (
    "name",
    "description",
    "personality",
    "scenario",
    "first_mes",
    "mes_example",
    "creator_notes",
    "system_prompt",
    "post_history_instructions",
    "creator",
    "character_version",
)
V3_FIELDS = {
    "assets",
    "nickname",
    "creator_notes_multilingual",
    "source",
    "group_only_greetings",
    "creation_date",
    "modification_date",
}
DEFAULT_INSTRUCTION = "以 {{char}} 的身份与 {{user}} 自然交流，保持角色的语气与背景。"
DEFAULT_OPTIONS = {
    "tools": None,
    "skills": None,
    "custom_error_message": None,
    "greeting_index": 0,
}
PERSONA_EXTENSION = "astrbot_persona_studio"


def _invalid(message: str) -> StudioError:
    return StudioError("invalid_card", message)


def load_json(raw: bytes | str) -> dict:
    if len(raw) > MAX_CARD_BYTES:
        raise StudioError("limit_exceeded", "角色卡 JSON 最大为 2 MiB。")
    try:

        def constant(value):
            raise ValueError(f"invalid constant: {value}")

        value = json.loads(raw, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _invalid("角色卡不是有效的 UTF-8 JSON。") from exc
    return normalize_card(value)


def normalize_card(value: dict) -> dict:
    """Validate either supported input format and return a V3 document."""
    if not isinstance(value, dict):
        raise _invalid("角色卡必须是 JSON 对象。")
    try:
        if len(canonical(value).encode("utf-8")) > MAX_CARD_BYTES:
            raise StudioError("limit_exceeded", "角色卡 JSON 最大为 2 MiB。")
    except (ValueError, TypeError, RecursionError) as exc:
        raise _invalid("角色卡包含无法保存的 JSON 值。") from exc
    card = copy.deepcopy(value)
    spec = card.get("spec")
    if spec not in ("chara_card_v2", "chara_card_v3"):
        raise _invalid("仅支持 Character Card V2/V3，请提供 spec 与 data。")
    expected = "2.0" if spec == "chara_card_v2" else "3.0"
    if card.get("spec_version") != expected:
        raise _invalid(f"{spec} 的 spec_version 必须为 {expected}。")
    data = card.get("data")
    if not isinstance(data, dict):
        raise _invalid("角色卡 data 必须是对象。")
    if spec == "chara_card_v2" and isinstance(data.get("extensions"), dict):
        # Recover fields from older V2 exports without discarding conflicting data.
        namespace = "astrbot_persona_studio_v3"
        extra = data["extensions"].get(namespace)
        if isinstance(extra, dict) and set(extra) <= V3_FIELDS:
            for key, value in extra.items():
                data.setdefault(key, value)
            if all(data[key] == value for key, value in extra.items()):
                del data["extensions"][namespace]
    for name in STRING_FIELDS:
        data.setdefault(name, "")
        if not isinstance(data[name], str):
            raise _invalid(f"data.{name} 必须为字符串。")
    if not data["name"].strip() or len(data["name"]) > 255:
        raise _invalid("角色名称必须为 1～255 字符，不能全为空白。")
    if len(data["character_version"]) > 255:
        raise _invalid("角色版本最多 255 字符。")
    for name in ("alternate_greetings", "tags", "group_only_greetings", "source"):
        if name in ("alternate_greetings", "tags", "group_only_greetings"):
            data.setdefault(name, [])
        if name in data and (
            not isinstance(data[name], list)
            or any(not isinstance(item, str) for item in data[name])
        ):
            raise _invalid(f"data.{name} 必须为字符串列表。")
    data.setdefault("extensions", {})
    if not isinstance(data["extensions"], dict):
        raise _invalid("data.extensions 必须为对象。")
    if "character_book" in data:
        book = data["character_book"]
        if not isinstance(book, dict) or not isinstance(book.get("entries", []), list):
            raise _invalid("character_book 必须为对象，entries 必须为列表。")
    for name in ("creation_date", "modification_date"):
        if name in data and (type(data[name]) is not int or data[name] < 0):
            raise _invalid(f"data.{name} 必须为非负 Unix 秒时间戳。")
    if "assets" in data and (
        not isinstance(data["assets"], list)
        or any(not isinstance(asset, dict) for asset in data["assets"])
    ):
        raise _invalid("data.assets 必须为对象列表。")
    if "nickname" in data and not isinstance(data["nickname"], str):
        raise _invalid("data.nickname 必须为字符串。")
    if "creator_notes_multilingual" in data and (
        not isinstance(data["creator_notes_multilingual"], dict)
        or any(
            not isinstance(v, str) for v in data["creator_notes_multilingual"].values()
        )
    ):
        raise _invalid("data.creator_notes_multilingual 必须为语言到字符串的映射。")
    card.update(spec="chara_card_v3", spec_version="3.0")
    if len(canonical(card).encode("utf-8")) > MAX_CARD_BYTES:
        raise StudioError("limit_exceeded", "转换后的角色卡 JSON 最大为 2 MiB。")
    return card


def new_card(name: str = "新角色") -> dict:
    card = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {"name": name},
    }
    return normalize_card(card)


def from_persona(persona: dict) -> dict:
    """Represent an existing core persona without inferring character metadata."""
    dialogs = persona.get("begin_dialogs")
    if not isinstance(persona.get("system_prompt"), str) or (
        dialogs is not None
        and (
            not isinstance(dialogs, list)
            or any(not isinstance(v, str) for v in dialogs)
        )
    ):
        raise _invalid("AstrBot 人格提示词必须为字符串，预设对话必须为字符串列表。")
    examples = "\n".join(
        ("{{user}}" if i % 2 == 0 else "{{char}}") + ": " + text
        for i, text in enumerate(dialogs or [])
    )
    card = new_card(persona["persona_id"])
    card["data"].update(
        system_prompt=persona["system_prompt"],
        mes_example=examples,
        extensions={
            PERSONA_EXTENSION: {
                "persona_import": {
                    "persona": copy.deepcopy(persona),
                    "mes_example": examples,
                }
            }
        },
    )
    return normalize_card(card)


def persona_origin(card: dict) -> dict | None:
    extension = card["data"].get("extensions", {}).get(PERSONA_EXTENSION)
    origin = extension.get("persona_import") if isinstance(extension, dict) else None
    if not isinstance(origin, dict) or not isinstance(origin.get("mes_example"), str):
        return None
    persona = origin.get("persona")
    if not isinstance(persona, dict) or not isinstance(
        persona.get("system_prompt"), str
    ):
        return None
    dialogs = persona.get("begin_dialogs")
    if dialogs is not None and (
        not isinstance(dialogs, list) or any(not isinstance(v, str) for v in dialogs)
    ):
        return None
    return origin


def _inflate(raw: bytes, limit: int = MAX_CARD_BYTES * 2) -> bytes:
    try:
        decoder = zlib.decompressobj()
        value = decoder.decompress(raw, limit + 1)
        if len(value) > limit or not decoder.eof:
            raise _invalid("PNG 文本数据过大或压缩内容不完整。")
        return value
    except zlib.error as exc:
        raise _invalid("PNG 文本压缩内容损坏。") from exc


def png_chunks(raw: bytes) -> list[tuple[bytes, bytes]]:
    if len(raw) > MAX_UPLOAD_BYTES:
        raise StudioError("limit_exceeded", "PNG 文件最大为 20 MiB。")
    if not raw.startswith(PNG_SIGNATURE):
        raise _invalid("文件不是 PNG 图片。")
    chunks, offset = [], 8
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise _invalid("PNG 数据被截断。")
        size = struct.unpack_from(">I", raw, offset)[0]
        kind = raw[offset + 4 : offset + 8]
        end = offset + 8 + size
        if end + 4 > len(raw):
            raise _invalid("PNG 数据块长度无效。")
        payload = raw[offset + 8 : end]
        if (
            zlib.crc32(kind + payload) & 0xFFFFFFFF
            != struct.unpack_from(">I", raw, end)[0]
        ):
            raise _invalid("PNG 数据块校验失败。")
        chunks.append((kind, payload))
        offset = end + 4
        if kind == b"IEND":
            break
    if (
        not chunks
        or chunks[0][0] != b"IHDR"
        or len(chunks[0][1]) != 13
        or chunks[-1] != (b"IEND", b"")
        or not any(k == b"IDAT" for k, _ in chunks)
        or offset != len(raw)
    ):
        raise _invalid("PNG 缺少必要数据块，或包含尾随数据。")
    width, height = struct.unpack_from(">II", chunks[0][1])
    if (
        not width
        or not height
        or max(width, height) > 8192
        or width * height > 32_000_000
    ):
        raise _invalid("PNG 尺寸不能超过 8192 边长或 3200 万像素。")
    return chunks


def _png_text(
    kind: bytes, payload: bytes, *, decode_asset: bool = False
) -> tuple[bytes, bytes] | None:
    if kind not in (b"tEXt", b"zTXt", b"iTXt") or b"\0" not in payload:
        return None
    key, text = payload.split(b"\0", 1)
    if key not in (b"chara", b"ccv3") and not (
        decode_asset and key.startswith(b"chara-ext-asset_:")
    ):
        return key, b""
    limit = (
        (MAX_UPLOAD_BYTES + 2) // 3 * 4
        if decode_asset and key.startswith(b"chara-ext-asset_:")
        else MAX_CARD_BYTES * 2
    )
    if kind == b"zTXt":
        if not text or text[0] != 0:
            raise _invalid("PNG zTXt 压缩方式无效。")
        text = _inflate(text[1:], limit)
    elif kind == b"iTXt":
        if len(text) < 4 or text[0] not in (0, 1) or text[1] != 0:
            raise _invalid("PNG iTXt 格式无效。")
        parts = text[2:].split(b"\0", 2)
        if len(parts) != 3:
            raise _invalid("PNG iTXt 格式无效。")
        text = _inflate(parts[2], limit) if text[0] else parts[2]
    return key, text


def import_card(raw: bytes) -> tuple[dict, bytes | None]:
    if len(raw) > MAX_UPLOAD_BYTES:
        raise StudioError("limit_exceeded", "上传文件最大为 20 MiB。")
    if not raw.startswith(PNG_SIGNATURE):
        return load_json(raw), None
    candidates = {}
    for kind, payload in png_chunks(raw):
        item = _png_text(kind, payload)
        if item and item[0] in (b"chara", b"ccv3"):
            if item[0] in candidates:
                raise _invalid("PNG 包含重复的角色卡数据块。")
            candidates[item[0]] = item[1]
    encoded = candidates.get(b"ccv3", candidates.get(b"chara"))
    if encoded is None:
        raise _invalid("PNG 中未找到 chara 或 ccv3 角色卡。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise _invalid("PNG 角色卡 Base64 数据无效。") from exc
    return load_json(data), raw


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def blank_png() -> bytes:
    width = height = 128
    pixels = (b"\0" + bytes((108, 88, 180, 255)) * width) * height
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(pixels))
        + _chunk(b"IEND", b"")
    )


def export_png(
    card: dict, image: bytes | None = None, *, resource_image: bytes | None = None
) -> bytes:
    card = normalize_card(card)
    chunks = png_chunks(image or blank_png())
    resources = []
    if resource_image and resource_image != image:
        resources = [
            (kind, payload)
            for kind, payload in png_chunks(resource_image)
            if kind in (b"tEXt", b"zTXt", b"iTXt")
            and payload.split(b"\0", 1)[0].startswith(b"chara-ext-asset_:")
        ]
        # __asset: references belong to the imported container, not the new icon.
        chunks = [
            (kind, payload)
            for kind, payload in chunks
            if not (
                kind in (b"tEXt", b"zTXt", b"iTXt")
                and payload.split(b"\0", 1)[0].startswith(b"chara-ext-asset_:")
            )
        ]
    result = bytearray(PNG_SIGNATURE)
    for kind, payload in chunks:
        text = _png_text(kind, payload)
        if text and text[0] in (b"chara", b"ccv3"):
            continue
        if kind == b"IEND":
            for resource_kind, resource_payload in resources:
                result.extend(_chunk(resource_kind, resource_payload))
            result.extend(
                _chunk(
                    b"tEXt",
                    b"ccv3\0" + base64.b64encode(canonical(card).encode("utf-8")),
                )
            )
        result.extend(_chunk(kind, payload))
    if len(result) > MAX_UPLOAD_BYTES:
        raise StudioError(
            "limit_exceeded", "导出的 PNG 超过 20 MiB，请改用 JSON 导出。"
        )
    return bytes(result)


def render_macros(text: str, char: str, user: str = "用户", original: str = "") -> str:
    values = {"char": char, "user": user, "original": original}
    return re.sub(r"\{\{(char|user|original)\}\}", lambda m: values[m[1]], text)


def parse_examples(text: str, char: str) -> tuple[list[list[dict]], bool]:
    """Preserve independent example blocks and non-paired messages."""
    result, unparsed = [], False
    for block in re.split(r"(?im)^\s*<START>\s*$", text):
        if not block.strip():
            continue
        messages = []
        marker = re.compile(
            r"(?m)^\s*(\{\{user\}\}|\{\{char\}\}|"
            + re.escape(char)
            + r"|You|User|Assistant)\s*:\s*",
            re.IGNORECASE,
        )
        matches = list(marker.finditer(block))
        if not matches or block[: matches[0].start()].strip():
            unparsed = True
            continue
        for i, match in enumerate(matches):
            speaker = match[1].lower()
            content = block[
                match.end() : matches[i + 1].start()
                if i + 1 < len(matches)
                else len(block)
            ].strip()
            messages.append(
                {
                    "role": "user"
                    if speaker in ("{{user}}", "you", "user")
                    else "assistant",
                    "content": content,
                }
            )
        result.append(messages)
    return result, unparsed


def compile_card(card: dict) -> dict:
    data = card["data"]
    origin = persona_origin(card)
    literal_prompt = bool(
        origin and data["system_prompt"] == origin["persona"]["system_prompt"]
    )
    instruction = (
        data["system_prompt"]
        if literal_prompt
        else data["system_prompt"] or DEFAULT_INSTRUCTION
    )
    if not literal_prompt:
        instruction = render_macros(
            instruction, data["name"], "{{user}}", DEFAULT_INSTRUCTION
        )
        # Expand the default inserted by {{original}} once, without evaluating code.
        instruction = render_macros(instruction, data["name"], "{{user}}")
    parts = [] if origin else [("角色指令", instruction), ("角色名称", data["name"])]
    parts.extend(
        (label, data[field])
        for label, field in (
            ("角色描述", "description"),
            ("性格", "personality"),
            ("场景", "scenario"),
        )
    )
    prompt = "\n\n".join(
        f"## {label}\n{value}" for label, value in parts if value.strip()
    )
    if origin:
        prompt = instruction + ("\n\n" + prompt if prompt else "")
    native_dialogs = bool(origin and data["mes_example"] == origin["mes_example"])
    if native_dialogs:
        # Core treats null dialogs as empty; retain the original null in the extension.
        begin_dialogs = copy.deepcopy(origin["persona"].get("begin_dialogs") or [])
        examples, unparsed = [], False
    else:
        begin_dialogs = []
        examples, unparsed = parse_examples(data["mes_example"], data["name"])
    warnings = []
    if len(begin_dialogs) % 2:
        warnings.append(
            "原始 AstrBot 预设对话为奇数条，已原样保留；核心会忽略这些预设。"
        )
    if unparsed:
        prompt += "\n\n## 示例对话原文\n" + data["mes_example"]
        examples = []
        warnings.append("示例对话包含非标准段落，已保留原文并放入人格提示词。")
    warnings.extend(book_warnings(data.get("character_book")))
    if any(key != PERSONA_EXTENSION for key in data.get("extensions", {})) or (
        data.get("extensions") and not origin
    ):
        warnings.append("扩展字段已保留，不执行酒馆脚本、正则或扩展指令。")
    if data.get("assets"):
        warnings.append(
            "主头像按 assets 的 icon/main 声明解析；其他资源原样保留，暂不参与对话。"
        )
    known = {"char", "user", "original"}
    macros = set(re.findall(r"\{\{([^{}]+)\}\}", canonical(data))) - known
    if macros:
        warnings.append("未实现的宏将原样保留：" + "、".join(sorted(macros)[:20]))
    return {
        "system_prompt": prompt,
        "begin_dialogs": begin_dialogs,
        "examples": examples,
        "literal_system_prompt": literal_prompt,
        "persona_import": origin is not None,
        "warnings": warnings,
    }
