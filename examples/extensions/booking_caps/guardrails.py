"""An output guardrail: replies never promise a price, because prices depend on the stylist."""

from __future__ import annotations

import re

from keelchat.registry.core import GuardContext, Guardrail, GuardResult, guardrail

PRICE = re.compile(r"[$€£]\s?\d+(?:[.,]\d{2})?")


@guardrail("no_price_promises", stage="output")
class NoPricePromises(Guardrail):
    """Removes quoted prices from replies; the salon confirms prices in person."""

    async def check(self, text: str, ctx: GuardContext) -> GuardResult:
        if not PRICE.search(text):
            return GuardResult()
        return GuardResult(flagged=True, reason="price quoted", redacted=PRICE.sub("(price confirmed at the salon)", text))
