"""Bounded, request-local Character Book matching. Never rewrites source data."""

from __future__ import annotations

NAMESPACE = "astrbot_persona_studio"
DEFAULT_SCAN_DEPTH = 4
DEFAULT_MAX_CHARS = 4000
MAX_SCAN_DEPTH = 50
MAX_CHARS = 16000
MAX_SCAN_CHARS = 32000
MAX_ENTRIES = 500
HEADER = "## 世界书\n"


def _integer(value, default, minimum, maximum):
    return value if type(value) is int and minimum <= value <= maximum else default


def book_settings(book):
    book = book if isinstance(book, dict) else {}
    extensions = book.get("extensions")
    extensions = extensions if isinstance(extensions, dict) else {}
    settings = extensions.get(NAMESPACE)
    settings = settings if isinstance(settings, dict) else {}
    return {
        "enabled": settings.get("enabled", True) is not False,
        "scan_depth": _integer(
            book.get("scan_depth"), DEFAULT_SCAN_DEPTH, 0, MAX_SCAN_DEPTH
        ),
        "max_chars": _integer(
            settings.get("max_chars"), DEFAULT_MAX_CHARS, 0, MAX_CHARS
        ),
    }


def _entry(entry, index):
    """Invalid/unsupported entries remain exportable but do not execute."""
    row = {
        "index": index,
        "name": f"条目 {index + 1}",
        "status": "unmatched",
        "reason": "未命中关键词",
        "matched_keys": [],
    }
    if not isinstance(entry, dict):
        return {**row, "status": "invalid", "reason": "条目必须是对象"}
    row["name"] = str(entry.get("name") or entry.get("comment") or row["name"])[:255]
    if entry.get("enabled", True) is False:
        return {**row, "status": "disabled", "reason": "条目已停用"}
    ext = entry.get("extensions", {})
    if not isinstance(ext, dict):
        return {**row, "status": "invalid", "reason": "extensions 必须为对象"}
    for key in ("enabled", "constant", "case_sensitive", "selective", "use_regex"):
        if key in entry and type(entry[key]) is not bool:
            return {**row, "status": "invalid", "reason": f"{key} 必须为布尔值"}
    for key, minimum, maximum in (
        ("priority", -10000, 10000),
        ("insertion_order", -1000000, 1000000),
    ):
        if key in entry and (
            type(entry[key]) is not int or not minimum <= entry[key] <= maximum
        ):
            return {
                **row,
                "status": "invalid",
                "reason": f"{key} 须为 {minimum}～{maximum} 之间的整数",
            }
    if not isinstance(entry.get("content"), str):
        return {**row, "status": "invalid", "reason": "正文必须为字符串"}
    if not entry["content"].strip():
        return {**row, "status": "empty", "reason": "正文为空"}
    keys, secondary = entry.get("keys", []), entry.get("secondary_keys", [])
    for label, values in (("关键词", keys), ("辅助关键词", secondary)):
        if (
            not isinstance(values, list)
            or len(values) > 64
            or any(not isinstance(v, str) or len(v) > 256 for v in values)
        ):
            return {
                **row,
                "status": "invalid",
                "reason": f"{label}须为字符串列表，最多 64 个，每个最多 256 字符",
            }
    position = entry.get("position", "after_char")
    ext_position = ext.get("position")
    if "position" not in entry and type(ext_position) is int and ext_position in (0, 1):
        position = "before_char" if ext_position == 0 else "after_char"
    unsupported = []
    if entry.get("use_regex"):
        unsupported.append("正则关键词")
    if any(line.lstrip().startswith("@@") for line in entry["content"].splitlines()):
        unsupported.append("V3 装饰器")
    if position not in ("before_char", "after_char") or (
        ext_position is not None
        and (type(ext_position) is not int or ext_position not in (0, 1))
    ):
        unsupported.append("插入位置")
    for key in (
        "use_regex",
        "match_whole_words",
        "matchWholeWords",
        "sticky",
        "cooldown",
        "delay",
        "group",
        "scan_depth",
        "scanDepth",
        "delay_until_recursion",
        "exclude_recursion",
        "prevent_recursion",
        "automation_id",
        "triggers",
    ):
        if ext.get(key):
            unsupported.append(key)
    if ext.get("selectiveLogic", 0) not in (None, 0):
        unsupported.append("辅助关键词逻辑")
    if ext.get("useProbability", True) and ext.get("probability", 100) not in (
        None,
        100,
    ):
        unsupported.append("概率触发")
    if ext.get("vectorized"):
        unsupported.append("向量触发")
    if unsupported:
        return {
            **row,
            "status": "unsupported",
            "reason": "暂不支持：" + "、".join(unsupported),
        }
    return {
        **row,
        "position": position,
        "keys": keys,
        "secondary_keys": secondary,
        "constant": entry.get("constant", False),
        "selective": entry.get("selective", False),
        "case_sensitive": entry.get("case_sensitive", ext.get("case_sensitive", False))
        is True,
        "priority": _integer(entry.get("priority"), 0, -10000, 10000),
        "order": _integer(entry.get("insertion_order"), 100, -1000000, 1000000),
        "content": entry["content"],
    }


