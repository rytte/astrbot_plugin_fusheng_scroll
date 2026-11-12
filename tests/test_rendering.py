import asyncio
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.message_components import Image, Nodes, Plain
from astrbot_plugin_fusheng_scroll.core.domain import Settings, StudioError
from astrbot_plugin_fusheng_scroll.main import PersonaStudioPlugin
from astrbot_plugin_fusheng_scroll.presentation import rendering
from astrbot_plugin_fusheng_scroll.presentation.templates import (
    format_time,
    prompt_html,
)

PNG = b"\x89PNG\r\n\x1a\nimage"


def fake_browser(
    env, *, height=3000, overview_height=1200, ready=True, content="prompt"
):
    measurements = []
    if content in {"metadata", "all"}:
        measurements.append({"width": 1000, "height": overview_height})
    if content in {"prompt", "all"}:
        measurements.append(
            {
                "width": 1000,
                "pages": [
                    {"top": top, "height": min(1500, height - top)}
                    for top in range(0, height, 1500)
                ],
            }
        )
    page = SimpleNamespace(
        set_content=AsyncMock(),
        evaluate=AsyncMock(side_effect=measurements),
        screenshot=AsyncMock(return_value=PNG),
    )
    calls = []

    @asynccontextmanager
    async def session(**kwargs):
        calls.append(kwargs)
        yield page

    service = SimpleNamespace(ready=ready, session=session)
    env.context.get_registered_star = lambda name: SimpleNamespace(
        activated=True, star_cls=SimpleNamespace(service=service)
    )
    return page, calls


async def detail(env):
    response = await env.studio.execute(
        env.event, "manage", "get", {"persona_id": "alice"}
    )
    assert response["ok"]
    return response


@pytest.mark.parametrize(
    "persona_id, expected",
    [
        ("澪", "人格_澪_当前_v3.md"),
        ("Mio 春日", "人格_Mio 春日_当前_v3.md"),
        (
            '澪/测试\\副本:一?二*三"四<五>六|七',
            "人格_澪_测试_副本_一_二_三_四_五_六_七_当前_v3.md",
        ),
        ("澪\x00测试\n副本\x7f", "人格_澪_测试_副本__当前_v3.md"),
        ("CON", "人格_CON_当前_v3.md"),
        ("...", "人格_未命名_当前_v3.md"),
        ("澪\u202e.md", "人格_澪_.md_当前_v3.md"),
    ],
)
def test_markdown_filename_is_readable_and_safe(persona_id, expected):
    result = {"persona": {"persona_id": persona_id}, "revision": 3}
    assert rendering.markdown_filename(result) == expected


@pytest.mark.parametrize("persona_id", ["澪" * 255, "A" * 255, "澪😊" * 100])
@pytest.mark.parametrize("content", ["metadata", "prompt", "all"])
def test_markdown_filename_limits_bytes_without_breaking_unicode(persona_id, content):
    result = {"persona": {"persona_id": persona_id}, "revision": 3}
    name = rendering.markdown_filename(result, content)
    assert len(name.encode("utf-8")) <= 200
    scope = {"metadata": "_元数据", "prompt": "", "all": "_完整"}[content]
    assert name.startswith("人格_") and name.endswith(f"_当前_v3{scope}.md")
    assert "\ufffd" not in name


def test_html_escapes_content_and_md_keeps_it():
    result = {
        "persona": {
            "persona_id": "name",
            "system_prompt": '<script>alert("bad")</script>\n'
            + "\u0060" * 3
            + "unsafe",
            "begin_dialogs": ["user", "assistant"],
            "tools": None,
            "skills": [],
        },
        "owner": ["platform", "user"],
    }
    document = rendering.content_html(result, 1000)
    assert "<script>" not in document and "&lt;script&gt;" in document
    assert "default-src" in document and "src=" not in document
    markdown = rendering.detail_markdown(result)
    assert result["persona"]["system_prompt"] in markdown
    assert "&lt;script&gt;" not in markdown


def test_markdown_only_exports_prompt_and_dialogs():
    result = {
        "persona": {
            "persona_id": "不应导出的人格ID",
            "system_prompt": "# 原始角色设定\n\n**保持原文**",
            "begin_dialogs": ["用户一", "助手一", "用户二", "助手二"],
            "tools": ["不应导出的工具"],
            "skills": ["不应导出的技能"],
            "custom_error_message": "不应导出的错误回复",
            "created_at": "不应导出的创建时间",
            "updated_at": "不应导出的更新时间",
        },
        "owner": ["不应导出的平台", "不应导出的所有者"],
        "revision": 7,
        "locked": True,
        "expected_revision": "不应导出的状态令牌",
        "backup_summary": {"latest": {"backup_id": "不应导出的备份摘要"}},
        "snapshot": {"backup_id": "不应导出的备份ID", "revision": 2},
    }
    assert rendering.detail_markdown(result) == (
        "# 系统提示词\n\n# 原始角色设定\n\n**保持原文**\n\n"
        "# 预设对话\n\n"
        "## 第 1 轮 · 用户\n\n用户一\n\n"
        "## 第 1 轮 · 助手\n\n助手一\n\n"
        "## 第 2 轮 · 用户\n\n用户二\n\n"
        "## 第 2 轮 · 助手\n\n助手二\n"
    )


