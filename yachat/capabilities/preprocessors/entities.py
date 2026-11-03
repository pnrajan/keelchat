from __future__ import annotations

import re
from datetime import date, timedelta

from yachat.registry.core import PreProcessor, preprocessor
from yachat.runtime.context import TurnContext

PHONE = re.compile(r"(?<!\d)(?:\+?1[ -.]?)?\(?(\d{3})\)?[ -.]?(\d{3})[ -.]?(\d{4})(?!\d)")
DATE_NUM = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
ADDRESS = re.compile(r"\b\d{1,6}\s+[A-Za-z0-9.'\- ]{2,40}\b(street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|court|ct)\b\.?", re.IGNORECASE)
RELATIVE = {"today": 0, "tomorrow": 1, "mañana": 1, "hoy": 0}


@preprocessor("entity_normalization", order=40)
class EntityNormalization(PreProcessor):
    """Finds dates, phone numbers and addresses; normalizes phones to E.164 and numeric dates to ISO in `ctx.text`."""

    async def run(self, ctx: TurnContext) -> TurnContext:
        text = ctx.text
        phones = [f"+1{a}{b}{c}" for a, b, c in PHONE.findall(text)]
        text = PHONE.sub(lambda m: f"+1{m.group(1)}{m.group(2)}{m.group(3)}", text)
        dates: list[str] = []

        def iso(m: re.Match[str]) -> str:
            mm, dd, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                d = date(yyyy, mm, dd).isoformat()
            except ValueError:
                return m.group(0)
            dates.append(d)
            return d

        text = DATE_NUM.sub(iso, text)
        for word, delta in RELATIVE.items():
            if re.search(rf"\b{word}\b", text, re.IGNORECASE):
                dates.append((date.today() + timedelta(days=delta)).isoformat())
        addresses = [m.group(0).strip() for m in ADDRESS.finditer(text)]
        ctx.entities = {k: v for k, v in (("phones", phones), ("dates", dates), ("addresses", addresses)) if v}
        ctx.text = text
        return ctx
