import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot_plugin_persona_studio.card_service import CardStudio
from astrbot_plugin_persona_studio.card_tools import (
    CARD_MODEL_TOOLS,
    card_tool_parameters,
)
from astrbot_plugin_persona_studio.card_web import PREFIX, CardWeb
from astrbot_plugin_persona_studio.cards import (
    _chunk,
    blank_png,
    export_png,
    import_card,
    new_card,
    png_chunks,
)
from astrbot_plugin_persona_studio.domain import Actor, canonical
from astrbot_plugin_persona_studio.main import PersonaStudioPlugin
from fastapi import FastAPI, Request


@pytest.fixture
async def web(env):
    cards = CardStudio(env.studio)
    env.studio.cards = cards
    context = SimpleNamespace(registered_web_apis=[])
    context.register_web_api = lambda *args: context.registered_web_apis.append(args)
    api = CardWeb(context, cards)
    api.register()
    app = FastAPI()
    identity = {"username": "dashboard-admin"}

    def route(handler):
        async def dispatch(request: Request):
            bound = PluginRequest(request, username=identity["username"])
            with bind_request_context(bound):
                return await handler()

        return dispatch

    for path, handler, methods, _ in context.registered_web_apis:
        app.add_api_route(path, route(handler), methods=methods)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            cards=cards, api=api, context=context, client=client, identity=identity
        )
    api.unregister()
    assert context.registered_web_apis == []


async def test_authenticated_web_full_import_edit_history_export(web):
    document = new_card("页面导入")
    document["data"]["character_version"] = "1"
    response = await web.client.post(
        PREFIX + "/import",
        files={"file": ("character.png", export_png(document), "image/png")},
    )
    assert response.status_code == 200, response.text
    preview = response.json()["data"]
    response = await web.client.post(
        PREFIX + "/action",
        json={"action": "create", "payload": {"import_id": preview["import_id"]}},
    )
    c = response.json()["data"]
    assert c["owner"] == ["webui", "dashboard-admin"]
    page = (await web.client.get(PREFIX + "/cards")).json()["data"]
    assert any(
        item["card_id"] == c["card_id"] and item["name"] == "页面导入"
        for item in page["items"]
    )
    for version, count in [("1", 1), ("2", 2)]:
        response = await web.client.post(
            PREFIX + "/action",
            json={
                "action": "update",
                "payload": {
                    "card_id": c["card_id"],
                    "expected_revision": c["revision"],
                    "data": {"description": version, "character_version": version},
                },
            },
        )
        c = response.json()["data"]
        assert len(c["snapshots"]) == count
    response = await web.client.get(
        PREFIX + "/export", params={"card_id": c["card_id"], "format": "png"}
    )
    assert import_card(response.content)[0] == c["document"]
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    avatar = (
        await web.client.get(PREFIX + "/avatar", params={"card_id": c["card_id"]})
    ).json()["data"]
    assert avatar["data_url"].startswith("data:image/png;base64,")


async def test_web_archive_unarchive_and_permanent_delete_preserve_core(web):
    document = new_card("待清理角色")
    response = await web.client.post(
        PREFIX + "/import",
        files={"file": ("character.png", export_png(document), "image/png")},
    )
    import_id = response.json()["data"]["import_id"]

    async def action(name, payload):
        return await web.client.post(
            PREFIX + "/action", json={"action": name, "payload": payload}
        )

    c = (await action("create", {"import_id": import_id})).json()["data"]
    c = (
        await action(
            "update",
            {
                "card_id": c["card_id"],
                "expected_revision": c["revision"],
                "data": {"description": "最后一次编辑"},
            },
        )
    ).json()["data"]
    current = c["document"]
    for name in ("archive", "unarchive", "archive"):
        response = await action(
            name, {"card_id": c["card_id"], "expected_revision": c["revision"]}
        )
        assert response.status_code == 200, response.text
        c = response.json()["data"]
        assert c["document"] == current and len(c["snapshots"]) == 1
    core = await web.cards.studio.current(c["persona_id"])
    payload = {
        "card_id": c["card_id"],
        "expected_revision": c["revision"],
        "confirm_name": "错误名称",
    }
    assert (await action("delete", payload)).status_code == 400
    payload["confirm_name"] = document["data"]["name"]
    response = await action("delete", payload)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["deleted"]
    assert await web.cards.studio.current(c["persona_id"]) == core
    for route in ("/card", "/export", "/card-image", "/avatar"):
        response = await web.client.get(
            PREFIX + route, params={"card_id": c["card_id"]}
        )
        assert response.status_code == 400
    assert (await action("delete", payload)).status_code == 400
    for deleted in ("false", "true"):
        listed = (
            await web.client.get(PREFIX + "/cards", params={"deleted": deleted})
        ).json()["data"]
        assert c["persona_id"] not in {item["persona_id"] for item in listed["items"]}
    assert (
        web.cards.storage.connection.execute(
            "SELECT * FROM card_imports WHERE import_id=?", (import_id,)
        ).fetchone()
        is None
    )