@pytest.mark.parametrize(
    "prompt",
    [
        "# 原始标题\n\n**粗体**\n- 列表\n",
        "开头\n\n末尾\n\n",
        "首行\r\n第二行\r\n",
        "\u0060" * 3 + "python\nprint('你好')\n" + "\u0060" * 3,
        '<script>alert("原文")</script>',
        "长提示词原文与换行完整保留。\n" * 3000,
    ],
    ids=["markdown", "blank_lines", "crlf", "code_fence", "html", "long_prompt"],
)
def test_markdown_preserves_prompt_text_without_truncation(prompt):
    result = {"persona": {"system_prompt": prompt, "begin_dialogs": []}}
    assert rendering.detail_markdown(result) == f"# 系统提示词\n\n{prompt}\n"


async def test_overview_uses_readable_metadata_without_prompt(created):
    result = (await detail(created))["data"]
    result["persona"]["tools"] = None
    document = rendering.overview_html(result, 1000)
    assert "版本与权限" in document and "人格信息与能力" in document
    assert "归属用户" in document and "平台实例" in document
    assert "创建时间" in document and "更新时间" in document
    assert "全部工具" in document and "已禁用" in document
    assert "版本1" not in document and "系统提示词" not in document
    assert "backup_summary" not in document and "created_at" not in document
    assert "null" not in document


@pytest.mark.parametrize("dialogs", [None, []])
async def test_empty_dialogs_have_no_heading(created, dialogs):
    result = (await detail(created))["data"]
    result["persona"]["begin_dialogs"] = dialogs
    document = rendering.content_html(result, 1000)
    assert "预设对话" not in document
    assert "自定义错误回复" not in document and "null" not in document
    assert "系统提示词" in document and "版本1" in document
    assert rendering.detail_markdown(result) == "# 系统提示词\n\n版本1\n"
    assert rendering.text_sections(result, "prompt") == [("系统提示词", "版本1")]


async def test_content_groups_prompt_and_dialogs(created):
    document = rendering.content_html((await detail(created))["data"], 1000)
    assert document.index("系统提示词") < document.index("预设对话")
    assert "第 1 轮 · 用户" in document and "第 1 轮 · 助手" in document
    assert "你好" in document and "你好呀" in document
    assert "归属用户" not in document and "人格信息与能力" not in document


def test_prompt_heading_style_preserves_text_and_escapes_html():
    prompt = "开头\n\n【说话风格】\n <script>不要执行</script>\n\n结尾\n"
    fragment = prompt_html(prompt)
    assert 'class="prompt-heading"' in fragment
    assert "<script>" not in fragment
    assert (
        '<span class="prompt-heading" data-prompt-heading>【说话风格】</span><br />'
        in fragment
    )
    assert "&lt;script&gt;不要执行&lt;/script&gt;" in fragment
    assert "<p>开头</p>" in fragment and "<p>结尾</p>" in fragment


async def test_overview_escapes_capability_names(created):
    result = (await detail(created))["data"]
    result["persona"]["tools"] = ['<script>alert("bad")</script>']
    document = rendering.overview_html(result, 1000)
    assert "<script>" not in document and "&lt;script&gt;" in document


@pytest.mark.parametrize(
    "value",
    [1791541635.7853906, "2026-10-09T10:27:15.800387", "2026-10-09T18:27:15+08:00"],
)
def test_metadata_times_use_consistent_utc(value):
    assert format_time(value) == "2026-10-09 10:27:15 UTC"


def test_page_height_does_not_require_fixed_line_multiple():
    assert Settings.parse({"render_page_height": 1001}).render_page_height == 1001


async def test_prompt_markdown_sends_only_file(created, tmp_path, monkeypatch):
    page, calls = fake_browser(created)
    monkeypatch.setattr(rendering, "get_astrbot_temp_path", lambda: str(tmp_path))
    captured = []
    captured_names = []

    async def send(message):
        component = message.chain[0]
        if component.type.value == "File":
            captured_names.append(component.name)
            path = await component.get_file()
            captured.append(
                (await asyncio.to_thread(Path(path).read_bytes)).decode("utf-8")
            )

    created.event.send = AsyncMock(side_effect=send)
    response = await rendering.present(
        created.studio, created.event, await detail(created), format="markdown"
    )
    assert response["ok"], response
    assert response["delivery"]["images_submitted"] == 0
    assert response["delivery"]["md_submitted"]
    assert captured_names == ["人格_alice_当前_v1.md"]
    assert captured == [
        "# 系统提示词\n\n版本1\n\n# 预设对话\n\n"
        "## 第 1 轮 · 用户\n\n你好\n\n"
        "## 第 1 轮 · 助手\n\n你好呀\n"
    ]
    assert not list(tmp_path.rglob("*.md"))
    assert not calls
    page.set_content.assert_not_called()
    page.screenshot.assert_not_called()
    assert created.event.send.await_count == 1


