"""A tool shipped by another team. The entry point calls `register`, which imports the decorated function."""

from __future__ import annotations

from yachat.registry.core import tool


@tool("salary_lookup", timeout_s=3.0)
async def salary_lookup(title: str, location: str = "") -> dict[str, str]:
    """Median salary for a title in a location (sample data)."""
    return {"title": title, "location": location, "median": "$120k"}


def register() -> None:
    return None  # importing this module registered the tool
