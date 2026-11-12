import io
from types import SimpleNamespace

import pytest
from astrbot_plugin_fusheng_scroll.core.domain import Settings
from astrbot_plugin_fusheng_scroll.presentation import rendering
from astrbot_plugin_fusheng_scroll.presentation.templates import (
    content_html,
    prompt_html,
)


@pytest.fixture
def markdown_persona():
    return {
        "persona": {
            "persona_id": "澪 · Markdown 展示",
            "system_prompt": "## 角色设定\n第一行\n第二行\n\n**温柔**，但不失原则。",
            "begin_dialogs": [
                "### 用户示例\n你好\n今天好吗？",
                "> 助手示例\n\n当然，记得保持 *好奇心*。",
            ],
            "tools": None,
            "skills": [],
        },
        "revision": 3,
    }


@pytest.mark.parametrize(
    "source, expected",
    [
        ("# 一级标题", "<h1>一级标题</h1>"),
        ("## 二级标题", "<h2>二级标题</h2>"),
        ("###### 六级标题", "<h6>六级标题</h6>"),
        ("**粗体**与*斜体*", "<strong>粗体</strong>与<em>斜体</em>"),
        ("~~删除线~~", "<s>删除线</s>"),
        ("- 条目一\n- 条目二", "<ul>\n<li>条目一</li>\n<li>条目二</li>\n</ul>"),
        ("1. 条目一\n2. 条目二", "<ol>\n<li>条目一</li>\n<li>条目二</li>\n</ol>"),
        ("> 引用内容", "<blockquote>\n<p>引用内容</p>\n</blockquote>"),
        ("---", "<hr />"),
        ("使用 `工具`", "使用 <code>工具</code>"),
        ("```python\nprint('你好')\n```", '<pre><code class="language-python">'),
        ("[参考](https://example.com)", '<a href="https://example.com">参考</a>'),
        ("| 规则 | 内容 |\n| --- | --- |\n| 风格 | 简洁 |", "<table>"),
        ("| 规则 | 内容 |\n| --- | --- |\n| 风格 | 简洁 |", "<td>简洁</td>"),
    ],
)
def test_prompt_renders_common_markdown(source, expected):
    assert expected in prompt_html(source)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_markdown_preserves_single_newlines(newline):
    assert prompt_html(newline.join(["第一行", "第二行", "第三行"])) == (
        "<p>第一行<br />\n第二行<br />\n第三行</p>\n"
    )
    assert prompt_html(newline.join(["第一段", "", "第二段"])) == (
        "<p>第一段</p>\n<p>第二段</p>\n"
    )


def test_code_block_preserves_whitespace_without_parsing_contents():
    source = "```text\n【不是标题】\n  **不是粗体**\n\n    缩进\n```"
    fragment = prompt_html(source)
    assert "【不是标题】\n  **不是粗体**\n\n    缩进\n" in fragment
    assert "prompt-heading" not in fragment and "<strong>" not in fragment
    assert "<br" not in fragment


@pytest.mark.parametrize(
    "source", ["这里有【小标题】", "【小标题】 *正文*", "**【小标题】**"]
)
def test_only_standalone_plain_bracket_headings_get_extra_style(source):
    assert "prompt-heading" not in prompt_html(source)


@pytest.mark.parametrize(
    "source",
    [
        "<script>window.personaUnsafe = true</script>",
        '<img src="file:///private.txt" onerror="alert(1)">',
        '<iframe src="https://example.com"></iframe>',
        "<style>body{display:none}</style>",
    ],
)
def test_markdown_escapes_raw_html(source):
    fragment = prompt_html(source)
    assert "&lt;" in fragment
    assert not any(tag in fragment for tag in ["<script", "<img", "<iframe", "<style"])


@pytest.mark.parametrize(
    "target", ["https://example.com/p.png", "data:image/png;base64,eA=="]
)
def test_markdown_images_are_labels_without_resource_urls(target):
    fragment = prompt_html(f"![**人物** <script>]({target})")
    assert "图片未加载：人物 &lt;script&gt;" in fragment
    assert "<img" not in fragment and "src=" not in fragment and target not in fragment


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "vbscript:alert(1)",
        "file:///private.txt",
        "data:text/html,test",
    ],
)
def test_markdown_rejects_unsafe_link_schemes(target):
    assert "href=" not in prompt_html(f"[点击]({target})")