async def test_historical_md_uses_snapshot_content_and_revision(
    created, tmp_path, monkeypatch
):
    updated = await created.studio.execute(
        created.event,
        "manage",
        "update",
        {"persona_id": "alice", "system_prompt": "版本2"},
    )
    assert updated["ok"], updated
    response = await created.studio.execute(
        created.event,
        "versions",
        "get",
        {"backup_id": updated["data"]["backup_id"]},
    )
    assert response["ok"], response
    assert response["data"]["revision"] == 2
    assert response["data"]["snapshot"]["revision"] == 1
    fake_browser(created, height=450)
    monkeypatch.setattr(rendering, "get_astrbot_temp_path", lambda: str(tmp_path))
    captured = []

    async def send(message):
        component = message.chain[0]
        if component.type.value == "File":
            path = await component.get_file()
            captured.append(
                (await asyncio.to_thread(Path(path).read_bytes)).decode("utf-8")
            )

    created.event.send = AsyncMock(side_effect=send)
    response = await rendering.present(
        created.studio, created.event, response, format="markdown"
    )
    assert response["ok"] and response["delivery"]["md_submitted"]
    file = created.event.send.await_args_list[-1].args[0].chain[0]
    assert file.type.value == "File"
    assert file.name == "人格_alice_历史_v1.md"
    assert captured == [
        "# 系统提示词\n\n版本1\n\n# 预设对话\n\n"
        "## 第 1 轮 · 用户\n\n你好\n\n"
        "## 第 1 轮 · 助手\n\n你好呀\n"
    ]
    assert not list(tmp_path.rglob("*.md"))


@pytest.mark.parametrize("family", ["manage", "versions"])
async def test_model_get_sends_overview_before_content(created, family):
    payload = {"persona_id": "alice"}
    if family == "versions":
        updated = await created.studio.execute(
            created.event,
            "manage",
            "update",
            {"persona_id": "alice", "system_prompt": "版本2"},
        )
        assert updated["ok"], updated
        payload = {"backup_id": updated["data"]["backup_id"]}
    payload["content"] = "all"
    page, _ = fake_browser(created, height=450, content="all")
    page.screenshot.side_effect = [PNG + b"overview", PNG + b"content"]
    plugin = PersonaStudioPlugin(created.context, created.config)
    plugin.studio = created.studio
    handler = plugin.persona_get if family == "manage" else plugin.persona_backup_get
    response = json.loads(await handler(created.event, **payload))
    assert response["ok"], response
    assert response["delivery"]["images_submitted"] == 2
    assert not response["delivery"]["md_submitted"]
    assert created.event.send.await_count == 2
    messages = [call.args[0] for call in created.event.send.await_args_list]
    assert [message.chain[0].type.value for message in messages] == ["Image", "Image"]
    assert [message.chain[0].file for message in messages] == [
        Image.fromBytes(PNG + b"overview").file,
        Image.fromBytes(PNG + b"content").file,
    ]
    overview = page.set_content.call_args_list[0].args[0]
    content = page.set_content.call_args_list[1].args[0]
    assert 'data-sheet="overview"' in overview
    assert "版本与权限" in overview and "人格信息与能力" in overview
    assert 'data-sheet="content"' in content and "系统提示词" in content


async def test_browser_missing_does_not_send_text(created):
    response = await rendering.present(
        created.studio, created.event, await detail(created)
    )
    assert response["error"]["code"] == "browser_unavailable"
    assert response["data"]["persona"]["system_prompt"] == "版本1"
    created.event.send.assert_not_called()


async def test_page_limit_rejects_without_partial_images(created):
    page, _ = fake_browser(created, height=100000, content="all")
    response = await rendering.present(
        created.studio, created.event, await detail(created), content="all"
    )
    assert response["error"]["code"] == "render_limit"
    created.event.send.assert_not_called()
    assert page.screenshot.await_count == 1


async def test_overview_pixel_limit_rejects_without_splitting(created):
    page, _ = fake_browser(created, overview_height=3000, content="metadata")
    response = await rendering.present(
        created.studio, created.event, await detail(created), content="metadata"
    )
    assert response["error"]["code"] == "render_limit"
    assert "档案" in response["error"]["message"]
    created.event.send.assert_not_called()
    page.screenshot.assert_not_called()


async def test_total_page_limit_includes_overview(created):
    fake_browser(created, height=500, content="all")
    created.config["render_max_pages"] = 1
    response = await rendering.present(
        created.studio, created.event, await detail(created), content="all"
    )
    assert response["error"]["code"] == "render_limit"
    created.event.send.assert_not_called()