async def test_page_list_includes_existing_core_personas_and_keeps_bindings(web):
    await web.cards.studio.manager.create_persona(
        "已有 AstrBot 人格", system_prompt="完整提示词", begin_dialogs=["问题", "回答"]
    )
    before = await web.cards.studio.current("已有 AstrBot 人格")
    first = (await web.client.get(PREFIX + "/cards")).json()["data"]
    second = (await web.client.get(PREFIX + "/cards")).json()["data"]
    assert {c["persona_id"] for c in first["items"]} == {"default", "已有 AstrBot 人格"}
    assert {c["card_id"] for c in first["items"]} == {
        c["card_id"] for c in second["items"]
    }
    item = next(c for c in first["items"] if c["persona_id"] == "已有 AstrBot 人格")
    assert item["persona_import"] and item["sync_status"] == "synced"
    response = await web.client.get(
        PREFIX + "/card", params={"card_id": item["card_id"]}
    )
    card = response.json()["data"]
    assert card["document"]["spec"] == "chara_card_v3" and card["owner"] is None
    response = await web.client.post(
        PREFIX + "/action",
        json={
            "action": "update",
            "payload": {
                "card_id": card["card_id"],
                "expected_revision": card["revision"],
                "data": {"system_prompt": "页面修改后的提示词"},
            },
        },
    )
    assert response.status_code == 200, response.text
    edited = response.json()["data"]
    assert edited["sync"]["status"] == "synced"
    after = await web.cards.studio.current("已有 AstrBot 人格")
    assert after["id"] == before["id"] and after["created_at"] == before["created_at"]
    assert (
        after["system_prompt"] == "页面修改后的提示词"
        and after["begin_dialogs"] == before["begin_dialogs"]
    )
    assert len(edited["snapshots"]) == 1


@pytest.mark.parametrize("input_format", ["json", "png"])
async def test_v2_import_and_all_exports_use_v3(web, input_format):
    document = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "unknown_top_level": {"keep": True},
        "data": {
            "name": "旧角色卡",
            "character_version": "作者版本-1",
            "creation_date": 123,
            "modification_date": 456,
            "character_book": {"entries": [{"content": "世界书内容"}]},
            "extensions": {"custom": [1, 2, 3]},
        },
    }
    raw = canonical(document).encode()
    if input_format == "png":
        image = blank_png()
        raw = (
            image[:-12]
            + _chunk(b"tEXt", b"chara\0" + base64.b64encode(raw))
            + image[-12:]
        )
    response = await web.client.post(
        PREFIX + "/import",
        files={"file": ("character." + input_format, raw)},
    )
    assert response.status_code == 200, response.text
    preview = response.json()["data"]
    assert preview["card"]["spec"] == "chara_card_v3"
    response = await web.client.post(
        PREFIX + "/action",
        json={"action": "create", "payload": {"import_id": preview["import_id"]}},
    )
    assert response.status_code == 200, response.text
    c = response.json()["data"]
    assert c["document"]["spec"] == "chara_card_v3"
    assert c["document"]["spec_version"] == "3.0"
    assert all(
        c["document"][key] == value
        for key, value in document.items()
        if key not in ("data", "spec", "spec_version")
    )
    assert all(
        c["document"]["data"][key] == value for key, value in document["data"].items()
    )
    stored = json.loads(
        web.cards.storage.connection.execute("SELECT document FROM cards").fetchone()[0]
    )
    assert stored == c["document"]
    snapshot = web.cards.storage.snapshot(
        c["card_id"], c["snapshots"][0]["snapshot_id"]
    )
    assert snapshot["snapshot"]["document"] == stored
    for output_format in ("json", "png"):
        params = {"card_id": c["card_id"], "format": output_format}
        for version in (None, "3", "2"):
            query = params if version is None else {**params, "version": version}
            response = await web.client.get(PREFIX + "/export", params=query)
            if version == "2":
                assert response.status_code == 400
                assert response.json()["data"]["code"] == "invalid_payload"
                continue
            assert response.status_code == 200, response.text
            raw = response.content
            if output_format == "png":
                texts = [
                    p for k, p in png_chunks(raw) if k in (b"tEXt", b"zTXt", b"iTXt")
                ]
                assert len(texts) == 1 and texts[0].startswith(b"ccv3\0")
                raw = base64.b64decode(texts[0].split(b"\0", 1)[1])
            assert json.loads(raw) == c["document"]
    assert len(web.cards.storage.snapshots(c["card_id"])) == 1


async def test_web_ignores_forged_user_and_denies_missing_authenticated_identity(web):
    document = new_card("伪造")
    response = await web.client.post(
        PREFIX + "/action",
        json={
            "action": "create",
            "payload": {"card": document, "owner": ["p", "victim"]},
        },
    )
    assert response.status_code == 400
    web.identity["username"] = None
    response = await web.client.get(PREFIX + "/cards", params={"username": "admin"})
    assert (
        response.status_code == 400 and response.json()["data"]["code"] == "forbidden"
    )
    with pytest.raises(Exception):
        CardStudio.web_actor("")


