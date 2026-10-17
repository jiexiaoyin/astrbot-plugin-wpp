"""WPP 平台消息事件类。

AstrBot 约定:
  - 基类 `AstrMessageEvent.send()` 只上报 metric, 平台必须重写才能真正发送
  - 发送逻辑走 `wpp_send.send_chain()` (event.send / send_by_session 共用)
  - 最后调用 `super().send(message)` 完成基类 metric 上报
"""

from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent, MessageChain

from .wpp_send import send_chain

if TYPE_CHECKING:
    from .wpp_client import WppClient


class WppMessageEvent(AstrMessageEvent):
    def __init__(self, *args, api: "WppClient | None" = None, account_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._api = api
        self.account_id = account_id

    async def send(self, message: MessageChain) -> None:
        """真正发送到微信, 然后走基类 metric 上报。"""
        try:
            if self._api is not None:
                await send_chain(
                    self._api,
                    self.session.session_id,
                    list(message.chain),
                    account_id=self.account_id,
                )
        finally:
            await super().send(message)


__all__ = ["WppMessageEvent"]
