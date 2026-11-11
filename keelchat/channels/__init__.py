"""Channels: web, slack, sms, whatsapp. Each owns its limits and compliance."""

from __future__ import annotations

from keelchat.channels.base import Channel
from keelchat.channels.slack import Slack
from keelchat.channels.sms import Sms
from keelchat.channels.web import Web
from keelchat.channels.whatsapp import WhatsApp

CHANNELS: dict[str, Channel] = {c.name: c for c in (Web(), Slack(), Sms(), WhatsApp())}

__all__ = ["CHANNELS", "Channel"]