async def test_web_validates_malformed_payloads_and_preview_has_no_side_effect(web):
    for payload in [
        None,
        [],
        {"action": [], "payload": {}},
        {"action": "get", "payload": []},
    ]:
        response = await web.client.post(PREFIX + "/action", json=payload)
        assert response.status_code == 400, response.text
    preview = await web.client.post(
        PREFIX + "/action",
        json={"action": "preview", "payload": {"card": new_card("预览")}},
    )
    assert preview.status_code == 200
    assert web.cards.storage.list(Actor("", "", True)) == []


async def test_worldbook_preview_unsaved_draft_validation_and_no_side_effects(web):
    document = new_card("爱莉")
    document["data"]["character_book"] = {
        "entries": [
            {
                "keys": ["白塔"],
                "content": "{{char}} 认识 {{user}}",
                "extensions": {"unknown": [1]},
            }
        ],
        "extensions": {"custom": {"original": True}},
    }
    payload = {"card": document, "prompt": "去白塔", "username": "访客"}
    response = await web.client.post(
        PREFIX + "/action", json={"action": "worldbook_preview", "payload": payload}
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["entries"][0]["matched_keys"] == ["白塔"]
    assert "爱莉 认识 访客" in result["after"]
    assert web.cards.storage.list(Actor("", "", True)) == []
    for patch in (
        {"prompt": []},
        {"history": "白塔"},
        {"history": [None]},
        {"history": ["x"] * 51},
        {"username": []},
        {"prompt": "x" * 32001},
    ):
        response = await web.client.post(
            PREFIX + "/action",
            json={"action": "worldbook_preview", "payload": {**payload, **patch}},
        )
        assert response.status_code == 400, response.text
    web.identity["username"] = ""
    response = await web.client.post(
        PREFIX + "/action", json={"action": "worldbook_preview", "payload": payload}
    )
    assert response.status_code == 400


async def test_worldbook_editor_end_to_end_in_browser(web, tmp_path):
    """Exercise the shipped page against real APIs, with no external server/data."""
    import shutil
    from pathlib import Path
    from urllib.parse import urlsplit

    async_playwright = pytest.importorskip("playwright.async_api").async_playwright

    executable = shutil.which("chromium") or shutil.which("google-chrome")
    if not executable:
        pytest.skip("No installed Chromium browser for editor integration test")
    document = new_card("爱莉")
    document["data"].update(
        description="温柔的旅行者，喜欢探索白塔与山间的旧城。",
        character_book={
            "name": "白塔纪事",
            "entries": [
                {
                    "name": "白塔法师协会",
                    "keys": ["白塔", "法师协会"],
                    "content": "白塔是法师协会的总部，{{char}} 曾在这里学习星象。",
                    "enabled": True,
                    "extensions": {"untouched": {"preserve": True}},
                },
                {
                    "name": "角色与世界",
                    "constant": True,
                    "content": "故事发生在群山之间的旧王国。",
                    "position": "before_char",
                },
                {
                    "name": "时间法则",
                    "constant": True,
                    "content": "这条规则带有冷却时间。",
                    "extensions": {"cooldown": 2},
                },
            ],
            "extensions": {"other_app": [1, 2, 3]},
        },
    )
    created = await web.client.post(
        PREFIX + "/action", json={"action": "create", "payload": {"card": document}}
    )
    card = created.json()["data"]
    bridge = """
    window.AstrBotPluginView={
      ready:async()=>{},onContext:fn=>fn({isDark:false}),
      request:async(path,method='GET',body)=>{
        const response=await fetch('/api'+path,{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
        const result=await response.json();if(!response.ok)throw Error(result.message||'请求失败');return result.data;
      },
      apiGet:(path,params={})=>window.AstrBotPluginView.request('/'+path+'?'+new URLSearchParams(params)),
      apiPost:(path,body)=>window.AstrBotPluginView.request('/'+path,'POST',body)
    };
    """
    root = Path(__file__).parents[1] / "views" / "studio"
    errors = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            executable_path=executable, headless=True, args=["--no-sandbox"]
        )
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def route_handler(route):
                request = route.request
                path = urlsplit(request.url).path
                if path == "/api/plugin/page/bridge-sdk.js":
                    await route.fulfill(
                        body=bridge, content_type="application/javascript"
                    )
                elif path.startswith("/api/"):
                    response = await web.client.request(
                        request.method,
                        PREFIX + request.url.split("/api", 1)[1],
                        content=request.post_data,
                        headers={"Content-Type": "application/json"},
                    )
                    await route.fulfill(
                        status=response.status_code,
                        body=response.content,
                        content_type="application/json",
                    )
                elif path in ("/", "/app.js", "/app.css", "/worldbook.js"):
                    file = root / ("index.html" if path == "/" else path[1:])
                    await route.fulfill(
                        body=file.read_bytes(),
                        content_type={
                            ".html": "text/html",
                            ".js": "application/javascript",
                            ".css": "text/css",
                        }[file.suffix],
                    )
                else:
                    await route.fulfill(status=404, body="Not found")

            await page.route("http://studio.test/**", route_handler)
            await page.goto("http://studio.test/")
            await page.get_by_role("button", name="编辑角色：爱莉", exact=True).click()
            await page.get_by_role("tab", name="世界书", exact=True).click()
            await page.locator('[data-wb-test="test"]').fill("今天我们去白塔看看吧。")
            await page.locator(
                '#wb-result:has-text("爱莉 曾在这里学习星象")'
            ).wait_for()
            assert await page.locator('.wb-result-row:has-text("已注入")').count() == 2
            assert "暂不支持" in await page.locator("#wb-result").inner_text()
            await page.screenshot(
                path=str(tmp_path / "worldbook-desktop.png"), full_page=True
            )

            # Chips preserve pending text on entry switches and during busy save.
            await page.locator('[data-wb-key="keys"]').fill("星象")
            await page.locator('[data-wb-select="1"]').click()
            await page.locator('[data-wb-select="0"]').click()
            assert (
                await page.locator('.wb-key-editor .tag-chip:has-text("星象")').count()
                == 1
            )
            await page.locator('[data-wb-key="keys"]').fill("星空")
            await page.locator('[data-action="save"]').click()
            await page.locator('#save-state:has-text("卡片已保存")').wait_for()
            saved = (
                await web.client.get(
                    PREFIX + "/card", params={"card_id": card["card_id"]}
                )
            ).json()["data"]
            book = saved["document"]["data"]["character_book"]
            assert book["entries"][0]["keys"] == ["白塔", "法师协会", "星象", "星空"]
            assert book["extensions"] == {"other_app": [1, 2, 3]}
            assert book["entries"][0]["extensions"] == {"untouched": {"preserve": True}}
            assert len(saved["snapshots"]) == 1

            # New entries, IME/Enter chips, and a character budget recompute live.
            await page.locator('[data-wb-action="add"]').click()
            await page.locator('[data-wb-entry="name"]').fill("星之庭院")
            await page.locator('[data-wb-key="keys"]').fill("花园")
            await page.locator('[data-wb-key="keys"]').press("Enter")
            await page.locator('[data-wb-key="keys"]').evaluate("""input=>{
              const clipboardData=new DataTransfer();clipboardData.setData('text/plain','庭院\\n花园、星园');
              input.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData}));
            }""")
            assert (
                await page.locator('.wb-key-editor .tag-chip:has-text("花园")').count()
                == 1
            )
            assert (
                await page.locator('.wb-key-editor .tag-chip:has-text("庭院")').count()
                == 1
            )
            await page.locator('[data-wb-key="keys"]').evaluate("""input=>{
              input.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));
              input.value='星海';input.dispatchEvent(new InputEvent('input',{bubbles:true,isComposing:true}));
              input.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key:'Enter',isComposing:true}));
            }""")
            assert (
                await page.locator('.wb-key-editor .tag-chip:has-text("星海")').count()
                == 0
            )
            await page.locator('[data-wb-key="keys"]').evaluate(
                "input=>input.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true}))"
            )
            await page.locator('[data-wb-key="keys"]').press("Enter")
            assert (
                await page.locator('.wb-key-editor .tag-chip:has-text("星海")').count()
                == 1
            )
            await page.locator('[data-wb-entry="content"]').fill(
                "爱莉与访客常在这里观察星星。"
            )
            await page.locator('[data-wb-test="test"]').fill("去花园看星星")
            await page.locator('#wb-result:has-text("命中：花园")').wait_for()
            await page.locator('[data-wb-book="max_chars"]').fill("1")
            await page.locator('#wb-result:has-text("预算不足")').wait_for()
            assert await page.locator('.wb-result-row:has-text("已注入")').count() == 0
            await page.locator('[data-wb-book="max_chars"]').fill("4000")

            # Invalid numerical edits are actionable and do not silently save defaults.
            await page.locator('[data-wb-book="scan_depth"]').fill("51")
            await page.locator('[data-action="save"]').click()
            await page.locator('#notice-message:has-text("0～50")').wait_for()
            await page.locator('[data-wb-book="scan_depth"]').fill("4")
            await page.locator('[data-action="save"]').click()
            await page.locator('#save-state:has-text("卡片已保存")').wait_for()
            assert (
                await page.locator('[data-wb-entry="name"]').input_value() == "星之庭院"
            )
            assert (
                await page.locator('[data-wb-test="test"]').input_value()
                == "去花园看星星"
            )

            # Mobile and dark mode fit the viewport; preview and editor remain usable.
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.evaluate("document.documentElement.dataset.theme='dark'")
            await page.locator('[data-wb-test="test"]').fill("去白塔看看")
            await page.locator('#wb-result:has-text("命中：白塔")').wait_for()
            assert await page.evaluate(
                "document.documentElement.scrollWidth<=innerWidth"
            )
            await page.locator('[data-wb-select="0"]').click()
            await page.locator('#wb-result:has-text("命中：白塔")').wait_for()
            await page.locator("#notice-close").evaluate("button=>button.click()")
            await page.screenshot(
                path=str(tmp_path / "worldbook-mobile.png"), full_page=True
            )
            await page.set_viewport_size({"width": 320, "height": 700})
            assert await page.evaluate(
                "document.documentElement.scrollWidth<=innerWidth"
            )

            # Archive/lock read-only behavior allows testing but blocks all mutation.
            latest = (
                await web.client.get(
                    PREFIX + "/card", params={"card_id": card["card_id"]}
                )
            ).json()["data"]
            await web.cards.execute(
                Actor("webui", "dashboard-admin", True),
                "lock",
                {
                    "card_id": saved["card_id"],
                    "expected_revision": latest["revision"],
                    "locked": True,
                },
            )
            await page.reload()
            await page.get_by_role("button", name="编辑角色：爱莉", exact=True).click()
            await page.get_by_role("tab", name="世界书", exact=True).click()
            assert await page.locator('[data-wb-entry="content"]').is_disabled()
            assert await page.locator('[data-wb-action="add"]').is_disabled()
            assert await page.locator('[data-wb-test="test"]').is_enabled()
            await page.locator('[data-wb-select="1"]').click()
            assert await page.locator('[data-wb-entry="constant"]').is_disabled()

            # A card with no imported book can create and save its first entry.
            fresh = (
                await web.client.post(
                    PREFIX + "/action",
                    json={"action": "create", "payload": {"card": new_card("新世界")}},
                )
            ).json()["data"]
            await page.reload()
            await page.get_by_role(
                "button", name="编辑角色：新世界", exact=True
            ).click()
            await page.get_by_role("tab", name="世界书", exact=True).click()
            assert await page.locator(".wb-empty").is_visible()
            await page.locator('[data-wb-action="add"]').first.click()
            await page.locator('[data-wb-entry="constant"]').select_option("true")
            await page.locator('[data-wb-entry="content"]').fill("新世界的常驻背景")
            await page.locator('#wb-result:has-text("新世界的常驻背景")').wait_for()
            await page.locator('[data-action="save"]').click()
            await page.locator('#save-state:has-text("卡片已保存")').wait_for()
            fresh = (
                await web.client.get(
                    PREFIX + "/card", params={"card_id": fresh["card_id"]}
                )
            ).json()["data"]
            assert fresh["document"]["data"]["character_book"]["extensions"] == {}
            assert (
                fresh["document"]["data"]["character_book"]["entries"][0]["content"]
                == "新世界的常驻背景"
            )
            assert errors == []
        finally:
            await browser.close()


