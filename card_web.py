from __future__ import annotations

import asyncio
import functools
from urllib.parse import quote

from astrbot.api import logger
from astrbot.api.web import error_response, request
from starlette.responses import Response

from .card_assets import CardImage, data_image, prepare_image
from .cards import MAX_UPLOAD_BYTES
from .domain import StudioError, check_payload

PREFIX = "/astrbot_plugin_persona_studio"


class CardWeb:
    def __init__(self, context, cards):
        self.context = context
        self.cards = cards
        self.handlers = []

    def register(self):
        for route, method, handler in (
            ("/cards", "GET", self.list_cards),
            ("/card", "GET", self.get_card),
            ("/action", "POST", self.action),
            ("/import", "POST", self.upload),
            ("/export", "GET", self.export),
            ("/avatar", "GET", self.avatar),
            ("/card-image", "GET", self.card_image),
            ("/upload-card-image", "POST", self.upload_card_image),
            ("/upload-avatar", "POST", self.upload_avatar),
        ):
            wrapped = self.guard(handler)
            self.context.register_web_api(
                PREFIX + route, wrapped, [method], "人格工坊角色卡管理"
            )
            self.handlers.append(wrapped)

    def unregister(self):
        self.context.registered_web_apis[:] = [
            api
            for api in self.context.registered_web_apis
            if api[1] not in self.handlers
        ]

    def actor(self):
        # This identity is supplied by AstrBot's authenticated dashboard route.
        # It is never accepted from a JSON body, query string or chat message.
        return self.cards.web_actor(request.username)

    def guard(self, handler):
        @functools.wraps(handler)
        async def wrapped():
            try:
                actor = self.actor()
                return await handler(actor)
            except StudioError as exc:
                return error_response(
                    f"{exc.code}：{exc}",
                    status_code=400,
                    data={"code": exc.code, "details": exc.details},
                )
            except Exception as exc:
                logger.error("Character card Web API failed (%s).", type(exc).__name__)
                return error_response(
                    "操作未全部完成，请刷新查看当前状态。", status_code=500
                )

        return wrapped

    async def list_cards(self, actor):
        try:
            offset = int(request.query.get("offset", "0"))
            limit = int(request.query.get("limit", "100"))
        except ValueError as exc:
            raise StudioError("invalid_payload", "分页参数必须为整数。") from exc
        data = await self.cards.execute(
            actor,
            "list",
            {
                "offset": offset,
                "limit": limit,
                "deleted": request.query.get("deleted") == "true",
            },
        )
        return {"status": "ok", "data": data}

    async def get_card(self, actor):
        data = await self.cards.execute(
            actor, "get", {"card_id": request.query.get("card_id")}
        )
        return {"status": "ok", "data": data}

    async def action(self, actor):
        if len(await request.body()) > 3 * 1024 * 1024:
            raise StudioError("limit_exceeded", "请求内容过大。")
        body = await request.json()
        check_payload(body, {"action", "payload"}, {"action", "payload"})
        result = await self.cards.execute(actor, body["action"], body["payload"])
        return {"status": "ok", "data": result}

    async def upload(self, actor):
        files = await request.files()
        file = files.get("file")
        if file is None:
            raise StudioError("invalid_payload", "请上传 JSON 或 PNG 文件。")
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        result = await self.cards.prepare_import(actor, raw)
        return {"status": "ok", "data": result}

    async def export(self, actor):
        raw_version = request.query.get("version")
        if raw_version not in (None, "3"):
            raise StudioError(
                "invalid_payload", "导出固定使用 Character Card V3；V2 仅用于导入兼容。"
            )
        fmt = request.query.get("format", "json")
        if fmt not in ("json", "png"):
            raise StudioError("invalid_payload", "导出格式只能是 json 或 png。")
        content, media_type, name = await self.cards.export(
            actor,
            request.query.get("card_id"),
            png=fmt == "png",
        )
        filename = quote(name[:80], safe="") + "." + fmt
        return Response(
            content,
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename=character.{fmt}; filename*=UTF-8''{filename}",
                "Cache-Control": "no-store",
            },
        )

    async def upload_card_image(self, actor):
        return await self.upload_image(actor, "card_image")

    async def upload_avatar(self, actor):
        return await self.upload_image(actor, "avatar")

    async def upload_image(self, actor, kind):
        files = await request.files()
        file = files.get("file")
        if file is None:
            raise StudioError("invalid_payload", "请选择 PNG、JPEG 或 WebP 图片。")
        raw = await file.read(MAX_UPLOAD_BYTES + 1)
        result = await self.cards.prepare_image(actor, raw, kind)
        return {"status": "ok", "data": result}

    async def card_image(self, actor):
        result = await self.cards.card_image(actor, request.query.get("card_id"))
        return {
            "status": "ok",
            "data": await self.thumbnail(result),
        }

    async def avatar(self, actor):
        result = await self.cards.avatar(actor, request.query.get("card_id"))
        return {
            "status": "ok",
            "data": await self.thumbnail(result),
        }

    async def thumbnail(self, result):
        if request.query.get("thumbnail") == "true" and result["data_url"]:
            raw = await asyncio.to_thread(
                lambda: prepare_image(data_image(result["data_url"]), avatar=True)
            )
            return {**result, "data_url": CardImage(raw, "image/png").data_url}
        return result
