import asyncio
import math

from keelchat import Chunk, Hit, VectorIndex, VectorStoreConfig, vector_store
from keelchat.testing import check_vector_store


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


@vector_store("dictstore")  # pick it with KNOWLEDGE_VECTOR_STORE=dictstore
class DictStore(VectorIndex):
    def __init__(self, cfg: VectorStoreConfig):
        self.rows: dict[str, tuple[Chunk, list[float]]] = {}

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        for chunk, vector in zip(chunks, vectors, strict=True):
            self.rows[chunk.id] = (chunk, vector)  # same id: replace

    async def search(self, vector: list[float], k: int = 5, max_level: int | None = None, dataset: str = "") -> list[Hit]:
        hits = [Hit(chunk=c, score=cosine(vector, v)) for c, v in self.rows.values() if c.dataset == dataset and (max_level is None or c.level <= max_level)]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    async def delete_doc(self, doc_id: str) -> None:
        for cid in [i for i, (c, _) in self.rows.items() if c.doc_id == doc_id]:
            del self.rows[cid]

    async def count(self, dataset: str | None = None) -> int:
        return sum(1 for c, _ in self.rows.values() if dataset is None or c.dataset == dataset)


async def test_dict_store() -> None:
    cfg = VectorStoreConfig(uri=None, table="chunks_test", dim=0)
    await check_vector_store(DictStore(cfg))


if __name__ == "__main__":
    asyncio.run(test_dict_store())
    print("check_vector_store passed")
