from __future__ import annotations

from typing import Any

from keelchat.channels.base import Channel, listable, split_text
from keelchat.runtime.context import ChannelMessage, InboundMessage, OutboundMessage


class WhatsApp(Channel):
    name = "whatsapp"
    LIMIT = 4096
    LIST_MAX = 10

    def normalize(self, payload: dict[str, Any]) -> InboundMessage:
        body = payload.get("text", {})
        text = body.get("body", "") if isinstance(body, dict) else str(body)
        return InboundMessage(text=text, channel="whatsapp", user_id=str(payload["from"]), channel_message_id=payload.get("id"))

    def payload_from_text(self, text: str, user_id: str, message_id: str | None) -> dict[str, Any]:
        return {"from": user_id, "id": message_id, "text": {"body": text}}

    def render(self, message: OutboundMessage) -> list[ChannelMessage]:
        out = [ChannelMessage(c, {"type": "text"}) for c in split_text(message.text, self.LIMIT)]
        rows = [
            {"id": str(it.get("id", i)), "title": str(it.get("title", ""))[:24], "description": str(it.get("location", ""))[:72]}
            for i, it in enumerate(message.items)
            if listable(it)
        ][: self.LIST_MAX]
        if rows:
            out.append(ChannelMessage("Choose one", {"type": "interactive", "interactive": {"type": "list", "action": {"sections": [{"rows": rows}]}}}))
        return out
