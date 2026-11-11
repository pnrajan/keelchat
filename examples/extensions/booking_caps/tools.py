"""A read-only tool. The type hints become its input and output schema; the docstring is what the model reads."""

from __future__ import annotations

from keelchat.registry.core import tool

#: sample data; a real tool would call your booking system here (the Tool Gateway applies the timeout and retries)
SLOTS = {
    "haircut": ["Tue 10:00", "Tue 15:30", "Thu 11:00"],
    "colour": ["Wed 09:00", "Fri 13:00"],
    "manicure": ["Mon 12:00", "Sat 10:30"],
}


@tool("slot_search", timeout_s=3.0, retries=1)
async def slot_search(service: str, day: str = "") -> dict[str, list[str]]:
    """Open appointment slots for a service (haircut, colour or manicure), optionally on one day (Mon to Sun)."""
    slots = SLOTS.get(service.strip().lower(), [])
    if day:
        slots = [s for s in slots if s.lower().startswith(day.strip().lower()[:3])]
    return {"slots": slots}
