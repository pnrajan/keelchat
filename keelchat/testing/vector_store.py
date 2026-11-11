"""What every vector store must do for knowledge search, as one check. Run it against yours:

    from keelchat.testing import check_vector_store

    async def test_my_store():
        await check_vector_store(MyStore(...))   # an empty store

It raises AssertionError naming the rule that failed. The store must be empty at the start; it is empty again at the end.
"""

from __future__ import annotations

import math

from keelchat.knowledge.models import Chunk
from keelchat.knowledge.vectorstore import VectorIndex


def _unit(*xs: float) -> list[float]:
    n = math.sqrt(sum(x * x for x in xs)) or 1.0
    return [x / n for x in xs]


def _chunk(cid: str, doc: str, dataset: str, level: int = 0, text: str = "") -> Chunk:
    return Chunk(
        id=cid,
        doc_id=doc,
        title=f"title {doc}",
        text=text or f"text {cid}",
        context="ctx",
        level=level,
        strategy="simple",
        source=f"src/{doc}",
        dataset=dataset,
    )


async def check_vector_store(store: VectorIndex) -> None:
    """Checks, in order: an empty store; nearest first with whole chunks back; one dataset per search; the level cap;
    an upsert of the same chunk id replaces it; count per dataset; delete removes a document's chunks only."""
    assert await store.count() == 0, "the store must start empty"
    assert await store.search(_unit(1, 0, 0, 0), k=3, dataset="t/a") == [], "searching an empty store returns no hits"

    a1, a2, a3 = _chunk("d1:0", "d1", "t/a"), _chunk("d1:1", "d1", "t/a"), _chunk("d2:0", "d2", "t/a", level=1)
    b1 = _chunk("d3:0", "d3", "t/b")
    shared = _chunk("d4:0", "d4", "")
    await store.upsert([a1, a2, a3, b1, shared], [_unit(1, 0, 0, 0), _unit(0.6, 0.8, 0, 0), _unit(0, 1, 0, 0), _unit(1, 0, 0, 0), _unit(1, 0, 0, 0)])

    hits = await store.search(_unit(1, 0.05, 0, 0), k=2, dataset="t/a")
    assert [h.chunk.id for h in hits] == ["d1:0", "d1:1"], f"nearest first within the dataset, k at most: got {[h.chunk.id for h in hits]}"
    assert hits[0].score >= hits[1].score and 0.9 < hits[0].score <= 1.0001, f"scores are cosine similarity, highest first: {[h.score for h in hits]}"
    got = hits[0].chunk
    assert (got.doc_id, got.title, got.text, got.context, got.level, got.source, got.dataset) == (a1.doc_id, a1.title, a1.text, "ctx", 0, a1.source, "t/a"), (
        f"a hit carries the whole chunk back: {got}"
    )

    assert {h.chunk.id for h in await store.search(_unit(1, 0, 0, 0), k=10, dataset="t/a")} == {"d1:0", "d1:1", "d2:0"}, "a search sees only its dataset"
    assert [h.chunk.id for h in await store.search(_unit(1, 0, 0, 0), k=10, dataset="t/b")] == ["d3:0"], "another dataset is kept apart"
    assert [h.chunk.id for h in await store.search(_unit(1, 0, 0, 0), k=10, dataset="")] == ["d4:0"], 'dataset "" is the shared corpus only'
    assert "d2:0" not in {h.chunk.id for h in await store.search(_unit(0, 1, 0, 0), k=10, max_level=0, dataset="t/a")}, "max_level leaves out higher levels"

    await store.upsert([_chunk("d1:0", "d1", "t/a", text="replaced")], [_unit(0, 0, 1, 0)])
    top = await store.search(_unit(0, 0, 1, 0), k=1, dataset="t/a")
    assert top and top[0].chunk.id == "d1:0" and top[0].chunk.text == "replaced", "an upsert with an existing id replaces that chunk"

    assert (await store.count(), await store.count("t/a"), await store.count("t/b"), await store.count("")) == (5, 3, 1, 1), (
        "count in all scopes and per dataset"
    )

    await store.delete_doc("d1")
    assert {h.chunk.id for h in await store.search(_unit(1, 0, 0, 0), k=10, dataset="t/a")} == {"d2:0"}, "delete_doc removes every chunk of that document"
    assert await store.count("t/b") == 1, "delete_doc leaves other documents alone"
    for doc in ("d2", "d3", "d4"):
        await store.delete_doc(doc)
    await store.delete_doc("missing")  # deleting a document with no chunks is not an error
    assert await store.count() == 0, "empty again after deleting every document"
