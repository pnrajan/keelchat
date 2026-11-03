"""DatasetStore port and its in-memory fake. Postgres adapter: state/postgres_store.py."""

from __future__ import annotations

from abc import ABC, abstractmethod

from yachat.datasets.models import Dataset


class DatasetStore(ABC):
    @abstractmethod
    async def get(self, tenant: str, name: str) -> Dataset | None: ...

    @abstractmethod
    async def list(self, tenant: str) -> list[Dataset]: ...

    @abstractmethod
    async def put(self, dataset: Dataset) -> None: ...

    @abstractmethod
    async def delete(self, tenant: str, name: str) -> bool: ...


class InMemoryDatasetStore(DatasetStore):
    def __init__(self) -> None:
        self._d: dict[tuple[str, str], Dataset] = {}

    async def get(self, tenant: str, name: str) -> Dataset | None:
        return self._d.get((tenant, name))

    async def list(self, tenant: str) -> list[Dataset]:
        return sorted((d for (t, _), d in self._d.items() if t == tenant), key=lambda d: d.name)

    async def put(self, dataset: Dataset) -> None:
        self._d[(dataset.tenant, dataset.name)] = dataset

    async def delete(self, tenant: str, name: str) -> bool:
        return self._d.pop((tenant, name), None) is not None