@pytest.mark.parametrize("name", CARD_MODEL_TOOLS)
async def test_new_model_tools_dispatch_fixed_action(env, name):
    plugin = PersonaStudioPlugin(env.context, env.config)
    plugin.dispatch_card = AsyncMock(return_value='{"ok": true}')
    params = card_tool_parameters(name)
    args = {key: "value" for key in params["required"]}
    tool = FunctionTool(
        name=name,
        description="test",
        parameters=params,
        handler=getattr(plugin, name),
        handler_module_path=type(plugin).__module__,
    )
    context = ContextWrapper(
        context=SimpleNamespace(context=env.context, event=env.event)
    )
    result = [r async for r in FunctionToolExecutor.execute(tool, context, **args)]
    assert result[-1].content[0].text == '{"ok": true}'
    plugin.dispatch_card.assert_awaited_once_with(
        env.event, CARD_MODEL_TOOLS[name], args
    )


@pytest.mark.parametrize("name", CARD_MODEL_TOOLS)
def test_card_schemas_work_with_all_core_provider_declarations(name):
    import jsonschema
    from astrbot.core.agent.tool import ToolSet

    schema = card_tool_parameters(name)
    toolset = ToolSet([FunctionTool(name=name, description="Test", parameters=schema)])
    declarations = [
        toolset.openai_schema()[0]["function"]["parameters"],
        toolset.anthropic_schema()[0]["input_schema"],
        toolset.google_schema()["function_declarations"][0]["parameters"],
    ]
    for declaration in declarations:
        jsonschema.Draft202012Validator.check_schema(declaration)
        assert set(declaration["properties"]) == set(schema["properties"])
        assert declaration["required"] == schema["required"]
        assert "owner" not in declaration["properties"]


