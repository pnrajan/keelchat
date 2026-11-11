"""Numbers in an analysis must come from the data. A number is grounded when it matches (within rounding) a value in
the query result, its summary statistics or the sandbox output, or a simple derivation of two of them: difference,
ratio or percentage change. Small integers (counts like "top 3", months) are always allowed."""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

_NUMBER = re.compile(r"(?<![A-Za-z_])-?\d[\d,]*(?:\.\d+)?")
#: pairwise derivations are only computed over this many base values (40k pairs)
MAX_PAIR_BASE = 200


def numbers_in_text(text: str) -> list[float]:
    out = []
    for m in _NUMBER.findall(text or ""):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def numbers_in(value: Any) -> list[float]:
    """Every number in a JSON-like value (strings are scanned for numbers too)."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, int | float):
        return [float(value)] if math.isfinite(value) else []
    if isinstance(value, str):
        return numbers_in_text(value)
    if isinstance(value, dict):
        return [n for v in value.values() for n in numbers_in(v)]
    if isinstance(value, list | tuple):
        return [n for v in value for n in numbers_in(v)]
    return []


def _close(source: float, claim: float) -> bool:
    """The claim is the source rounded: to the digits the claim shows (0.0086 for 0.008578), or to a few significant
    figures. Either sign convention for decreases. No absolute floor: it would let any small statistic match."""
    if source == claim:
        return True
    shown = max(0, -int(Decimal(repr(claim)).normalize().as_tuple().exponent))
    return abs(abs(source) - abs(claim)) <= max(0.006 * max(abs(source), abs(claim)), 0.5 * 10.0**-shown)


class Grounding:
    def __init__(self, sources: Iterable[float], pair_base: Iterable[float] | None = None):
        self.values = sorted({round(v, 6) for v in sources})
        base = sorted({round(v, 6) for v in (pair_base if pair_base is not None else self.values)}, key=abs, reverse=True)[:MAX_PAIR_BASE]
        derived: set[float] = set()
        for a, b in itertools.permutations(base, 2):
            derived.add(a - b)
            if b:
                derived |= {a / b, a / b * 100, (a - b) / b, (a - b) / b * 100}
        self.derived = sorted(derived)

    @staticmethod
    def _near(sorted_values: list[float], v: float) -> bool:
        import bisect

        for target in (v, -v):
            i = bisect.bisect_left(sorted_values, target)
            for j in (i - 1, i, i + 1):
                if 0 <= j < len(sorted_values) and _close(sorted_values[j], target):
                    return True
        return False

    def grounded(self, v: float) -> bool:
        if float(v).is_integer() and abs(v) <= 12:
            return True
        return self._near(self.values, v) or self._near(self.derived, v)

    def ungrounded(self, value: Any) -> list[float]:
        return [n for n in numbers_in(value) if not self.grounded(n)]
