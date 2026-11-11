"""A channel: how one messaging platform's payloads become inbound messages, and replies become its messages.

Channels own their limits and compliance. Registering is adding an instance to `CHANNELS`; sessions started with
`channel: "telegram"` then render through it. Delivery (calling Telegram's sendMessage) belongs in your webhook
handler, outside the runtime."""

from __future__ import annotations

from typing import Any

from keelchat.channels import CHANNELS
from keelchat.channels.base import Channel, items_as_text, split_text
from keelchat.runtime.context import ChannelMessage, InboundMessage, OutboundMessage


class Telegram(Channel):
    name = "telegram"
    LIMIT = 4096

    def normalize(self, payload: dict[str, Any]) -> InboundMessage:
        msg = payload.get("message") or {}
        chat = msg.get("chat") or {}
        return InboundMessage(
            text=str(msg.get("text", "")), channel=self.name, user_id=f"tg:{chat.get('id', '')}", channel_message_id=str(msg.get("message_id", "")) or None
        )

    def payload_from_text(self, text: str, user_id: str, message_id: str | None) -> dict[str, Any]:
        return {"message": {"text": text, "chat": {"id": user_id.removeprefix("tg:")}, "message_id": message_id}}

    def render(self, message: OutboundMessage) -> list[ChannelMessage]:
        text = message.text + ("\n" + items_as_text(message.items) if message.items else "")
        return [ChannelMessage(chunk, {"parse_mode": "HTML"}) for chunk in split_text(text, self.LIMIT)]


CHANNELS.setdefault(Telegram.name, Telegram())