async def test_send_failure_reports_partial_without_retry(created):
    fake_browser(created)
    created.event.send.side_effect = [None, RuntimeError("platform failed")]
    response = await rendering.present(
        created.studio, created.event, await detail(created)
    )
    assert not response["ok"] and not response["retry_safe"]
    assert response["delivery"]["images_submitted"] == 1
    assert response["delivery"]["failed_message_may_have_been_delivered"]
    assert created.event.send.await_count == 2


async def test_timeout_does_not_retry(created):
    page, _ = fake_browser(created)
    page.set_content.side_effect = asyncio.TimeoutError
    with pytest.raises(StudioError, match="超时"):
        await rendering.render_pages(
            created.context, (await detail(created))["data"], Settings()
        )
    page.set_content.assert_awaited_once()


async def test_revocation_while_rendering_does_not_return_raw_data(created):
    page, _ = fake_browser(created)

    async def revoke(*args, **kwargs):
        created.config["whitelist"] = []
        return PNG

    page.screenshot.side_effect = revoke
    response = await rendering.present(
        created.studio, created.event, await detail(created)
    )
    assert response["error"]["code"] == "forbidden"
    assert "data" not in response
    created.event.send.assert_not_called()


@pytest.mark.parametrize("family", ["manage", "versions"])
@pytest.mark.parametrize("output_format", ["text", "image", "markdown"])
@pytest.mark.parametrize("content", ["metadata", "prompt", "all"])
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("platform", ["aiocqhttp", "satori", "telegram"])
async def test_model_get_format_content_matrix(
    created, tmp_path, monkeypatch, family, output_format, content, group, platform
):
    if group:
        created.config["allow_group_chat"] = True
        created.event = created.native_event(group=True)
        created.event.message_obj.group_id = "test-group"
        created.event.session_id = "test-group"
    await created.manager.update_persona(
        "alice", custom_error_message="META_ERROR", tools=None, skills=[]
    )
    payload = {"persona_id": "alice"}
    if family == "versions":
        updated = await created.studio.execute(
            created.event,
            "manage",
            "update",
            {"persona_id": "alice", "system_prompt": "版本2"},
        )
        assert updated["ok"], updated
        payload = {"backup_id": updated["data"]["backup_id"]}
    payload.update({"format": output_format, "content": content})
    created.event.platform_meta.name = platform
    monkeypatch.setattr(rendering, "get_astrbot_temp_path", lambda: str(tmp_path))
    captured_files = []

    async def send(message):
        component = message.chain[0]
        if component.type.value == "File":
            path = await component.get_file()
            captured_files.append(
                (
                    component.name,
                    (await asyncio.to_thread(Path(path).read_bytes)).decode("utf-8"),
                )
            )

    created.event.send = AsyncMock(side_effect=send)
    if output_format == "image":
        page, _ = fake_browser(created, height=450, content=content)
    plugin = PersonaStudioPlugin(created.context, created.config)
    plugin.studio = created.studio
    response = json.loads(
        await getattr(
            plugin, "persona_get" if family == "manage" else "persona_backup_get"
        )(created.event, **payload)
    )
    assert response["ok"], response
    assert response["delivery"]["format"] == output_format
    assert response["delivery"]["content"] == content
    assert response["data"]["persona"]["system_prompt"] == "版本1"
    if output_format == "image":
        expected_sheets = (
            ["overview", "content"]
            if content == "all"
            else ["overview" if content == "metadata" else "content"]
        )
        documents = [call.args[0] for call in page.set_content.await_args_list]
        assert len(documents) == len(expected_sheets)
        for document, sheet in zip(documents, expected_sheets):
            assert '<div class="brand-name">浮生绘卷</div>' in document
            assert f'data-sheet="{sheet}"' in document
            if sheet == "content":
                assert 'class="hero-meta"' not in document
        displayed = "\n".join(documents)
        assert response["delivery"]["images_submitted"] == len(expected_sheets)
        assert not response["delivery"]["md_submitted"]
        assert response["delivery"]["forwards_submitted"] == 0
        assert all(
            call.args[0].chain[0].type.value == "Image"
            for call in created.event.send.await_args_list
        )
    elif output_format == "markdown":
        assert len(captured_files) == 1
        scope = {"metadata": "_元数据", "prompt": "", "all": "_完整"}[content]
        kind = "历史" if family == "versions" else "当前"
        assert captured_files[0][0] == f"人格_alice_{kind}_v1{scope}.md"
        displayed = captured_files[0][1]
        assert response["delivery"]["md_submitted"]
        assert response["delivery"]["images_submitted"] == 0
        assert response["delivery"]["forwards_submitted"] == 0
        assert created.event.send.await_count == 1
        assert not list(tmp_path.rglob("*.md"))
    elif platform in {"aiocqhttp", "satori"}:
        assert created.event.send.await_count == 1
        component = created.event.send.await_args.args[0].chain[0]
        assert isinstance(component, Nodes)
        assert all(
            node.uin == "bot" and node.name == "浮生绘卷" for node in component.nodes
        )
        displayed = "\n".join(node.content[0].text for node in component.nodes)
        assert response["delivery"]["forwards_submitted"] == 1
        assert response["delivery"]["forward_nodes_submitted"] == len(component.nodes)
        assert response["delivery"]["images_submitted"] == 0
        assert not response["delivery"]["md_submitted"]
    else:
        components = [
            call.args[0].chain[0] for call in created.event.send.await_args_list
        ]
        assert components and all(
            isinstance(component, Plain) for component in components
        )
        displayed = "".join(component.text for component in components)
        assert response["delivery"]["forwards_submitted"] == 0
        assert response["delivery"]["forward_nodes_submitted"] == 0
        assert response["delivery"]["images_submitted"] == 0
        assert not response["delivery"]["md_submitted"]
    assert response["delivery"]["texts_submitted"] == (
        created.event.send.await_count
        if output_format == "text" and platform == "telegram"
        else 0
    )
    assert ("版本与权限" in displayed) == (content in {"metadata", "all"})
    assert ("META_ERROR" in displayed) == (content in {"metadata", "all"})
    assert ("系统提示词" in displayed) == (content in {"prompt", "all"})
    assert ("预设对话" in displayed) == (content in {"prompt", "all"})
    if content in {"metadata", "all"}:
        assert "全部工具" in displayed and "已禁用" in displayed