def test_markdown_does_not_parse_local_file_images():
    fragment = prompt_html("![本地图片](file:///private.txt)")
    assert "<img" not in fragment and "src=" not in fragment
    assert "![本地图片](file:///private.txt)" in fragment


def test_prompt_and_each_dialog_render_markdown_without_changing_exports(
    markdown_persona,
):
    persona = markdown_persona["persona"]
    document = content_html(markdown_persona, 1000)
    assert "<h2>角色设定</h2>" in document
    assert "第一行<br />\n第二行" in document
    assert "<h3>用户示例</h3>" in document
    assert "你好<br />\n今天好吗？" in document
    assert "<blockquote>" in document and "<em>好奇心</em>" in document
    markdown = rendering.detail_markdown(markdown_persona)
    assert persona["system_prompt"] in markdown
    assert all(dialog in markdown for dialog in persona["begin_dialogs"])
    sections = rendering.text_sections(markdown_persona, "prompt")
    assert [text for _, text in sections] == [
        persona["system_prompt"],
        *persona["begin_dialogs"],
    ]


def long_markdown(case):
    if case == "paragraph":
        return "长段落必须按文字行分页，不能把整个段落当成不可拆分的块。" * 220
    if case == "code":
        return (
            "```text\n"
            + "\n".join(
                f"第 {index + 1} 行：" + "code_without_spaces_" * 8
                for index in range(45)
            )
            + "\n```"
        )
    if case == "tall-row":
        return (
            "| 规则 | 说明 |\n| --- | --- |\n| 长内容 | "
            + "这一行超过单页，仍须完整输出。" * 180
            + " |"
        )
    return (
        "# 澪的角色设定\n\n"
        "保持 **温柔**，同时坚持 *清晰的边界*。\n"
        "这次回车仍然换行。\n\n"
        "## 对话原则\n\n"
        "- 尊重用户的表达\n  - 理解真实意图\n  - 不捏造事实\n"
        "- 使用 `persona_manage`，而不是复述历史\n\n"
        "> 保持耐心。\n> 需要澄清时，先提出问题。\n\n"
        "---\n\n## 能力说明\n\n"
        "| 场景 | 推荐行为 | 注意事项 |\n| --- | --- | --- |\n"
        + "\n".join(
            f"| 场景 {index + 1} | 优先询问与确认 | " + "LongIdentifier_" * 5 + " |"
            for index in range(14)
        )
        + "\n\n## 输出示例\n\n```text\n"
        + "\n".join(
            f"第 {index + 1} 行：完整保留代码块内容与换行。" for index in range(35)
        )
        + "\n```\n\n【说话风格】\n保持自然，避免生硬的模板。\n\n"
        "![人物](https://example.com/portrait.png)\n"
        "<script>window.personaUnsafe = true</script>\n"
    )


MEASURE_CONTENT = """async ({height}) => {
    await document.fonts.ready;
    const sheet = document.querySelector('[data-sheet="content"]');
    const flow = sheet.querySelector('.flow');
    const capacity = () => height - (sheet.getBoundingClientRect().height - flow.getBoundingClientRect().height);
    const origin = flow.getBoundingClientRect();
    const lines = [];
    for (const prose of flow.querySelectorAll('[data-text]')) {
        const walker = document.createTreeWalker(prose, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            if (!node.textContent.trim()) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            for (const rect of range.getClientRects()) {
                if (rect.width > 0 && rect.height > 0) lines.push({top: rect.top - origin.top, bottom: rect.bottom - origin.top});
            }
        }
    }
    lines.sort((first, second) => first.top - second.top);
    const blocks = [...flow.querySelectorAll('[data-heading], [data-prompt-heading], .prose :is(h1,h2,h3,h4,h5,h6)')].map(element => {
        const rect = element.getBoundingClientRect();
        const next = lines.find(line => line.top >= rect.bottom - origin.top - 1);
        return {top: rect.top - origin.top, bottom: Math.max(rect.bottom - origin.top, next ? next.bottom : 0)};
    });
    blocks.push(...[...flow.querySelectorAll('.prose tr, .prose pre')].map(element => {
        const rect = element.getBoundingClientRect();
        return {top: rect.top - origin.top, bottom: rect.bottom - origin.top};
    }));
    const firstCapacity = capacity();
    sheet.classList.add('continuation');
    const continuationCapacity = capacity();
    sheet.classList.remove('continuation');
    const heading = flow.querySelector('.prose h2');
    return {
        lines, blocks, firstCapacity, continuationCapacity, height: origin.height,
        promptText: flow.querySelector('[data-text]').innerText,
        heading: heading ? {display: getComputedStyle(heading).display, fontSize: getComputedStyle(heading).fontSize, before: getComputedStyle(heading, '::before').content} : null,
        overflow: [...flow.querySelectorAll('.prose *')].filter(element => {
            const rect = element.getBoundingClientRect();
            return rect.right > origin.right - 27 || rect.left < origin.left + 27;
        }).map(element => element.tagName),
        activeElements: flow.querySelectorAll('script, img, iframe').length,
        unsafe: typeof window.personaUnsafe
    };
}"""


