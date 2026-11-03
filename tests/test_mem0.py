"""User memory in mem0 (self-hosted on an embedded Qdrant, real SiliconFlow embeddings): scoped to workspace, user
and agent, one memory per item, and nothing inferred from what people say."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from yachat.state.mem0_store import InMemoryMem0, Mem0MemoryStore, mem0_from_env, scoped_user
from yachat.state.models import MemoryRecord


@pytest.fixture(params=["fake", "mem0"])
def store(request, tmp_path: Path, monkeypatch) -> Mem0MemoryStore:
    """The same store over the fake backend and over real self-hosted mem0 (embedded Qdrant, SiliconFlow embeddings)."""
    if request.param == "fake":
        return Mem0MemoryStore(InMemoryMem0())
    pytest.importorskip("mem0")
    if not os.getenv("SILICONFLOW_API_KEY"):
        pytest.skip("self-hosted mem0 embeds with SiliconFlow")
    monkeypatch.delenv("MEM0_API_KEY", raising=False)
    monkeypatch.delenv("MEM0_QDRANT_URL", raising=False)
    monkeypatch.setenv("MEM0_QDRANT_PATH", str(tmp_path / "qdrant"))
    s = mem0_from_env()
    assert s is not None
    return s


def rec(tenant: str, user: str, item: str, value: object, agent: str = "hiring-agent") -> MemoryRecord:
    return MemoryRecord(tenant=tenant, user_id=user, agent=agent, item=item, value=value, half_life_s=1e6)


async def test_memories_are_scoped_to_workspace_user_and_agent(store):
    await store.upsert(rec("acme", "u1", "location", "Austin"))
    await store.upsert(rec("globex", "u1", "location", "Boston"))  # same user id, another workspace
    await store.upsert(rec("acme", "u1", "location", "Denver", agent="support-assistant"))
    await store.upsert(rec("acme", "u2", "location", "Miami"))
    assert [r.value for r in await store.list("u1", "hiring-agent", "acme")] == ["Austin"]
    assert [r.value for r in await store.list("u1", "hiring-agent", "globex")] == ["Boston"]
    assert await store.delete_user("u1", tenant="acme") == 2  # both of u1's agents in acme, nothing in globex
    assert await store.list("u1", "hiring-agent", "acme") == []
    assert [r.value for r in await store.list("u1", "hiring-agent", "globex")] == ["Boston"]
    assert [r.value for r in await store.list("u2", "hiring-agent", "acme")] == ["Miami"]


async def test_one_memory_per_item_keeps_structured_values_and_first_created_time(store):
    first = rec("acme", "u1", "job_preferences", {"shift": "night", "roles": ["nurse"]})
    await store.upsert(first)
    await store.upsert(rec("acme", "u1", "job_preferences", {"shift": "day", "roles": ["nurse", "midwife"]}))
    (r,) = await store.list("u1", "hiring-agent", "acme")
    assert r.value == {"shift": "day", "roles": ["nurse", "midwife"]} and r.created_at == first.created_at


async def test_nothing_is_inferred_from_the_text(store):
    await store.upsert(rec("acme", "u1", "location", "Austin"))
    rows = await store._all("acme", "u1", "hiring-agent")
    assert len(rows) == 1 and rows[0].text.startswith("location:")  # stored as written, not rephrased into facts


def test_a_workspace_name_cannot_reach_into_another_scope():
    assert scoped_user("acme", "u1") == "acme/u1"
    with pytest.raises(ValueError):
        scoped_user("acme/u1", "x")  # would collide with acme's user u1


async def test_the_memory_writer_stores_only_policy_items_in_mem0(store, policy_yaml):
    if not os.getenv("SILICONFLOW_API_KEY"):
        pytest.skip("the memory writer calls the configured model")
    from yachat.policy.compiler import build
    from yachat.runtime.llm import ModelConfig
    from yachat.runtime.llm_factory import llm_from_env
    from yachat.state.memory_writer import MemorySnapshot, MemoryWriter

    policy = build(policy_yaml)
    writer = MemoryWriter(store, llm_from_env(), ModelConfig.from_env())
    text = "I want night shift nurse jobs paying at least $25 an hour. My dog is called Rex and I hate Mondays."
    written = await writer.write(MemorySnapshot(user_id="u9", policy=policy, turn_id="t1", user_text=text, consent=True))
    items = {r.item for r in await store.list("u9", policy.agent, policy.tenant)}
    assert "job_preferences" in written and items == set(written)  # only policy-listed items, nothing about Rex or Mondays
    assert not await writer.write(MemorySnapshot(user_id="u10", policy=policy, turn_id="t2", user_text=text, declined=True))
    assert await store.list("u10", policy.agent, policy.tenant) == []  # declined, nothing stored