@pytest.mark.parametrize("family", ["manage", "versions"])
async def test_default_get_sends_prompt_only(created, family):
    payload = {"persona_id": "alice"}
    if family == "versions":
        updated = await created.studio.execute(
            created.event,
            "manage",
            "update",
            {"persona_id": "alice", "system_prompt": "版本2"},
        )
        assert updated["ok"], updated
        payload = {"backup_id": updated["data"]["backup_id"]}
    page, _ = fake_browser(created, height=450)
    plugin = PersonaStudioPlugin(created.context, created.config)
    plugin.studio = created.studio
    response = json.loads(
        await getattr(
            plugin, "persona_get" if family == "manage" else "persona_backup_get"
        )(created.event, **payload)
    )
    assert response["ok"], response
    assert response["delivery"]["format"] == "image"
    assert response["delivery"]["content"] == "prompt"
    assert response["delivery"]["images_submitted"] == 1
    assert created.event.send.await_count == 1
    page.set_content.assert_awaited_once()
    document = page.set_content.await_args.args[0]
    assert 'data-sheet="content"' in document
    assert 'class="hero-meta"' not in document
    assert "版本与权限" not in document


async def test_prompt_single_page_does_not_reserve_overview_slot(created):
    created.config["render_max_pages"] = 1
    fake_browser(created, height=450)
    response = await rendering.present(
        created.studio, created.event, await detail(created)
    )
    assert response["ok"] and response["delivery"]["images_submitted"] == 1


async def test_paginated_all_images_preserve_order(created):
    page, calls = fake_browser(created, content="all")
    response = await rendering.present(
        created.studio, created.event, await detail(created), content="all"
    )
    assert response["ok"] and response["delivery"]["images_submitted"] == 3
    assert calls[0]["javascript_enabled"] is False
    assert page.screenshot.call_args_list[0].kwargs["clip"]["height"] == 1200
    assert page.screenshot.call_args_list[2].kwargs["clip"]["y"] == 1500
    assert page.evaluate.call_args_list[1].args[1]["maxPages"] == 19
    assert 'data-sheet="overview"' in page.set_content.call_args_list[0].args[0]
    assert 'data-sheet="content"' in page.set_content.call_args_list[1].args[0]


@pytest.mark.parametrize(
    "platform",
    [
        "dingtalk",
        "discord",
        "kook",
        "lark",
        "line",
        "mattermost",
        "misskey",
        "qqofficial",
        "qqofficial_webhook",
        "slack",
        "telegram",
        "webchat",
        "wecom",
        "wecom_ai_bot",
        "weixin_oc",
        "weixin_official_account",
        "custom",
    ],
)
async def test_text_uses_plain_messages_on_other_platforms(created, platform):
    created.event.platform_meta.name = platform
    response = await rendering.present(
        created.studio, created.event, await detail(created), format="text"
    )
    assert response["ok"], response
    assert response["delivery"]["forwards_submitted"] == 0
    assert response["delivery"]["forward_nodes_submitted"] == 0
    assert response["delivery"]["texts_submitted"] == 1
    created.event.send.assert_awaited_once()
    message = created.event.send.await_args.args[0]
    assert message.type == "tool_direct_result"
    assert len(message.chain) == 1 and isinstance(message.chain[0], Plain)
    assert "系统提示词\n\n版本1" in message.chain[0].text