@pytest.mark.parametrize("case", ["mixed", "paragraph", "code", "tall-row"])
@pytest.mark.parametrize("width, page_height", [(1000, 900), (1000, 1500), (480, 900)])
async def test_real_browser_markdown_pagination(
    case, width, page_height, markdown_persona, tmp_path
):
    from astrbot_plugin_browser.service import BrowserService, find_system_chromium
    from PIL import Image

    executable = find_system_chromium()
    if executable is None:
        pytest.skip("No installed Chromium browser for integration test")
    service = BrowserService(browser_executable=str(executable))
    await service.initialize()
    markdown_persona["persona"]["system_prompt"] = long_markdown(case)
    requests = []
    try:
        async with service.session(
            viewport={"width": width, "height": page_height}, javascript_enabled=False
        ) as page:
            page.on("request", lambda request: requests.append(request.url))
            await page.set_content(content_html(markdown_persona, width))
            measured = await page.evaluate(MEASURE_CONTENT, {"height": page_height})
            assert not measured["overflow"]
            assert measured["activeElements"] == 0 and measured["unsafe"] == "undefined"
            assert (
                "你好\n今天好吗？"
                in await page.locator(".dialog.user .prose").inner_text()
            )
            if case == "mixed":
                assert "这次回车仍然换行。" in measured["promptText"]
                assert measured["heading"] == {
                    "display": "block",
                    "fontSize": "27px",
                    "before": "none",
                }
            layout = await page.evaluate(
                rendering.PAGINATE_CONTENT, {"height": page_height, "maxPages": 20}
            )
            assert "error" not in layout and len(layout["pages"]) > 1
            assert all(
                0 < bounds["height"] <= page_height for bounds in layout["pages"]
            )
            spans = await page.evaluate("""() => [...document.querySelectorAll('[data-sheet="content"]')].map(sheet => {
                const viewport = sheet.querySelector('.flow-window').getBoundingClientRect();
                const flow = sheet.querySelector('.flow').getBoundingClientRect();
                return {start: viewport.top - flow.top, end: viewport.bottom - flow.top, hasHeader: Boolean(sheet.querySelector('.persona-header'))};
            })""")
            assert spans[0]["start"] == 0 and spans[0]["hasHeader"]
            assert all(not span["hasHeader"] for span in spans[1:])
            assert abs(spans[-1]["end"] - measured["height"]) <= 1
            assert all(
                abs(first["end"] - second["start"]) < 0.05
                for first, second in zip(spans, spans[1:])
            )
            protected = measured["lines"] + [
                block
                for block in measured["blocks"]
                if block["bottom"] - block["top"]
                <= min(measured["firstCapacity"], measured["continuationCapacity"])
            ]
            for rect in protected:
                assert any(
                    span["start"] <= rect["top"] + 0.05
                    and span["end"] >= rect["bottom"] - 0.05
                    for span in spans
                ), rect
            assert not requests
        if case == "mixed":
            context = SimpleNamespace(
                get_registered_star=lambda name: SimpleNamespace(
                    activated=True, star_cls=SimpleNamespace(service=service)
                )
            )
            images = await rendering.render_pages(
                context,
                markdown_persona,
                Settings(render_width=width, render_page_height=page_height),
            )
            assert len(images) == len(layout["pages"])
            for index, image in enumerate(images, start=1):
                with Image.open(io.BytesIO(image)) as picture:
                    assert picture.width == width and 0 < picture.height <= page_height
                    assert (
                        picture.width * picture.height <= Settings().render_max_pixels
                    )
                (tmp_path / f"markdown-{index}.png").write_bytes(image)
            print(f"Markdown preview: {tmp_path}")
    finally:
        await service.close()
