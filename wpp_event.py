"""WPP 平台消息事件类。

AstrBot 回复通过 event.send() 发送 — 基类 AstrMessageEvent.send() 只上报 metric,
必须由平台子类重写才能真正发到微信。参考 aiocqhttp/dingtalk 事件类范式。
"""

import logging
from typing import TYPE_CHECKING

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Image

if TYPE_CHECKING:
    from .wpp_client import WppClient


class WppMessageEvent(AstrMessageEvent):
    def __init__(self, *args, api=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._api = api

    async def send(self, message: MessageChain) -> None:
        """重写: 真正发消息到微信。"""
        to_wxid = self.session.session_id

        text_parts = []
        image_parts = []
        for comp in message.chain:
            if isinstance(comp, Plain):
                text_parts.append(comp.text)
            elif isinstance(comp, Image):
                image_parts.append(comp.file or comp.url or "")

        for img in image_parts:
            try:
                await self._api.send_image(to_wxid, img)
                logger.info(f"[WPP] event.send image OK -> {to_wxid}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] event.send image failed: {e}")

        if text_parts:
            text = "\n".join(text_parts)
            try:
                r = await self._api.send_text(to_wxid, text)
                logger.info(f"[WPP] event.send text OK -> {to_wxid}: {str(r.get('Message'))[:80]}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[WPP] event.send text failed: {e}")

        await super().send(message)
