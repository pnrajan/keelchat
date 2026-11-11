from __future__ import annotations

from typing import Any

from keelchat.channels.base import Channel
from keelchat.runtime.context import ChannelMessage, InboundMessage, OutboundMessage


class Web(Channel):
    name = "web"

    def normalize(self, payload: dict[str, Any]) -> InboundMessage:
        return InboundMessage(
            text=str(payload.get("text", "")),
            channel="web",
            user_id=str(payload["user_id"]),
            channel_message_id=payload.get("message_id"),
            action=payload.get("action"),
            metadata=dict(payload.get("metadata", {})),
        )

    def render(self, message: OutboundMessage) -> list[ChannelMessage]:
        return [ChannelMessage(message.text, {"items": message.items, "language": message.language, "end": message.end_of_conversation})]
