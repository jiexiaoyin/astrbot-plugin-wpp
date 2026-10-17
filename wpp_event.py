"""WPP 平台消息事件类。

AstrBot 约定:
  - 基类 `AstrMessageEvent.send()` 只上报 metric, 平台必须重写才能真正发送
  - 发送逻辑走 `wpp_send.send_chain()` (event.send / send_by_session 共用)
  - 最后调用 `super().send(message)` 完成基类 metric 上报
"""

from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Reply

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
                chain = list(message.chain)
                self._fill_reply_sender(chain)
                await send_chain(
                    self._api,
                    self.session.session_id,
                    chain,
                    account_id=self.account_id,
                )
        finally:
            await super().send(message)

    def _fill_reply_sender(self, chain: list) -> None:
        """补全核心生成的 Reply 的发送者信息。

        AstrBot 的 ResultDecorateStage 只设置 `Reply(id=event.message_obj.message_id)`,
        但 WPP 引用 XML 需要 fromusr 才能让老客户端回查昵称。只有当 Reply 引用的
        正是当前事件消息时才补, 避免覆盖插件/模型自己构造的引用。
        """
        msg_obj = getattr(self, "message_obj", None)
        if msg_obj is None:
            return
        current_id = str(getattr(msg_obj, "message_id", "") or "")
        sender = getattr(msg_obj, "sender", None)
        sender_id = str(getattr(sender, "user_id", "") or "")
        nickname = str(getattr(sender, "nickname", "") or "")
        for comp in chain:
            if not isinstance(comp, Reply):
                continue
            if current_id and str(getattr(comp, "id", "")) != current_id:
                continue
            if not getattr(comp, "sender_id", None) and sender_id:
                comp.sender_id = sender_id
            if not getattr(comp, "sender_nickname", "") and nickname:
                comp.sender_nickname = nickname


__all__ = ["WppMessageEvent"]
