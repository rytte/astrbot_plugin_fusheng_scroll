from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from markdown_it import MarkdownIt

PAGE_CSS = """
*{box-sizing:border-box;}
html,body{margin:0;padding:0;background:#f4f3f9;color:#283047;}
body{font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC","Noto Sans CJK SC","WenQuanYi Micro Hei",sans-serif;}
.sheet{width:100%;padding:36px;background:linear-gradient(135deg,#f0edf9 0%,#f7f7fb 45%,#eef4f7 100%);}
.sheet.continuation{padding-top:24px;padding-bottom:24px;}
.continuation .persona-header{display:none;}
.continuation-header{display:none;align-items:center;gap:14px;margin-bottom:18px;font-size:13px;line-height:20px;color:#9a90ac;}
.continuation .continuation-header{display:flex;}
.continuation-header::after{content:"";height:1px;flex:1;background:#e2ddeb;}
.masthead{display:flex;align-items:center;gap:12px;margin-bottom:24px;}
.brand-mark{display:grid;place-items:center;width:42px;height:42px;border-radius:13px;background:#7561b9;color:white;font-size:17px;font-weight:700;letter-spacing:1px;}
.brand-name{font-size:17px;font-weight:700;color:#4d436b;}
.brand-subtitle{margin-top:3px;font-size:11px;letter-spacing:2px;color:#8e86a2;}
.hero{margin-bottom:28px;}
.eyebrow{font-size:13px;letter-spacing:2px;color:#8b7ba6;margin-bottom:10px;}
h1{margin:0;font-size:38px;font-weight:700;line-height:1.35;letter-spacing:.5px;overflow-wrap:anywhere;}
.hero-meta{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px;align-items:center;}
.pill{padding:7px 12px;border-radius:9px;background:#e9e3f4;color:#72539c;font-size:14px;line-height:20px;}
.pill.open{background:#e1eeea;color:#46796d;}
.pill.locked{background:#f6e5e7;color:#a65267;}
.hero-note{font-size:14px;color:#8c879b;}
.panel{padding:25px 28px;background:#fff;border:1px solid #e6e3ed;border-radius:18px;box-shadow:0 5px 18px #32255005;}
.panel+.panel{margin-top:18px;}
h2{display:flex;align-items:center;gap:10px;margin:0 0 23px;font-size:20px;line-height:28px;font-weight:700;color:#403652;}
h2::before{content:"";display:block;width:5px;height:20px;border-radius:3px;background:#9d88c0;flex-shrink:0;}
.fields{display:grid;grid-template-columns:1fr 1fr;gap:20px 32px;margin:0;}
.field{min-width:0;}
dt{font-size:13px;line-height:20px;color:#9993a5;margin-bottom:6px;}
dd{margin:0;font-size:18px;line-height:27px;color:#3a3c4d;overflow-wrap:anywhere;white-space:pre-wrap;}
.wide{grid-column:1/-1;}
.capability{margin-top:24px;padding-top:20px;border-top:1px solid #eeecf2;}
.capability-label{font-size:13px;color:#9993a5;margin-bottom:11px;}
.tags{display:flex;flex-wrap:wrap;gap:8px;}
.tag{max-width:100%;padding:7px 11px;background:#f2f0f7;color:#635771;border-radius:8px;font-size:15px;line-height:22px;overflow-wrap:anywhere;white-space:pre-wrap;}
.tag.enabled{background:#eaf3ef;color:#4f7c6b;}
.tag.disabled{background:#f2f3f5;color:#8b8c96;}
.fine-print{margin-top:22px;font-size:13px;line-height:22px;color:#9a94a7;}
.footer{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-top:24px;font-size:12px;line-height:20px;color:#aaa1b8;}
.footer-brand{letter-spacing:1.5px;}
.content-card{padding:20px 0;background:#fff;border:1px solid #e6e3ed;border-radius:18px;overflow:hidden;box-shadow:0 5px 18px #32255005;}
.flow{padding:8px 28px;display:flow-root;}
.flow-window{overflow:hidden;}
.flow section+section{margin-top:32px;padding-top:28px;border-top:1px solid #eeecf2;}
.flow h2{margin-bottom:18px;}
.prose{margin:0;white-space:normal;overflow-wrap:anywhere;word-break:normal;font-family:inherit;font-size:21px;line-height:34px;color:#414355;}
.prose p{margin:0 0 16px;}
.prose :is(h1,h2,h3,h4,h5,h6){display:block;margin:24px 0 12px;line-height:1.5;letter-spacing:0;color:#55436f;}
.prose h1{font-size:30px;}
.prose h2{font-size:27px;}
.prose h2::before{display:none;content:none;}
.prose h3{font-size:24px;}
.prose :is(h4,h5,h6){font-size:21px;}
.prose :is(ul,ol){margin:0 0 16px;padding-left:1.6em;}
.prose li+li{margin-top:6px;}
.prose li>p{margin-bottom:8px;}
.prose li>:is(ul,ol){margin-top:6px;margin-bottom:0;}
.prose li::marker{color:#9784af;}
.prose blockquote{margin:0 0 18px;padding:12px 18px;border-left:4px solid #b19bc9;background:#f7f4fb;border-radius:0 8px 8px 0;color:#70617f;}
.prose blockquote>:last-child{margin-bottom:0;}
.prose code{padding:2px 5px;border-radius:4px;background:#efedf5;color:#725a8d;font-family:"Cascadia Code",Consolas,"SFMono-Regular",monospace;font-size:.86em;box-decoration-break:clone;-webkit-box-decoration-break:clone;}
.prose pre{margin:0 0 18px;padding:16px 18px;border:1px solid #e6e1ef;border-radius:10px;background:#f6f4f9;white-space:pre-wrap;overflow-wrap:anywhere;tab-size:4;}
.prose pre code{padding:0;border-radius:0;background:none;color:#4d455c;white-space:inherit;}
.prose table{width:100%;table-layout:fixed;border-collapse:collapse;margin:0 0 18px;font-size:18px;line-height:29px;}
.prose :is(th,td){padding:10px 12px;border:1px solid #e4dfeb;vertical-align:top;overflow-wrap:anywhere;}
.prose th{background:#eee8f6;color:#66527f;font-weight:700;}
.prose tbody tr:nth-child(even){background:#faf8fc;}
.prose hr{margin:24px 0;border:0;border-top:1px solid #e5dfed;}
.prose a{color:#7860a2;text-decoration:underline;text-underline-offset:3px;}
.prose .image-label{color:#93889f;font-size:.9em;}
.prose>:first-child{margin-top:0;}
.prose>:last-child{margin-bottom:0;}
.prompt-heading{font-weight:700;color:#705a91;}
.dialog+.dialog{margin-top:20px;}
.dialog-role{display:flex;align-items:center;gap:9px;margin-bottom:10px;font-size:14px;line-height:22px;color:#8f819f;}
.role-dot{width:7px;height:7px;background:#b29fcf;border-radius:50%;}
.dialog.assistant .role-dot{background:#8aada3;}
.dialog .prose{padding:15px 18px;background:#f7f5fa;border-radius:11px;}
.dialog.assistant .prose{background:#f1f7f5;}
"""