async def test_plain_text_chunks_preserve_all_prompt_text(created, monkeypatch):
    prompt = "首行\n\n" + "完整原文，中文与换行不能丢失。" * 900 + "\n结尾"
    await created.manager.update_persona("alice", system_prompt=prompt)
    created.event.platform_meta.name = "discord"
    monkeypatch.setattr(rendering, "FORWARD_MAX_NODES", 1)
    response = await rendering.present(
        created.studio, created.event, await detail(created), format="text"
    )
    assert response["ok"], response
    messages = [call.args[0] for call in created.event.send.await_args_list]
    assert len(messages) > 1
    assert all(
        message.type == "tool_direct_result"
        and len(message.chain) == 1
        and isinstance(message.chain[0], Plain)
        and len(message.chain[0].text) <= rendering.TEXT_MESSAGE_CHARS
        for message in messages
    )
    assert "".join(message.chain[0].text for message in messages) == (
        f"系统提示词\n\n{prompt}\n\n"
        "预设对话 · 第 1 轮 · 用户\n\n你好\n\n"
        "预设对话 · 第 1 轮 · 助手\n\n你好呀"
    )
    assert response["delivery"]["texts_submitted"] == len(messages)
    assert response["delivery"]["forwards_submitted"] == 0
    assert response["delivery"]["forward_nodes_submitted"] == 0


@pytest.mark.parametrize("platform", ["aiocqhttp", "satori", "telegram"])
async def test_only_forward_text_requires_bot_id(created, platform):
    created.event.platform_meta.name = platform
    created.event.message_obj.self_id = ""
    response = await rendering.present(
        created.studio, created.event, await detail(created), format="text"
    )
    if platform == "telegram":
        assert response["ok"], response
        assert response["delivery"]["texts_submitted"] == 1
        created.event.send.assert_awaited_once()
    else:
        assert response["error"]["code"] == "invalid_identity"
        assert response["delivery"]["texts_submitted"] == 0
        created.event.send.assert_not_called()


async def test_plain_text_send_failure_reports_partial_without_retry(created):
    await created.manager.update_persona("alice", system_prompt="完整原文" * 2000)
    created.event.platform_meta.name = "telegram"
    created.event.send.side_effect = [None, RuntimeError("platform failed")]
    response = await rendering.present(
        created.studio, created.event, await detail(created), format="text"
    )
    assert response["error"]["code"] == "delivery_failed"
    assert not response["retry_safe"]
    assert response["delivery"]["texts_submitted"] == 1
    assert response["delivery"]["forwards_submitted"] == 0
    assert response["delivery"]["failed_message_may_have_been_delivered"]
    assert created.event.send.await_count == 2


@pytest.mark.parametrize("group", [False, True])
async def test_plain_text_revocation_stops_remaining_messages(created, group):
    if group:
        created.config["allow_group_chat"] = True
        created.event = created.native_event(group=True)
    await created.manager.update_persona("alice", system_prompt="完整原文" * 2000)
    created.event.platform_meta.name = "telegram"

    async def revoke(message):
        if group:
            created.config["allow_group_chat"] = False
        else:
            created.config["whitelist"] = []

    created.event.send.side_effect = revoke
    response = await rendering.present(
        created.studio, created.event, await detail(created), format="text"
    )
    assert response["error"]["code"] == ("private_only" if group else "forbidden")
    assert "data" not in response and not response["retry_safe"]
    assert response["delivery"]["texts_submitted"] == 1
    assert response["delivery"]["forwards_submitted"] == 0
    created.event.send.assert_awaited_once()


@pytest.mark.parametrize("platform", ["aiocqhttp", "satori"])
async def test_forward_send_failure_does_not_retry_as_plain_text(created, platform):
    created.event.platform_meta.name = platform
    created.event.send.side_effect = RuntimeError("platform failed")
    response = await rendering.present(
        created.studio, created.event, await detail(created), format="text"
    )
    assert response["error"]["code"] == "delivery_failed"
    assert not response["retry_safe"]
    assert response["delivery"]["texts_submitted"] == 0
    assert response["delivery"]["forwards_submitted"] == 0
    created.event.send.assert_awaited_once()
    assert isinstance(created.event.send.await_args.args[0].chain[0], Nodes)


@pytest.mark.parametrize("platform", ["aiocqhttp", "satori"])
async def test_forward_chunks_preserve_all_prompt_text(created, platform):
    prompt = "首行\n\n" + "完整原文，中文与换行不能丢失。" * 900 + "\n结尾"
    await created.manager.update_persona("alice", system_prompt=prompt)
    created.event.platform_meta.name = platform
    response = await rendering.present(
        created.studio, created.event, await detail(created), format="text"
    )
    assert response["ok"], response
    nodes = created.event.send.await_args.args[0].chain[0].nodes
    prompt_nodes = [
        node for node in nodes if node.content[0].text.startswith("系统提示词")
    ]
    assert len(prompt_nodes) > 1
    assert (
        "".join(node.content[0].text.split("\n\n", 1)[1] for node in prompt_nodes)
        == prompt
    )
    assert all(
        len(node.content[0].text) <= rendering.FORWARD_NODE_CHARS for node in nodes
    )


