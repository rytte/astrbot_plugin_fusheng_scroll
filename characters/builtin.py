"""Install the bundled mascot once, then leave its user's copy untouched."""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import logger

from ..core.domain import Actor, StudioError
from .codec import import_card

BUILTIN_ID = "builtin_zhihui"
BUILTIN_CARD_PATH = (
    Path(__file__).resolve().parents[1] / "resources" / "characters" / "zhihui.png"
)
SYSTEM_ACTOR = Actor("builtin", BUILTIN_ID, True)


async def install_builtin_character(cards):
    """Called after startup validation; never switch sessions or overwrite cards."""
    async with cards.studio.lock:
        if cards.storage.builtin_card_id(BUILTIN_ID) is not None:
            return None

        raw = await asyncio.to_thread(BUILTIN_CARD_PATH.read_bytes)
        document, image = await asyncio.to_thread(import_card, raw)
        if image is None:
            raise StudioError("invalid_card", "内置知绘必须是带角色数据的 PNG 卡片。")
        options = cards.options({})
        cards.validate(document, options)
        cards.check_card_quota(SYSTEM_ACTOR)
        await cards.studio.check_quota(SYSTEM_ACTOR, cards.studio.settings())
        asset_id = await asyncio.to_thread(cards.storage.put_asset, image)
        card = cards.storage.create_builtin(
            BUILTIN_ID, document, options=options, asset_id=asset_id
        )
        # The marker and pending binding are already committed. Any interrupted or
        # failed core write is repaired manually, never by creating another card.
        await cards.sync(SYSTEM_ACTOR, card)
        card = cards.storage.get(card["card_id"])
        cards.studio.storage.audit(
            None, "card.install_builtin", card["card_id"], card["sync_status"]
        )
        if card["sync_status"] == "synced":
            logger.info("已导入内置角色「知绘」，可在角色库中编辑并选择使用。")
        else:
            logger.warning(
                "内置知绘已保存，但人格尚未同步；请在角色库查看状态并手动处理。"
            )
        return card
