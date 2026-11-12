from __future__ import annotations

import asyncio
import math
import tempfile
import unicodedata
from pathlib import Path

from astrbot.api.event import MessageChain
from astrbot.api.message_components import File, Image, Node, Nodes, Plain
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from ..core.branding import PLUGIN_NAME
from ..core.domain import StudioError, identify, view_options
from .templates import content_html, metadata_fields, overview_html

FORWARD_NODE_CHARS = 4000
FORWARD_MAX_NODES = 100
FORWARD_PLATFORMS = frozenset({"aiocqhttp", "satori"})
TEXT_MESSAGE_CHARS = 2000

PAGINATE_CONTENT = r"""
async ({height, maxPages}) => {
    await document.fonts.ready;
    const source = document.querySelector('[data-sheet="content"]');
    const flow = source.querySelector('.flow');
    const availableHeight = () => Math.floor(height - (source.getBoundingClientRect().height - flow.getBoundingClientRect().height));
    const firstCapacity = availableHeight();
    source.classList.add('continuation');
    const continuationCapacity = availableHeight();
    source.classList.remove('continuation');
    const bounds = flow.getBoundingClientRect();
    if (firstCapacity <= 0 || continuationCapacity <= 0) return {error: '页面标题与边距超过分页高度，请增加分页高度。'};
    const textLines = [];
    const textRects = element => {
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
        const rects = [];
        let node;
        while ((node = walker.nextNode())) {
            if (!node.textContent.trim()) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            for (const rect of range.getClientRects()) {
                if (rect.width > 0 && rect.height > 0) rects.push(rect);
            }
        }
        return rects;
    };
    for (const element of flow.querySelectorAll('[data-text]')) {
        for (const rect of textRects(element)) {
            textLines.push({top: rect.top - bounds.top, bottom: rect.bottom - bounds.top});
        }
    }
    textLines.sort((first, second) => first.top - second.top);
    const blockRanges = [];
    for (const heading of flow.querySelectorAll('[data-heading], [data-prompt-heading], .prose :is(h1,h2,h3,h4,h5,h6)')) {
        const title = heading.getBoundingClientRect();
        const following = textLines.find(rect => rect.top >= title.bottom - bounds.top - 1);
        blockRanges.push({top: title.top - bounds.top, bottom: Math.max(title.bottom - bounds.top, following ? following.bottom : 0)});
    }
    for (const element of flow.querySelectorAll('.prose tr, .prose pre')) {
        const rect = element.getBoundingClientRect();
        blockRanges.push({top: rect.top - bounds.top, bottom: rect.bottom - bounds.top});
    }
    const spans = [];
    const totalHeight = Math.ceil(bounds.height);
    let start = 0;
    while (start < totalHeight) {
        const capacity = spans.length === 0 ? firstCapacity : continuationCapacity;
        const protectedRanges = textLines.concat(blockRanges.filter(rect => rect.bottom - rect.top <= capacity));
        let end = Math.min(start + capacity, totalHeight);
        if (end < totalHeight) {
            while (true) {
                const crossing = protectedRanges.filter(rect => rect.top < end && rect.bottom > end);
                if (!crossing.length) break;
                end = Math.min(...crossing.map(rect => rect.top));
                if (end <= start) return {error: '页面空间不足以完整显示文字行，请增加分页高度。'};
            }
        }
        spans.push({start, end});
        if (spans.length > maxPages) return {error: '正文页数超过上限；未发送截断图片。'};
        start = end;
    }
    const sheets = spans.map((span, index) => {
        const sheet = source.cloneNode(true);
        if (index > 0) {
            sheet.classList.add('continuation');
            sheet.querySelector('.persona-header').remove();
        }
        sheet.querySelector('.flow-window').style.height = (span.end - span.start) + 'px';
        sheet.querySelector('.flow').style.transform = 'translateY(-' + span.start + 'px)';
        sheet.querySelector('[data-page-number]').textContent = '正文 · ' + (index + 1) + ' / ' + spans.length;
        return sheet;
    });
    document.body.replaceChildren(...sheets);
    return {
        width: document.documentElement.scrollWidth,
        pages: sheets.map(sheet => {
            const rect = sheet.getBoundingClientRect();
            return {top: rect.top, height: Math.ceil(rect.height)};
        })
    };
}
"""