def document(body: str, width: int) -> str:
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        f"<style>{PAGE_CSS}body{{width:{width}px;}}</style>"
        f"</head><body>{body}</body></html>"
    )


def format_time(value) -> str:
    if value is None:
        return "未提供"
    try:
        if isinstance(value, int | float):
            moment = datetime.fromtimestamp(value, timezone.utc)
        else:
            moment = datetime.fromisoformat(str(value))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (ValueError, OverflowError, OSError):
        return str(value)


def fields_html(fields: list[tuple]) -> str:
    return (
        '<dl class="fields">'
        + "".join(
            f'<div class="field{" wide" if wide else ""}"><dt>{html.escape(label)}</dt>'
            f"<dd>{html.escape(str(value))}</dd></div>"
            for label, value, wide in fields
        )
        + "</dl>"
    )


def header_html(result: dict, kind: str, *, include_metadata: bool = True) -> str:
    persona_id = html.escape(result["persona"]["persona_id"])
    snapshot = result.get("snapshot")
    revision = snapshot["revision"] if snapshot else result.get("revision", "未提供")
    version = f"{'历史快照' if snapshot else '当前版本'} · {revision}"
    locked = result.get("locked", False)
    return (
        '<header class="persona-header"><div class="masthead"><span class="brand-mark">FS</span>'
        '<div><div class="brand-name">浮生绘卷</div>'
        '<div class="brand-subtitle">FUSHENG SCROLL</div></div></div>'
        f'<div class="hero"><div class="eyebrow">{kind}</div><h1>{persona_id}</h1>'
        + (
            f'<div class="hero-meta"><span class="pill">{html.escape(version)}</span>'
            f'<span class="pill {"locked" if locked else "open"}">{"已锁定" if locked else "未锁定"}</span></div>'
            if include_metadata
            else ""
        )
        + "</div></header>"
    )


def metadata_fields(result: dict) -> tuple[list[tuple], list[tuple]]:
    persona = result["persona"]
    owner = result.get("owner")
    exists = result.get("exists", True)
    status = (
        "同名记录已替换"
        if result.get("record_conflict")
        else "存在"
        if exists
        else "已删除"
    )
    version_fields = [
        ("归属用户", owner[1] if owner else "未登记，仅管理员可管理", not owner),
        ("平台实例", owner[0] if owner else "未登记", False),
        ("当前记录状态", status, False),
        ("当前版本", result.get("revision", "未提供"), False),
        (
            "写入保护",
            "已锁定，修改前需人工解锁" if result.get("locked") else "未锁定",
            False,
        ),
    ]
    snapshot = result.get("snapshot")
    summary = result.get("backup_summary")
    actions = {"update": "修改人格", "delete": "删除人格", "restore": "恢复历史"}
    if snapshot:
        version_fields.extend(
            [
                ("展示的快照版本", snapshot["revision"], False),
                ("备份时间", format_time(snapshot["created_at"]), False),
                (
                    "备份动作",
                    actions.get(snapshot["action"], snapshot["action"]),
                    False,
                ),
                ("备份 ID", snapshot["backup_id"], True),
            ]
        )
    elif summary is not None:
        version_fields.append(("保留备份", f"{summary['count']} 份", False))
        if summary.get("latest"):
            latest = summary["latest"]
            version_fields.extend(
                [
                    ("最近备份时间", format_time(latest["created_at"]), False),
                    (
                        "最近备份动作",
                        actions.get(latest["action"], latest["action"]),
                        False,
                    ),
                    ("最近备份 ID", latest["backup_id"], True),
                ]
            )
    persona_fields = [
        ("人格 ID", persona["persona_id"], False),
        ("数据库记录", persona.get("id", "未提供"), False),
        ("创建时间", format_time(persona.get("created_at")), False),
        ("更新时间", format_time(persona.get("updated_at")), False),
        ("所在分组", persona.get("folder_id") or "未分组", False),
        ("排序位置", persona.get("sort_order", "未提供"), False),
        (
            "自定义错误回复",
            persona.get("custom_error_message")
            if persona.get("custom_error_message") is not None
            else "未设置",
            True,
        ),
    ]
    return version_fields, persona_fields


