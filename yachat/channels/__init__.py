"""Channels: web, slack, sms, whatsapp. Each owns its limits and compliance."""

from __future__ import annotations

from yachat.channels.base import Channel
from yachat.channels.slack import Slack
from yachat.channels.sms import Sms
from yachat.channels.web import Web
from yachat.channels.whatsapp import WhatsApp

CHANNELS: dict[str, Channel] = {c.name: c for c in (Web(), Slack(), Sms(), WhatsApp())}

__all__ = ["CHANNELS", "Channel"]