@pytest.mark.parametrize("platform", ["aiocqhttp", "satori"])
async def test_forward_node_limit_does_not_send_partial_text(
    created, monkeypatch, platform
):
    created.event.platform_meta.name = platform
    monkeypatch.setattr(rendering, "FORWARD_MAX_NODES", 1)
    response = await rendering.present(
        created.studio, created.event, await detail(created), format="text"
    )
    assert response["error"]["code"] == "forward_limit"
    assert response["delivery"]["texts_submitted"] == 0
    assert response["delivery"]["forward_nodes_submitted"] == 0
    created.event.send.assert_not_called()


@pytest.mark.parametrize("group", [False, True])
async def test_forward_uses_native_current_chat_forward_api(
    created, monkeypatch, group
):
    from astrbot.api.event import AstrMessageEvent
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,
    )

    if group:
        created.config["allow_group_chat"] = True
        created.event = created.native_event(group=True)
        created.event.message_obj.group_id = "test-group"
        created.event.session_id = "user_test-group"
    created.event.platform_meta.name = "aiocqhttp"
    response = await rendering.present(
        created.studio,
        created.event,
        await detail(created),
        format="text",
        content="all",
    )
    assert response["ok"], response
    bot = SimpleNamespace(call_action=AsyncMock())
    message = created.event.send.await_args.args[0]
    native = AiocqhttpMessageEvent(
        created.event.message_str,
        created.event.message_obj,
        created.event.platform_meta,
        created.event.session_id,
        bot,
    )
    monkeypatch.setattr(AstrMessageEvent, "send", AsyncMock())
    await native.send(message)
    bot.call_action.assert_awaited_once()
    call = bot.call_action.await_args
    assert call.args == (
        "send_group_forward_msg" if group else "send_private_forward_msg",
    )
    assert call.kwargs["group_id" if group else "user_id"] == (
        "test-group" if group else "user"
    )
    assert (
        len(call.kwargs["messages"]) == response["delivery"]["forward_nodes_submitted"]
    )
    assert all(node["data"]["user_id"] == "bot" for node in call.kwargs["messages"])


@pytest.mark.parametrize("group", [False, True])
async def test_forward_uses_native_satori_message_api(created, monkeypatch, group):
    from astrbot.api.event import AstrMessageEvent
    from astrbot.core.platform.sources.satori.satori_event import SatoriPlatformEvent

    if group:
        created.config["allow_group_chat"] = True
        created.event = created.native_event(group=True)
        created.event.message_obj.group_id = "test-group"
        created.event.session_id = "test-group"
    created.event.platform_meta.name = "satori"
    response = await rendering.present(
        created.studio,
        created.event,
        await detail(created),
        format="text",
        content="all",
    )
    assert response["ok"], response
    adapter = SimpleNamespace(
        send_http_request=AsyncMock(return_value=[{"id": "sent"}])
    )
    message = created.event.send.await_args.args[0]
    native = SatoriPlatformEvent(
        created.event.message_str,
        created.event.message_obj,
        created.event.platform_meta,
        created.event.session_id,
        adapter,
    )
    monkeypatch.setattr(AstrMessageEvent, "send", AsyncMock())
    await native.send(message)
    adapter.send_http_request.assert_awaited_once()
    call = adapter.send_http_request.await_args
    assert call.args[:2] == ("POST", "/message.create")
    payload = call.args[2]
    assert payload["channel_id"] == ("test-group" if group else "user")
    assert payload["content"].startswith("<message forward>")
    assert payload["content"].endswith("</message>")
    assert (
        payload["content"].count('<author id="bot" name="浮生绘卷"/>')
        == (response["delivery"]["forward_nodes_submitted"])
    )
    assert "版本与权限" in payload["content"] and "系统提示词" in payload["content"]


@pytest.mark.parametrize("output_format", ["image", "text", "markdown"])
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("platform", ["aiocqhttp", "satori", "telegram"])
async def test_revocation_after_delivery_hides_structured_data(
    created, tmp_path, monkeypatch, output_format, group, platform
):
    if group:
        created.config["allow_group_chat"] = True
        created.event = created.native_event(group=True)
    if output_format == "image":
        fake_browser(created, height=450)
    created.event.platform_meta.name = platform
    monkeypatch.setattr(rendering, "get_astrbot_temp_path", lambda: str(tmp_path))

    async def revoke(message):
        if group:
            created.config["allow_group_chat"] = False
        else:
            created.config["whitelist"] = []

    created.event.send = AsyncMock(side_effect=revoke)
    response = await rendering.present(
        created.studio, created.event, await detail(created), format=output_format
    )
    assert not response["ok"] and "data" not in response
    assert not response["retry_safe"]
    assert created.event.send.await_count == 1
    assert not list(tmp_path.rglob("*.md"))


