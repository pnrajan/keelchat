"""Memory writer: runs after the reply, off the critical path. Typed extraction per field; privacy rules live here."""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from keelchat.logging import get_logger, redact_fields
from keelchat.policy.compiler import CompiledMemoryItem, CompiledPolicy
from keelchat.policy.memory_types import validate_value
from keelchat.runtime.llm import LLMClient, LLMRequest, ModelConfig
from keelchat.state.models import MemoryRecord
from keelchat.state.store import MemoryStore

log = get_logger("memory")


@dataclass
class MemorySnapshot:
    """Everything the writer needs, copied so the turn can finish without it."""

    user_id: str
    policy: CompiledPolicy
    turn_id: str
    user_text: str
    validated_answers: dict[str, Any] = dc_field(default_factory=dict)
    consent: bool = False
    #: the user answered no to being remembered: nothing is written, sensitive or not
    declined: bool = False
    flagged: bool = False


def extraction_schema(item: CompiledMemoryItem) -> dict[str, Any]:
    """One structured call per field: {found: bool, volunteered: bool, value: <field type>}."""
    return {
        "type": "object",
        "properties": {"found": {"type": "boolean"}, "volunteered": {"type": "boolean"}, "value": {"anyOf": [item.schema_, {"type": "null"}]}},
        "required": ["found", "volunteered", "value"],
        "additionalProperties": False,
    }


def decoded(value: Any, schema: dict[str, Any]) -> Any:
    """Some models return a structured value JSON-encoded as a string ('{"shift": "night"}'). When the item is not a
    string, decode it so the type check sees the value, not its encoding; anything else is left as given."""
    if isinstance(value, str) and schema.get("type") not in (None, "string"):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


class MemoryWriter:
    def __init__(self, store: MemoryStore, llm: LLMClient, models: ModelConfig):
        self.store, self.llm, self.models = store, llm, models

    async def _put(self, snap: MemorySnapshot, item: CompiledMemoryItem, value: Any, volunteered: bool) -> bool:
        if not validate_value(item.schema_, value):
            log.info("memory value dropped: wrong type", item=item.name)
            return False
        if item.sensitive and not (snap.consent and volunteered):
            log.info("sensitive item withheld", item=item.name, consent=snap.consent, volunteered=volunteered)
            return False
        await self.store.upsert(
            MemoryRecord(
                user_id=snap.user_id,
                tenant=snap.policy.tenant,
                agent=snap.policy.agent,
                item=item.name,
                value=value,
                source_turn_id=snap.turn_id,
                half_life_s=item.half_life_s,
                sensitive=item.sensitive,
            )
        )
        return True

    async def _extract(self, snap: MemorySnapshot, item: CompiledMemoryItem) -> tuple[Any, bool] | None:
        system = (
            f"Extract the memory field '{item.name}' from the user's message. Instruction: {item.extract}. "
            "Set found=false when the message does not state it. Set volunteered=true only when the user stated it themselves."
        )
        req = LLMRequest(
            "memory_extract",
            snap.policy.models.get("small", self.models.classify),  # the small tier pinned at build
            system,
            [{"role": "user", "content": snap.user_text}],
            max_tokens=300,
            effort=self.models.effort_classify,
            metadata={"item": item.name, "schema": item.schema_, "instruction": item.extract},
        )
        out = await self.llm.structured(req, extraction_schema(item))
        if not out.get("found") or out.get("value") is None:
            return None
        return decoded(out["value"], item.schema_), bool(out.get("volunteered"))

    async def write(self, snap: MemorySnapshot) -> list[str]:
        if snap.flagged or snap.declined or not snap.policy.memory:
            return []
        written: list[str] = []
        for item in snap.policy.memory:
            if item.from_:
                step, field = item.from_.split(".", 1)
                if field in snap.validated_answers and snap.validated_answers[field] is not None:
                    if await self._put(snap, item, snap.validated_answers[field], volunteered=True):
                        written.append(item.name)
                continue
            if item.name in snap.validated_answers and snap.validated_answers[item.name] is not None:
                if await self._put(snap, item, snap.validated_answers[item.name], volunteered=True):
                    written.append(item.name)
                continue
            if not snap.user_text.strip():
                continue
            try:
                found = await self._extract(snap, item)
            except Exception as e:
                log.error("memory extraction failed", item=item.name, error=f"{e.__class__.__name__}: {e}")
                continue
            if found is not None and await self._put(snap, item, found[0], found[1]):
                written.append(item.name)
        if written:
            log.info("memory written", items=redact_fields({w: "" for w in written}, snap.policy.sensitive_items), count=len(written))
        return written

    async def export(self, user_id: str, agent: str, tenant: str = "default") -> list[dict[str, Any]]:
        return [r.model_dump(mode="json") for r in await self.store.list(user_id, agent, tenant)]

    async def delete(self, user_id: str, agent: str | None = None, tenant: str = "default") -> int:
        return await self.store.delete_user(user_id, agent, tenant)
