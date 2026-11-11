from __future__ import annotations

from typing import Any

from keelchat.channels.base import Channel, items_as_text, split_text
from keelchat.runtime.context import ChannelMessage, InboundMessage, OutboundMessage

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "baja"}


class Sms(Channel):
    name = "sms"
    LIMIT = 160

    def normalize(self, payload: dict[str, Any]) -> InboundMessage:
        return InboundMessage(text=str(payload.get("Body", "")), channel="sms", user_id=str(payload["From"]), channel_message_id=payload.get("MessageSid"))

    def payload_from_text(self, text: str, user_id: str, message_id: str | None) -> dict[str, Any]:
        return {"Body": text, "From": user_id, "MessageSid": message_id}

    def compliance(self, payload: dict[str, Any]) -> ChannelMessage | None:
        if str(payload.get("Body", "")).strip().lower() in STOP_WORDS:
            return ChannelMessage("You have been unsubscribed and will receive no more messages. Reply START to opt back in.", {"compliance": "stop"})
        return None

    def render(self, message: OutboundMessage) -> list[ChannelMessage]:
        text = message.text + ("\n" + items_as_text(message.items) if message.items and "as posted" not in message.text else "")
        chunks = split_text(text, self.LIMIT - 8)
        if len(chunks) == 1:
            return [ChannelMessage(chunks[0])]
        return [ChannelMessage(f"({i}/{len(chunks)}) {c}") for i, c in enumerate(chunks, 1)]