def book_warnings(book):
    if not isinstance(book, dict):
        return []
    warnings = []
    if book.get("recursive_scanning"):
        warnings.append("世界书递归扫描暂不支持；仅扫描真实对话，不扫描条目正文。")
    if book.get("token_budget") is not None:
        warnings.append("世界书 token_budget 暂不计量；使用人格工坊的字符预算。")
    if "scan_depth" in book and (
        type(book["scan_depth"]) is not int
        or not 0 <= book["scan_depth"] <= MAX_SCAN_DEPTH
    ):
        warnings.append(
            f"世界书 scan_depth 无效；使用默认最近 {DEFAULT_SCAN_DEPTH} 条历史消息。"
        )
    ext = book.get("extensions", {})
    settings = ext.get(NAMESPACE, {}) if isinstance(ext, dict) else {}
    if (
        isinstance(settings, dict)
        and "enabled" in settings
        and type(settings["enabled"]) is not bool
    ):
        warnings.append("世界书 enabled 无效；按默认启用状态运行。")
    if (
        isinstance(settings, dict)
        and "max_chars" in settings
        and (
            type(settings["max_chars"]) is not int
            or not 0 <= settings["max_chars"] <= MAX_CHARS
        )
    ):
        warnings.append(f"世界书字符预算无效；使用默认 {DEFAULT_MAX_CHARS} 字符。")
    entries = book.get("entries", [])
    if not isinstance(entries, list):
        return warnings
    if len(entries) > MAX_ENTRIES:
        warnings.append(f"世界书最多运行前 {MAX_ENTRIES} 条，其余条目仅保留。")
    invalid = [_entry(e, i) for i, e in enumerate(entries[:MAX_ENTRIES])]
    for row in invalid:
        if row["status"] in ("invalid", "unsupported"):
            warnings.append(f"世界书「{row['name']}」未参与对话：{row['reason']}。")
    return warnings


def _message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            and not part.get("_no_save")
        )
    return ""


def scan_messages(prompt, contexts, depth):
    history = []
    for message in reversed(contexts):
        if len(history) >= depth:
            break
        if (
            isinstance(message, dict)
            and message.get("role") in ("user", "assistant")
            and not message.get("_no_save")
        ):
            text = _message_text(message.get("content"))
            if text:
                history.append(text)
    messages = list(reversed(history))
    if isinstance(prompt, str) and prompt:
        messages.append(prompt)
    texts, remaining, truncated = [], MAX_SCAN_CHARS, False
    for text in reversed(messages):
        if len(text) > remaining:
            truncated = True
        if remaining:
            texts.append(text[-remaining:])
            remaining -= min(remaining, len(text))
    return list(reversed(texts)), truncated


def evaluate_book(book, prompt="", contexts=(), *, render=lambda text: text):
    """Return both actual injection and diagnostics using the same selection rules."""
    settings = book_settings(book)
    texts, truncated = scan_messages(prompt, contexts, settings["scan_depth"])
    entries = book.get("entries", []) if isinstance(book, dict) else []
    rows = [_entry(e, i) for i, e in enumerate(entries[:MAX_ENTRIES])]
    candidates = []
    folded = [text.casefold() for text in texts]
    key_matches = {}
    for row in rows:
        if row["status"] != "unmatched":
            continue
        if not settings["enabled"]:
            row.update(status="book_disabled", reason="世界书已停用")
            continue
        if not row["constant"]:
            haystack = texts if row["case_sensitive"] else folded

            def matches(keys):
                matched = []
                for key in keys:
                    key = render(key).strip()
                    needle = key if row["case_sensitive"] else key.casefold()
                    cache_key = (row["case_sensitive"], needle)
                    if cache_key not in key_matches:
                        key_matches[cache_key] = bool(needle) and any(
                            needle in text for text in haystack
                        )
                    if key_matches[cache_key]:
                        matched.append(key)
                return matched

            row["matched_keys"] = matches(row["keys"])
            if not row["matched_keys"]:
                continue
            if row["selective"]:
                row["matched_secondary_keys"] = matches(row["secondary_keys"])
                if not row["matched_secondary_keys"]:
                    row.update(reason="已命中主关键词，未命中辅助关键词")
                    continue
        row["reason"] = (
            "始终生效" if row["constant"] else "命中：" + "、".join(row["matched_keys"])
        )
        row["content"] = render(row["content"]).strip()
        if not row["content"]:
            row.update(status="empty", reason="宏展开后正文为空")
            continue
        if row.get("matched_secondary_keys"):
            row["reason"] += "；辅助命中：" + "、".join(row["matched_secondary_keys"])
        candidates.append(row)
    selected = {"before_char": [], "after_char": []}
    used = 0
    for row in sorted(
        candidates, key=lambda r: (-r["priority"], r["order"], r["index"])
    ):
        group = selected[row["position"]]
        cost = len(row["content"]) + (2 if group else len(HEADER))
        if used + cost > settings["max_chars"]:
            row.update(status="budget", reason=row["reason"] + "；超出字符预算，已跳过")
            continue
        used += cost
        row["status"] = "included"
        group.append(row)

    def section(position):
        group = sorted(selected[position], key=lambda r: (r["order"], r["index"]))
        return HEADER + "\n\n".join(r["content"] for r in group) if group else ""

    return {
        **settings,
        "used_chars": used,
        "scanned_messages": len(texts),
        "scan_truncated": truncated,
        "before": section("before_char"),
        "after": section("after_char"),
        "entries": [
            {
                k: v
                for k, v in row.items()
                if k
                not in (
                    "keys",
                    "secondary_keys",
                    "content",
                    "constant",
                    "selective",
                    "case_sensitive",
                    "priority",
                    "order",
                )
            }
            for row in rows
        ],
        "warnings": book_warnings(book),
    }