async def test_json_declared_avatar_has_correct_mime_and_empty_assets_hide_original(
    web,
):
    import io

    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(image, "JPEG")
    uri = "data:image/jpeg;base64," + base64.b64encode(image.getvalue()).decode()
    document = new_card("JSON 主头像")
    document["data"]["assets"] = [
        {"type": "icon", "name": "main", "uri": uri, "ext": "jpg"}
    ]
    response = await web.client.post(
        PREFIX + "/action",
        json={"action": "create", "payload": {"card": document, "bind": False}},
    )
    card = response.json()["data"]
    assert card["asset_id"] is None
    response = await web.client.get(
        PREFIX + "/avatar", params={"card_id": card["card_id"]}
    )
    avatar = response.json()["data"]
    assert avatar["data_url"] == uri and avatar["warning"] is None
    assert avatar["revision"] == card["revision"]
    response = await web.client.post(
        PREFIX + "/action",
        json={
            "action": "update",
            "payload": {
                "card_id": card["card_id"],
                "expected_revision": card["revision"],
                "data": {"assets": []},
            },
        },
    )
    assert response.status_code == 200
    avatar = (
        await web.client.get(PREFIX + "/avatar", params={"card_id": card["card_id"]})
    ).json()["data"]
    assert avatar["data_url"] is None and avatar["warning"] is None