def overview_html(result: dict, width: int) -> str:
    persona = result["persona"]
    version_fields, persona_fields = metadata_fields(result)
    capabilities = []
    for name, label in (("tools", "可用工具"), ("skills", "可用技能")):
        names = persona.get(name)
        if names is None:
            tags = (
                '<span class="tag enabled">全部工具</span>'
                if name == "tools"
                else '<span class="tag enabled">全部技能</span>'
            )
        elif not names:
            tags = '<span class="tag disabled">已禁用</span>'
        else:
            tags = "".join(
                f'<span class="tag">{html.escape(item)}</span>' for item in names
            )
        capabilities.append(
            f'<div class="capability"><div class="capability-label">{label}</div><div class="tags">{tags}</div></div>'
        )
    body = (
        '<main class="sheet" data-sheet="overview">'
        + header_html(result, "人格档案")
        + '<section class="panel"><h2>版本与权限</h2>'
        + fields_html(version_fields)
        + "</section>"
        + '<section class="panel"><h2>人格信息与能力</h2>'
        + fields_html(persona_fields)
        + "".join(capabilities)
        + "</section>"
        + '<div class="fine-print">锁定仅约束本插件；查看与切换不受锁定影响。工具配置不代表执行授权。</div>'
        + '<footer class="footer"><span class="footer-brand">FUSHENG SCROLL</span><span>人格档案 · 独立展示</span></footer></main>'
    )
    return document(body, width)


def render_prompt_text(renderer, tokens, index, options, env) -> str:
    text = tokens[index].content
    starts_line = index == 0 or tokens[index - 1].type in {"softbreak", "hardbreak"}
    ends_line = index == len(tokens) - 1 or tokens[index + 1].type in {
        "softbreak",
        "hardbreak",
    }
    if starts_line and ends_line and re.fullmatch(r"\s*【[^】\n]{1,100}】\s*", text):
        return f'<span class="prompt-heading" data-prompt-heading>{html.escape(text)}</span>'
    return renderer.text(tokens, index, options, env)


def render_image_label(renderer, tokens, index, options, env) -> str:
    label = renderer.renderInlineAsText(tokens[index].children or [], options, env)
    return (
        f'<span class="image-label">[图片未加载：{html.escape(label or "图片")}]</span>'
    )


PROMPT_MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(
    ["table", "strikethrough"]
)
PROMPT_MARKDOWN.add_render_rule("text", render_prompt_text)
PROMPT_MARKDOWN.add_render_rule("image", render_image_label)


def prompt_html(text: str) -> str:
    return PROMPT_MARKDOWN.render(text)


def content_html(result: dict, width: int) -> str:
    persona = result["persona"]
    sections = [
        "<section><h2 data-heading>系统提示词</h2>"
        f'<div class="prose" data-text>{prompt_html(persona["system_prompt"])}</div></section>'
    ]
    dialogs = persona.get("begin_dialogs") or []
    if dialogs:
        dialog_html = []
        for index, text in enumerate(dialogs):
            role = "用户" if index % 2 == 0 else "助手"
            kind = "user" if index % 2 == 0 else "assistant"
            dialog_html.append(
                f'<div class="dialog {kind}"><div class="dialog-role" data-heading><span class="role-dot"></span>第 {index // 2 + 1} 轮 · {role}</div>'
                f'<div class="prose" data-text>{prompt_html(text)}</div></div>'
            )
        sections.append(
            "<section><h2 data-heading>预设对话</h2>"
            + "".join(dialog_html)
            + "</section>"
        )
    body = (
        '<main class="sheet" data-sheet="content">'
        + header_html(result, "人格正文", include_metadata=False)
        + '<header class="continuation-header"><span>接上页</span></header>'
        + '<div class="content-card"><div class="flow-window"><div class="flow">'
        + "".join(sections)
        + "</div></div></div>"
        + '<footer class="footer"><span class="footer-brand">FUSHENG SCROLL</span><span data-page-number>正文</span></footer></main>'
    )
    return document(body, width)