def markdown_filename(result: dict, content: str = "prompt") -> str:
    snapshot = result.get("snapshot")
    revision = snapshot["revision"] if snapshot else result["revision"]
    kind = "历史" if snapshot else "当前"
    prefix = "人格_"
    scope = {"metadata": "_元数据", "prompt": "", "all": "_完整"}[content]
    suffix = f"_{kind}_v{revision}{scope}.md"
    name = "".join(
        "_"
        if character in '<>:"/\\|?*'
        or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        else character
        for character in result["persona"]["persona_id"]
    )
    byte_limit = 200 - len((prefix + suffix).encode("utf-8"))
    name = (
        name.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore").strip(" .")
    )
    return f"{prefix}{name or '未命名'}{suffix}"


def metadata_sections(result: dict) -> list[tuple[str, list[tuple]]]:
    version_fields, persona_fields = metadata_fields(result)
    version_fields.append(
        (
            "权限说明",
            "锁定仅约束本插件；查看与切换不受锁定影响。工具配置不代表执行授权。",
            True,
        )
    )
    for name, label in (("tools", "可用工具"), ("skills", "可用技能")):
        names = result["persona"].get(name)
        value = (
            ("全部工具" if name == "tools" else "全部技能")
            if names is None
            else "\n".join(names)
            if names
            else "已禁用"
        )
        persona_fields.append((label, value, True))
    return [("版本与权限", version_fields), ("人格信息与能力", persona_fields)]


def detail_markdown(result: dict, content: str = "prompt") -> str:
    _, content = view_options({"content": content})
    persona = result["persona"]
    sections = []
    if content in {"metadata", "all"}:
        for title, fields in metadata_sections(result):
            lines = [f"# {title}"]
            for label, value, _ in fields:
                value_text = str(value).replace("\n", "\n  ")
                lines.append(f"- {label}：{value_text}")
            sections.append("\n\n".join(lines))
    if content == "metadata":
        return "\n\n".join(sections) + "\n"
    sections.append(f"# 系统提示词\n\n{persona['system_prompt']}")
    dialogs = persona.get("begin_dialogs") or []
    if dialogs:
        dialog_sections = ["# 预设对话"]
        for index, text in enumerate(dialogs):
            role = "用户" if index % 2 == 0 else "助手"
            dialog_sections.append(f"## 第 {index // 2 + 1} 轮 · {role}\n\n{text}")
        sections.append("\n\n".join(dialog_sections))
    return "\n\n".join(sections) + "\n"


def text_sections(result: dict, content: str) -> list[tuple[str, str]]:
    sections = []
    if content in {"metadata", "all"}:
        sections.extend(
            (title, "\n".join(f"{label}：{value}" for label, value, _ in fields))
            for title, fields in metadata_sections(result)
        )
    if content in {"prompt", "all"}:
        sections.append(("系统提示词", result["persona"]["system_prompt"]))
        for index, text in enumerate(result["persona"].get("begin_dialogs") or []):
            role = "用户" if index % 2 == 0 else "助手"
            sections.append((f"预设对话 · 第 {index // 2 + 1} 轮 · {role}", text))
    return sections


def split_text(text: str, limit: int) -> list[str]:
    if limit <= 0:
        raise StudioError("forward_limit", "转发节点没有足够空间容纳正文。")
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline > start:
                end = newline + 1
        chunks.append(text[start:end])
        start = end
    return chunks or [""]


def forward_message(event, result: dict, content: str) -> tuple[MessageChain, int]:
    _, content = view_options({"content": content})
    bot_id = event.get_self_id()
    if not bot_id:
        raise StudioError("invalid_identity", "缺少机器人账号 ID，不能发送合并转发。")
    nodes = []
    for title, text in text_sections(result, content):
        chunks = split_text(text, FORWARD_NODE_CHARS - len(title) - 32)
        for index, chunk in enumerate(chunks, start=1):
            heading = f"{title}（{index}/{len(chunks)}）" if len(chunks) > 1 else title
            nodes.append(
                Node(
                    name="浮生绘卷",
                    uin=str(bot_id),
                    content=[Plain(f"{heading}\n\n{chunk}")],
                )
            )
            if len(nodes) > FORWARD_MAX_NODES:
                raise StudioError(
                    "forward_limit",
                    f"合并转发超过 {FORWARD_MAX_NODES} 个节点，未发送截断内容；请选择 markdown。",
                )
    return MessageChain([Nodes(nodes)], type="tool_direct_result"), len(nodes)