async def test_bad_avatar_returns_warning_without_failing_card_load_or_json_export(web):
    document = new_card("缺失图片")
    document["data"]["assets"] = [
        {"type": "icon", "name": "main", "uri": "embeded://missing.png"}
    ]
    response = await web.client.post(
        PREFIX + "/action",
        json={"action": "create", "payload": {"card": document, "bind": False}},
    )
    card = response.json()["data"]
    response = await web.client.get(
        PREFIX + "/avatar", params={"card_id": card["card_id"]}
    )
    assert response.status_code == 200
    assert response.json()["data"]["data_url"] is None
    assert "CHARX" in response.json()["data"]["warning"]
    response = await web.client.get(
        PREFIX + "/export", params={"card_id": card["card_id"]}
    )
    assert (
        response.status_code == 200
        and response.json()["data"]["assets"] == document["data"]["assets"]
    )
    response = await web.client.get(
        PREFIX + "/export", params={"card_id": card["card_id"], "format": "png"}
    )
    assert response.status_code == 400


def picture(fmt="PNG", color="red", size=(24, 40)):
    import io

    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, fmt)
    return stream.getvalue()


async def image_upload(web, kind, raw):
    endpoint = "upload-avatar" if kind == "avatar" else "upload-card-image"
    response = await web.client.post(
        PREFIX + "/" + endpoint,
        files={"file": ("image.bin", raw, "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def web_card(web):
    document = new_card("独立图片")
    document["data"]["description"] = "保留角色设定"
    document["data"]["modification_date"] = 1
    response = await web.client.post(
        PREFIX + "/action", json={"action": "create", "payload": {"card": document}}
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def image_save(web, card, images, **data):
    response = await web.client.post(
        PREFIX + "/action",
        json={
            "action": "update",
            "payload": {
                "card_id": card["card_id"],
                "expected_revision": card["revision"],
                "images": images,
                **({"data": data} if data else {}),
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def image_pixels(raw):
    import io

    from PIL import Image

    with Image.open(io.BytesIO(raw)) as image:
        return image.size, image.convert("RGB").getpixel((0, 0))


def decode_image(url):
    return base64.b64decode(url.split(",", 1)[1])


@pytest.mark.parametrize("endpoint", ["card-image", "avatar"])
async def test_gallery_thumbnail_preserves_original_image_and_revision(web, endpoint):
    card = await web_card(web)
    uploaded = await image_upload(
        web, "card_image", picture(size=(1200, 800), color="blue")
    )
    card = await image_save(web, card, {"card_image": uploaded["upload_id"]})
    params = {"card_id": card["card_id"]}
    original = (await web.client.get(PREFIX + "/" + endpoint, params=params)).json()[
        "data"
    ]
    response = await web.client.get(
        PREFIX + "/" + endpoint, params={**params, "thumbnail": "true"}
    )
    assert response.status_code == 200, response.text
    thumbnail = response.json()["data"]
    assert image_pixels(decode_image(thumbnail["data_url"]))[0] == (512, 341)
    assert thumbnail["revision"] == card["revision"]
    assert thumbnail["warning"] == original["warning"]
    assert image_pixels(decode_image(original["data_url"]))[0] == (1200, 800)
    assert (await web.client.get(PREFIX + "/" + endpoint, params=params)).json()[
        "data"
    ] == original


async def test_gallery_list_has_bounded_description_and_tags(web):
    document = new_card("画廊简介")
    document["data"].update(description="星空" * 200, tags=["天文", "日常"])
    created = await web.client.post(
        PREFIX + "/action", json={"action": "create", "payload": {"card": document}}
    )
    assert created.status_code == 200, created.text
    card = created.json()["data"]
    response = await web.client.get(PREFIX + "/cards")
    item = next(
        item
        for item in response.json()["data"]["items"]
        if item["card_id"] == card["card_id"]
    )
    assert item["description"] == "星空" * 120
    assert item["tags"] == ["天文", "日常"]


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
async def test_separate_card_picture_and_avatar_stage_then_save_and_export(web, fmt):
    card = await web_card(web)
    before = web.cards.storage.get(card["card_id"])
    cover = await image_upload(web, "card_image", picture(fmt, color="blue"))
    avatar = await image_upload(web, "avatar", picture(fmt, size=(1024, 768)))
    assert image_pixels(decode_image(avatar["data_url"]))[0] == (512, 384)
    assert web.cards.storage.get(card["card_id"]) == before
    assert len(card["snapshots"]) == 1
    card = await image_save(
        web,
        card,
        {"card_image": cover["upload_id"], "avatar": avatar["upload_id"]},
        description="同时保存的文字",
    )
    assert card["sync"]["status"] == "synced"
    assert card["document"]["data"]["description"] == "同时保存的文字"
    assert card["document"]["data"]["character_version"] == ""
    assert card["document"]["data"]["modification_date"] > 1
    assert len(card["snapshots"]) == 1
    params = {"card_id": card["card_id"]}
    actual_avatar = (await web.client.get(PREFIX + "/avatar", params=params)).json()[
        "data"
    ]
    actual_cover = (await web.client.get(PREFIX + "/card-image", params=params)).json()[
        "data"
    ]
    assert actual_avatar["data_url"] == avatar["data_url"]
    assert image_pixels(decode_image(actual_cover["data_url"]))[0] == (24, 40)
    assert image_pixels(decode_image(actual_cover["data_url"]))[1][2] > 240
    assert actual_cover["revision"] == card["revision"]
    exported = await web.client.get(
        PREFIX + "/export", params={**params, "format": "png"}
    )
    assert exported.status_code == 200
    document, _ = import_card(exported.content)
    assert document == card["document"]
    assert image_pixels(exported.content)[0] == (24, 40)
    assert image_pixels(exported.content)[1][2] > 240
    assert document["data"]["assets"][0]["uri"] == avatar["data_url"]
    # JSON retains a portable avatar without requiring plugin-local files.
    exported = await web.client.get(PREFIX + "/export", params=params)
    assert exported.json() == card["document"]
    assert image_pixels(decode_image(exported.json()["data"]["assets"][0]["uri"]))[
        0
    ] == (512, 384)


async def test_uploaded_images_restore_together_only_when_role_version_changes(web):
    card = await web_card(web)
    cover = await image_upload(web, "card_image", picture(color="blue"))
    avatar = await image_upload(web, "avatar", picture(color="red"))
    card = await image_save(
        web,
        card,
        {"card_image": cover["upload_id"], "avatar": avatar["upload_id"]},
        character_version="1",
    )
    assert len(card["snapshots"]) == 2
    milestone = card["snapshots"][0]["snapshot_id"]
    original_asset, original_document = card["asset_id"], card["document"]
    other = await image_upload(web, "card_image", picture(color="green"))
    card = await image_save(
        web, card, {"card_image": other["upload_id"], "avatar": "ccdefault"}
    )
    assert card["asset_id"] != original_asset and len(card["snapshots"]) == 2
    response = await web.client.post(
        PREFIX + "/action",
        json={
            "action": "restore",
            "payload": {
                "card_id": card["card_id"],
                "expected_revision": card["revision"],
                "snapshot_id": milestone,
            },
        },
    )
    restored = response.json()["data"]
    assert restored["asset_id"] == original_asset
    assert restored["document"] == original_document
    assert len(restored["snapshots"]) == 2
    actual = (
        await web.client.get(PREFIX + "/avatar", params={"card_id": card["card_id"]})
    ).json()["data"]
    assert actual["data_url"] == avatar["data_url"]


async def test_avatar_can_follow_uploaded_card_image_without_changing_other_assets(web):
    card = await web_card(web)
    response = await web.client.post(
        PREFIX + "/action",
        json={
            "action": "update",
            "payload": {
                "card_id": card["card_id"],
                "expected_revision": card["revision"],
                "data": {
                    "assets": [
                        {"type": "background", "name": "scene", "custom": True},
                        {
                            "type": "icon",
                            "name": "main",
                            "uri": "file:///unavailable",
                            "custom": "kept",
                        },
                    ]
                },
            },
        },
    )
    card = response.json()["data"]
    cover = await image_upload(web, "card_image", picture())
    card = await image_save(
        web, card, {"card_image": cover["upload_id"], "avatar": "ccdefault"}
    )
    assets = card["document"]["data"]["assets"]
    assert assets[0] == {"type": "background", "name": "scene", "custom": True}
    assert assets[1]["custom"] == "kept" and assets[1]["uri"] == "ccdefault:"
    actual = (
        await web.client.get(PREFIX + "/avatar", params={"card_id": card["card_id"]})
    ).json()["data"]
    assert image_pixels(decode_image(actual["data_url"])) == image_pixels(
        decode_image(cover["data_url"])
    )


async def test_card_picture_replacement_keeps_old_png_embedded_avatar(web):
    document = new_card("内嵌资源")
    document["data"]["assets"] = [
        {"type": "icon", "name": "main", "uri": "__asset:avatar.png", "ext": "png"}
    ]
    image = blank_png()
    image = (
        image[:-12]
        + _chunk(
            b"tEXt", b"chara-ext-asset_:avatar.png\0" + base64.b64encode(picture())
        )
        + image[-12:]
    )
    response = await web.client.post(
        PREFIX + "/import",
        files={"file": ("old.png", export_png(document, image), "image/png")},
    )
    response = await web.client.post(
        PREFIX + "/action",
        json={
            "action": "create",
            "payload": {"import_id": response.json()["data"]["import_id"]},
        },
    )
    card = response.json()["data"]
    before = (
        await web.cards.avatar(web.cards.web_actor("dashboard-admin"), card["card_id"])
    )["data_url"]
    cover = await image_upload(web, "card_image", picture(color="blue"))
    card = await image_save(web, card, {"card_image": cover["upload_id"]})
    after = (
        await web.cards.avatar(web.cards.web_actor("dashboard-admin"), card["card_id"])
    )["data_url"]
    assert before == after
    exported, _, _ = await web.cards.export(
        web.cards.web_actor("dashboard-admin"), card["card_id"], png=True
    )
    assert image_pixels(exported)[1] == (0, 0, 255)
    assert any(
        p.startswith(b"chara-ext-asset_:avatar.png\0") for _, p in png_chunks(exported)
    )


@pytest.mark.parametrize("failure", ["owner", "kind", "expired", "revision", "locked"])
async def test_image_save_checks_upload_owner_kind_expiry_revision_and_card_lock(
    web, failure
):
    card = await web_card(web)
    upload = await image_upload(web, "avatar", picture())
    images = {"avatar": upload["upload_id"]}
    revision = card["revision"]
    if failure == "owner":
        web.identity["username"] = "different-admin"
    elif failure == "kind":
        images = {"card_image": upload["upload_id"]}
    elif failure == "expired":
        with web.cards.storage.connection:
            web.cards.storage.connection.execute(
                "UPDATE card_image_uploads SET created_at=0"
            )
    elif failure == "revision":
        revision -= 1
    elif failure == "locked":
        web.cards.storage.set_locked(card["card_id"], True)
        revision += 1
    before = web.cards.storage.get(card["card_id"])
    response = await web.client.post(
        PREFIX + "/action",
        json={
            "action": "update",
            "payload": {
                "card_id": card["card_id"],
                "expected_revision": revision,
                "images": images,
            },
        },
    )
    assert response.status_code == 400
    assert web.cards.storage.get(card["card_id"]) == before


@pytest.mark.parametrize(
    "payload",
    [{}, {"file": "local-path"}, {"avatar": None}, {"card_image": "../../secret"}],
)
async def test_malformed_image_save_is_rejected(web, payload):
    card = await web_card(web)
    response = await web.client.post(
        PREFIX + "/action",
        json={
            "action": "update",
            "payload": {
                "card_id": card["card_id"],
                "expected_revision": card["revision"],
                "images": payload,
            },
        },
    )
    assert response.status_code == 400
    assert web.cards.storage.get(card["card_id"])["revision"] == card["revision"]


@pytest.mark.parametrize("endpoint", ["upload-avatar", "upload-card-image"])
async def test_image_upload_rejects_invalid_image_and_requires_authentication(
    web, endpoint
):
    response = await web.client.post(
        PREFIX + "/" + endpoint,
        files={"file": ("fake.png", b"not a picture", "image/png")},
    )
    assert response.status_code == 400
    assert (
        web.cards.storage.connection.execute(
            "SELECT COUNT(*) FROM card_image_uploads"
        ).fetchone()[0]
        == 0
    )
    web.identity["username"] = ""
    response = await web.client.post(
        PREFIX + "/" + endpoint, files={"file": ("image.png", picture(), "image/png")}
    )
    assert response.status_code == 400
    assert (
        web.cards.storage.connection.execute(
            "SELECT COUNT(*) FROM card_image_uploads"
        ).fetchone()[0]
        == 0
    )


async def test_image_upload_strips_card_metadata_and_never_imports_text(web):
    card = await web_card(web)
    other_card = new_card("不应导入的角色")
    other_card["data"]["description"] = "不应覆盖"
    uploaded = await image_upload(web, "card_image", export_png(other_card))
    preview = decode_image(uploaded["data_url"])
    assert not any(k in (b"tEXt", b"zTXt", b"iTXt") for k, _ in png_chunks(preview))
    card = await image_save(web, card, {"card_image": uploaded["upload_id"]})
    assert card["document"]["data"]["name"] == "独立图片"
    assert card["document"]["data"]["description"] == "保留角色设定"