@pytest.mark.parametrize("page_height", [1001, 1500])
async def test_real_browser_renders_long_offline_content(
    created, tmp_path, page_height
):
    from astrbot_plugin_browser.service import BrowserService, find_system_chromium
    from PIL import Image

    executable = find_system_chromium()
    if executable is None:
        pytest.skip("No installed Chromium browser for integration test")
    service = BrowserService(browser_executable=str(executable))
    await service.initialize()
    created.context.get_registered_star = lambda name: SimpleNamespace(
        activated=True, star_cls=SimpleNamespace(service=service)
    )
    try:
        response = await detail(created)
        response["data"]["persona"]["system_prompt"] = "\n".join(
            f"第 {index + 1} 行：完整人格提示词，保留换行与中文。"
            for index in range(80)
        )
        images = await rendering.render_pages(
            created.context, response["data"], Settings()
        )
        assert len(images) >= 3
        for index, image in enumerate(images):
            with Image.open(io.BytesIO(image)) as picture:
                assert picture.width == 1000 and picture.height > 0
                assert picture.width * picture.height <= Settings().render_max_pixels
                if index:
                    assert picture.height <= 1500
            (tmp_path / f"preview-{index + 1}.png").write_bytes(image)
        for content in ("metadata", "all"):
            selected_images = await rendering.render_pages(
                created.context, response["data"], Settings(), content
            )
            if content == "metadata":
                assert len(selected_images) == 1
            else:
                assert len(selected_images) == len(images) + 1
            for index, image in enumerate(selected_images):
                with Image.open(io.BytesIO(image)) as picture:
                    assert picture.width == 1000 and picture.height > 0
                    assert (
                        picture.width * picture.height <= Settings().render_max_pixels
                    )
                    if index:
                        assert picture.height <= 1500
                (tmp_path / f"{content}-{index + 1}.png").write_bytes(image)
        prompt = (
            "\n"
            + "\n\n".join(
                f"【第 {index + 1} 节】\n"
                + "长段落完整换行验证，标点、中文与原始内容均保留。" * 20
                for index in range(12)
            )
            + "\n"
        )
        response["data"]["persona"]["system_prompt"] = prompt
        async with service.session(
            viewport={"width": 1000, "height": page_height}, javascript_enabled=False
        ) as page:
            await page.set_content(rendering.content_html(response["data"], 1000))
            layout = await page.evaluate(
                rendering.PAGINATE_CONTENT, {"height": page_height, "maxPages": 19}
            )
            assert "error" not in layout and len(layout["pages"]) > 1
            assert all(
                0 < bounds["height"] <= page_height for bounds in layout["pages"]
            )
            inspected = await page.evaluate("""() => {
                const sheets = [...document.querySelectorAll('[data-sheet="content"]')];
                let broken = 0;
                for (const sheet of sheets) {
                    const viewport = sheet.querySelector('.flow-window').getBoundingClientRect();
                    for (const element of sheet.querySelectorAll('[data-text]')) {
                        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
                        let node;
                        while ((node = walker.nextNode())) {
                            if (!node.textContent.trim()) continue;
                            const range = document.createRange();
                            range.selectNodeContents(node);
                            for (const rect of range.getClientRects()) {
                                if (rect.width > 0 && ((rect.top < viewport.top - .05 && rect.bottom > viewport.top + .05) || (rect.top < viewport.bottom - .05 && rect.bottom > viewport.bottom + .05))) broken++;
                            }
                        }
                    }
                }
                return {
                    broken,
                    promptHeadings: sheets[0].querySelectorAll('[data-prompt-heading]').length,
                    pages: sheets.map(sheet => ({
                        hasHeader: Boolean(sheet.querySelector('.persona-header')),
                        hasTitle: Boolean(sheet.querySelector('h1')),
                        hasMetadata: Boolean(sheet.querySelector('.hero-meta')),
                        continuation: sheet.classList.contains('continuation'),
                        hintVisible: sheet.querySelector('.continuation-header').getBoundingClientRect().height > 0,
                        contentHeight: sheet.querySelector('.flow-window').getBoundingClientRect().height,
                        pageNumber: sheet.querySelector('[data-page-number]').textContent
                    }))
                };
            }""")
            assert inspected["broken"] == 0
            assert inspected["promptHeadings"] == 12
            assert prompt in rendering.detail_markdown(response["data"])
            first_page = inspected["pages"][0]
            assert first_page["hasHeader"] and first_page["hasTitle"]
            assert not first_page["hasMetadata"]
            assert not first_page["continuation"] and not first_page["hintVisible"]
            for index, subsequent_page in enumerate(inspected["pages"][1:], start=2):
                assert not subsequent_page["hasHeader"]
                assert not subsequent_page["hasTitle"]
                assert not subsequent_page["hasMetadata"]
                assert (
                    subsequent_page["continuation"] and subsequent_page["hintVisible"]
                )
                assert (
                    subsequent_page["pageNumber"]
                    == f"正文 · {index} / {len(layout['pages'])}"
                )
            assert (
                inspected["pages"][1]["contentHeight"]
                > first_page["contentHeight"] + 100
            )
    finally:
        await service.close()