def plain_messages(result: dict, content: str) -> list[MessageChain]:
    _, content = view_options({"content": content})
    text = "\n\n".join(
        f"{title}\n\n{body}" for title, body in text_sections(result, content)
    )
    return [
        MessageChain([Plain(chunk)], type="tool_direct_result")
        for chunk in split_text(text, TEXT_MESSAGE_CHARS)
    ]


async def render_pages(
    context, result: dict, settings, content: str = "prompt"
) -> list[bytes]:
    _, content = view_options({"content": content})
    registered = context.get_registered_star("astrbot_plugin_browser")
    service = getattr(getattr(registered, "star_cls", None), "service", None)
    if (
        registered is None
        or not registered.activated
        or service is None
        or not service.ready
    ):
        raise StudioError(
            "browser_unavailable",
            "详情图片需要已启用且就绪的 astrbot_plugin_browser，不会自动改发全文。",
        )

    async def render():
        images = []
        async with service.session(
            viewport={
                "width": settings.render_width,
                "height": settings.render_page_height,
            },
            javascript_enabled=False,
            timeout=settings.render_timeout,
        ) as page:
            if content in {"metadata", "all"}:
                await page.set_content(
                    overview_html(result, settings.render_width), wait_until="load"
                )
                overview = await page.evaluate(
                    "async () => { await document.fonts.ready; return {width: document.documentElement.scrollWidth, height: Math.ceil(document.querySelector('.sheet').getBoundingClientRect().height)}; }"
                )
                width, height = overview["width"], overview["height"]
                if (
                    not isinstance(width, int | float)
                    or not isinstance(height, int | float)
                    or not math.isfinite(width)
                    or not math.isfinite(height)
                    or width > settings.render_width
                    or width <= 0
                    or height <= 0
                    or settings.render_width * height > settings.render_max_pixels
                ):
                    raise StudioError(
                        "render_limit",
                        "人格档案尺寸无效或超出单图像素上限；档案不拆页、不截断。",
                    )
                image = await page.screenshot(
                    type="png",
                    animations="disabled",
                    full_page=True,
                    clip={
                        "x": 0,
                        "y": 0,
                        "width": settings.render_width,
                        "height": height,
                    },
                    timeout=settings.render_timeout * 1000,
                )
                if not isinstance(image, bytes) or not image.startswith(
                    b"\x89PNG\r\n\x1a\n"
                ):
                    raise StudioError("render_failed", "浏览器没有返回有效 PNG。")
                images.append(image)
                if content == "metadata":
                    return images
            await page.set_content(
                content_html(result, settings.render_width), wait_until="load"
            )
            layout = await page.evaluate(
                PAGINATE_CONTENT,
                {
                    "height": settings.render_page_height,
                    "maxPages": settings.render_max_pages - len(images),
                },
            )
            if "error" in layout:
                raise StudioError("render_limit", layout["error"])
            width, pages = layout["width"], layout["pages"]
            if (
                not isinstance(width, int | float)
                or not math.isfinite(width)
                or not 0 < width <= settings.render_width
                or not pages
                or len(pages) + len(images) > settings.render_max_pages
            ):
                raise StudioError(
                    "render_limit", "正文尺寸或总页数超出上限，不截断内容。"
                )
            for bounds in pages:
                top, page_height = bounds["top"], bounds["height"]
                if (
                    not isinstance(top, int | float)
                    or not isinstance(page_height, int | float)
                    or not math.isfinite(top)
                    or not math.isfinite(page_height)
                    or top < 0
                    or not 0 < page_height <= settings.render_page_height
                    or settings.render_width * page_height > settings.render_max_pixels
                ):
                    raise StudioError(
                        "render_limit", "正文分页尺寸无效或超过像素上限。"
                    )
                image = await page.screenshot(
                    type="png",
                    animations="disabled",
                    full_page=True,
                    clip={
                        "x": 0,
                        "y": top,
                        "width": settings.render_width,
                        "height": page_height,
                    },
                    timeout=settings.render_timeout * 1000,
                )
                if not isinstance(image, bytes) or not image.startswith(
                    b"\x89PNG\r\n\x1a\n"
                ):
                    raise StudioError("render_failed", "浏览器没有返回有效 PNG。")
                images.append(image)
        return images

    try:
        return await asyncio.wait_for(render(), timeout=settings.render_timeout)
    except StudioError:
        raise
    except asyncio.TimeoutError as exc:
        raise StudioError(
            "render_timeout", "人格详情渲染超时，没有自动重试或改发全文。"
        ) from exc
    except Exception as exc:
        raise StudioError(
            "render_failed", "人格详情图片渲染失败，没有自动重试或改发全文。"
        ) from exc


