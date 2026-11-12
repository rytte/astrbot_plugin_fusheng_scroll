import copy

import pytest
from astrbot_plugin_fusheng_scroll.worldbook import (
    HEADER,
    MAX_SCAN_CHARS,
    NAMESPACE,
    book_settings,
    book_warnings,
    evaluate_book,
)


def entry(**patch):
    return {"keys": ["白塔", "Tower"], "content": "白塔属于法师协会。", **patch}


def book(*entries, **patch):
    return {"entries": list(entries), **patch}


def test_keyword_case_macros_and_original_data_are_preserved():
    source = book(entry(content="{{char}} 认识 {{user}}", extensions={"custom": [1]}))
    before = copy.deepcopy(source)
    result = evaluate_book(
        source,
        "visit the tower",
        render=lambda t: t.replace("{{char}}", "爱莉").replace("{{user}}", "访客"),
    )
    assert result["entries"][0]["matched_keys"] == ["Tower"]
    assert result["after"] == HEADER + "爱莉 认识 访客"
    assert source == before
    source["entries"][0]["case_sensitive"] = True
    assert not evaluate_book(source, "tower")["after"]
    assert evaluate_book(source, "Tower")["after"]


def test_scan_depth_current_message_and_real_multimodal_history_only():
    source = book(entry(), scan_depth=1)
    history = [
        {"role": "user", "content": "白塔"},
        {"role": "assistant", "content": [{"type": "text", "text": "现在去花园"}]},
        {"role": "tool", "content": "白塔"},
        {"role": "system", "content": "白塔"},
        {"role": "assistant", "content": "白塔", "_no_save": True},
    ]
    assert not evaluate_book(source, "你好", history)["after"]
    source["scan_depth"] = 2
    assert evaluate_book(source, "你好", history)["after"]
    source["scan_depth"] = 0
    assert not evaluate_book(source, "你好", history)["after"]
    assert evaluate_book(source, "白塔", history)["after"]
    assert not evaluate_book(source, "白", [{"role": "user", "content": "塔"}])["after"]


def test_constant_selective_disabled_and_no_recursive_activation():
    source = book(
        entry(constant=True, content="花园"),
        entry(keys=["花园"], content="不递归"),
        entry(selective=True, secondary_keys=["协会"], content="辅助条件"),
        entry(constant=True, enabled=False, content="已停用"),
    )
    result = evaluate_book(source, "去白塔")
    assert [r["status"] for r in result["entries"]] == [
        "included",
        "unmatched",
        "unmatched",
        "disabled",
    ]
    assert "未命中辅助关键词" in result["entries"][2]["reason"]
    assert "辅助条件" in evaluate_book(source, "去白塔协会")["after"]
    source["extensions"] = {NAMESPACE: {"enabled": False}}
    assert not evaluate_book(source, "白塔协会")["after"]


def test_budget_selection_is_whole_entry_priority_then_insertion_order():
    source = book(
        entry(constant=True, content="低优先级", insertion_order=1),
        entry(constant=True, content="优先", priority=10, insertion_order=10),
        entry(constant=True, content="短", priority=1, insertion_order=5),
        extensions={NAMESPACE: {"max_chars": len(HEADER) + 5}},
    )
    result = evaluate_book(source)
    assert [r["status"] for r in result["entries"]] == [
        "budget",
        "included",
        "included",
    ]
    assert result["after"] == HEADER + "短\n\n优先"
    assert result["used_chars"] == len(result["after"]) == result["max_chars"]
    source["extensions"][NAMESPACE]["max_chars"] = 0
    assert evaluate_book(source)["used_chars"] == 0


def test_before_after_shared_budget_and_sillytavern_position_compatibility():
    source = book(
        entry(constant=True, content="前", extensions={"position": 0}),
        entry(constant=True, content="后", position="after_char"),
        extensions={NAMESPACE: {"max_chars": 2 * len(HEADER) + 2}},
    )
    result = evaluate_book(source)
    assert result["before"] == HEADER + "前"
    assert result["after"] == HEADER + "后"
    assert result["used_chars"] == len(result["before"]) + len(result["after"])


@pytest.mark.parametrize(
    "patch",
    [
        {"extensions": {"use_regex": True}},
        {"use_regex": True},
        {"content": "@@activate\n带有高级规则的正文"},
        {"extensions": {"probability": 50}},
        {"extensions": {"position": 4, "depth": 2}},
        {"extensions": {"cooldown": 3}},
        {"extensions": {"group": "只选一个"}},
        {"extensions": {"selectiveLogic": 2}},
        {"position": "at_depth"},
    ],
)
def test_unsupported_rules_are_retained_and_explicitly_skipped(patch):
    source = book(entry(constant=True, **patch))
    before = copy.deepcopy(source)
    result = evaluate_book(source, "白塔")
    assert result["entries"][0]["status"] == "unsupported"
    assert result["warnings"] and not result["after"]
    assert source == before


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        1,
        "text",
        {"content": []},
        {"content": "正文", "keys": "白塔"},
        {"content": "正文", "enabled": "yes"},
        {"content": "正文", "extensions": []},
    ],
)
def test_invalid_imported_entries_cannot_break_chat(bad):
    result = evaluate_book(book(bad, entry(constant=True)), "白塔")
    assert result["entries"][0]["status"] == "invalid"
    assert result["entries"][1]["status"] == "included"
    assert result["warnings"]


def test_scan_text_is_bounded_and_retains_newest_input():
    result = evaluate_book(
        book(entry()),
        "x" * MAX_SCAN_CHARS + "白塔",
        [{"role": "user", "content": "旧"}],
    )
    assert result["scan_truncated"] and result["entries"][0]["status"] == "included"


def test_invalid_settings_defaults_and_compatibility_warnings():
    source = book(
        scan_depth=-1,
        token_budget=100,
        recursive_scanning=True,
        extensions={NAMESPACE: {"max_chars": -1}},
    )
    assert book_settings(source) == {
        "enabled": True,
        "scan_depth": 4,
        "max_chars": 4000,
    }
    assert len(book_warnings(source)) == 4
    assert evaluate_book(None)["entries"] == []


def test_entry_limit_and_rendered_content_budget():
    source = book(
        *[entry(constant=True, content="{{char}}") for _ in range(501)],
        extensions={NAMESPACE: {"max_chars": len(HEADER) + 10}},
    )
    result = evaluate_book(
        source, render=lambda t: t.replace("{{char}}", "角色长名称" * 2)
    )
    assert len(result["entries"]) == 500
    assert result["used_chars"] == len(HEADER) + 10
    assert len([e for e in result["entries"] if e["status"] == "included"]) == 1
    assert any("500" in w for w in result["warnings"])


def test_invalid_book_toggle_uses_visible_default_and_empty_render_is_not_injected():
    source = book(entry(constant=True), extensions={NAMESPACE: {"enabled": "bad"}})
    result = evaluate_book(source)
    assert result["enabled"] and result["after"] and result["warnings"]
    result = evaluate_book(source, render=lambda text: "")
    assert not result["after"] and result["entries"][0]["status"] == "empty"
