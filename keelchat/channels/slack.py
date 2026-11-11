from __future__ import annotations

from typing import Any

from keelchat.channels.base import Channel, items_as_text, split_text
from keelchat.runtime.context import ChannelMessage, InboundMessage, OutboundMessage


class Slack(Channel):
    name = "slack"
    LIMIT = 4000

    def normalize(self, payload: dict[str, Any]) -> InboundMessage:
        ev = payload.get("event", payload)
        return InboundMessage(text=str(ev.get("text", "")), channel="slack", user_id=str(ev["user"]), channel_message_id=ev.get("ts"))

    def payload_from_text(self, text: str, user_id: str, message_id: str | None) -> dict[str, Any]:
        return {"event": {"text": text, "user": user_id, "ts": message_id}}

    def render(self, message: OutboundMessage) -> list[ChannelMessage]:
        text = message.text + ("\n" + items_as_text(message.items) if message.items and "as posted" not in message.text else "")
        return [ChannelMessage(c, {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": c}}]}) for c in split_text(text, self.LIMIT)]
