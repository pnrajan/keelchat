"""WhatsApp through Meta's WhatsApp Cloud API: a published agent answers a business number.

Meta calls our webhook: a GET to verify it (hub.verify_token), then POSTs for each message, signed with the app secret
(X-Hub-Signature-256, HMAC-SHA256 of the raw body). Replies go out through the Graph API with the number's access
token. The token and app secret are stored encrypted (Fernet, key derived from YACHAT_EMBED_SECRET); the
`WhatsAppSender` port has a fake that keeps an outbox for tests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from abc import ABC, abstractmethod
from typing import Any

from yachat.logging import get_logger
from yachat.runtime.context import ChannelMessage

log = get_logger("whatsapp")
GRAPH = "https://graph.facebook.com/v21.0"


class WhatsAppError(Exception):
    """Sending failed; the message is for logs."""


def _fernet() -> Any:
    from cryptography.fernet import Fernet

    secret = os.getenv("YACHAT_EMBED_SECRET")
    if not secret:
        raise WhatsAppError("YACHAT_EMBED_SECRET must be set to store WhatsApp credentials")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"yachat-whatsapp:" + secret.encode()).digest()))


def seal(value: str) -> str:
    return str(_fernet().encrypt(value.encode()).decode())


def unseal(value: str) -> str:
    return str(_fernet().decrypt(value.encode()).decode())


def signed(app_secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def text_messages(update: dict[str, Any]) -> list[dict[str, Any]]:
    """The text messages in one webhook update (status updates, reactions and media are ignored)."""
    out = []
    for entry in update.get("entry") or []:
        for change in entry.get("changes") or []:
            for m in (change.get("value") or {}).get("messages") or []:
                if m.get("type") == "text" and m.get("from") and (m.get("text") or {}).get("body"):
                    out.append(m)
                elif m.get("type") == "interactive" and m.get("from"):  # a reply chosen from a list
                    picked = (m.get("interactive") or {}).get("list_reply") or (m.get("interactive") or {}).get("button_reply") or {}
                    if picked.get("title"):
                        out.append({"from": m["from"], "id": m.get("id"), "text": {"body": picked["title"]}})
    return out


class WhatsAppSender(ABC):
    @abstractmethod
    async def send(self, phone_number_id: str, token: str, to: str, message: ChannelMessage) -> None: ...


class CloudSender(WhatsAppSender):
    async def send(self, phone_number_id: str, token: str, to: str, message: ChannelMessage) -> None:
        import httpx

        body: dict[str, Any] = {"messaging_product": "whatsapp", "to": to}
        if message.payload.get("type") == "interactive":
            inter = dict(message.payload["interactive"])
            inter.setdefault("body", {"text": message.text})
            inter["action"] = {"button": "Choose", **inter.get("action", {})}
            body |= {"type": "interactive", "interactive": inter}
        else:
            body |= {"type": "text", "text": {"body": message.text}}
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.post(f"{GRAPH}/{phone_number_id}/messages", json=body, headers={"authorization": f"Bearer {token}"})
                r.raise_for_status()
        except httpx.HTTPError as e:
            raise WhatsAppError(f"graph api: {e}") from e


class FakeWhatsAppSender(WhatsAppSender):
    def __init__(self) -> None:
        self.outbox: list[tuple[str, str, ChannelMessage]] = []

    async def send(self, phone_number_id: str, token: str, to: str, message: ChannelMessage) -> None:
        self.outbox.append((phone_number_id, to, message))
