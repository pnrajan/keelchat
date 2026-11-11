from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from keelchat.runtime.context import ChannelMessage, InboundMessage, OutboundMessage


def split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for word in text.split(" "):
        while len(word) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(word[:limit])
            word = word[limit:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= limit:
            cur += " " + word
        else:
            parts.append(cur)
            cur = word
    if cur:
        parts.append(cur)
    return parts


def listable(item: dict[str, Any]) -> bool:
    """Items a text channel lists. `detail: true` items (a full analysis, say) are for rich clients only."""
    return item.get("type") != "resume_token" and not item.get("detail")


def items_as_text(items: list[dict[str, Any]]) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        if not listable(it):
            continue
        label = it.get("title") or it.get("label") or it.get("text") or str(it)
        extra = f" ({it['location']})" if it.get("location") else ""
        lines.append(f"{i}. {label}{extra}")
    return "\n".join(lines)


class Channel(ABC):
    name: str

    @abstractmethod
    def normalize(self, payload: dict[str, Any]) -> InboundMessage: ...

    @abstractmethod
    def render(self, message: OutboundMessage) -> list[ChannelMessage]: ...

    def payload_from_text(self, text: str, user_id: str, message_id: str | None) -> dict[str, Any]:
        """Wrap plain text as this channel's payload so text turns get the same compliance handling."""
        return {"text": text, "user_id": user_id, "message_id": message_id}

    def compliance(self, payload: dict[str, Any]) -> ChannelMessage | None:
        """Channel-level compliance (e.g. SMS STOP), handled before the pipeline. None → proceed."""
        return None