def delivery_actor(studio, event, result):
    actor = identify(event, studio.settings())
    if not actor.admin and result.get("owner") != actor.owner:
        raise StudioError("not_found_or_forbidden", "发送前权限已改变，停止发送详情。")
    return actor


async def present(
    studio, event, response: dict, *, format: str = "image", content: str = "prompt"
) -> dict:
    result = response["data"]
    submitted = 0
    file_submitted = False
    texts_submitted = 0
    forwards_submitted = 0
    forward_nodes_submitted = 0
    path = None
    sending = False
    outcome = None
    try:
        delivery_actor(studio, event, result)
        format, content = view_options({"format": format, "content": content})
        nodes_count = 0
        if format == "image":
            images = await render_pages(
                studio.context, result, studio.settings(), content
            )
            messages = [
                MessageChain([Image.fromBytes(image)], type="tool_direct_result")
                for image in images
            ]
        elif format == "markdown":
            directory = Path(get_astrbot_temp_path()) / PLUGIN_NAME
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                suffix=".md",
                prefix="persona_",
                dir=directory,
                delete=False,
            ) as stream:
                path = Path(stream.name)
                stream.write(detail_markdown(result, content))
            name = markdown_filename(result, content)
            messages = [
                MessageChain(
                    [File(name=name, file=str(path))], type="tool_direct_result"
                )
            ]
        elif event.get_platform_name() in FORWARD_PLATFORMS:
            message, nodes_count = forward_message(event, result, content)
            messages = [message]
        else:
            messages = plain_messages(result, content)
        for message in messages:
            delivery_actor(studio, event, result)
            sending = True
            await event.send(message)
            sending = False
            if format == "image":
                submitted += 1
            elif format == "markdown":
                file_submitted = True
            elif nodes_count:
                forwards_submitted += 1
                forward_nodes_submitted += nodes_count
            else:
                texts_submitted += 1
        delivery_actor(studio, event, result)
        response["delivery"] = {
            "format": format,
            "content": content,
            "images_submitted": submitted,
            "md_submitted": file_submitted,
            "texts_submitted": texts_submitted,
            "forwards_submitted": forwards_submitted,
            "forward_nodes_submitted": forward_nodes_submitted,
            "status": "submitted_to_platform",
        }
        outcome = response
    except Exception as exc:
        error = (
            exc
            if isinstance(exc, StudioError)
            else StudioError(
                "delivery_failed",
                "详情消息发送失败，可能已有部分消息送达，请勿自动重试。",
            )
        )
        accessible = True
        try:
            delivery_actor(studio, event, result)
        except StudioError as permission_error:
            accessible = False
            error = permission_error
        outcome = {
            "ok": False,
            "error": {
                "code": error.code,
                "message": str(error),
                "details": error.details,
            },
            "delivery": {
                "format": format,
                "content": content,
                "images_submitted": submitted,
                "md_submitted": file_submitted,
                "texts_submitted": texts_submitted,
                "forwards_submitted": forwards_submitted,
                "forward_nodes_submitted": forward_nodes_submitted,
                "failed_message_may_have_been_delivered": sending,
            },
            "retry_safe": False,
        }
        if accessible:
            outcome["data"] = result
    finally:
        if path is not None:
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError:
                if outcome is not None:
                    outcome["ok"] = False
                    outcome["error"] = {
                        "code": "temporary_cleanup_failed",
                        "message": "消息处理结束，但临时文件清理失败，请管理员检查临时目录，不要重试发送。",
                    }
                    outcome["retry_safe"] = False
    return outcome
