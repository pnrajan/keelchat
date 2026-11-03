"""The audit archive: records move from the hot store (Postgres) to Iceberg; reads span both; retention deletes files."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yachat.state.iceberg_store import FIELDS, IcebergAuditArchive, sql_catalog
from yachat.state.models import AuditEvent
from yachat.state.store import ArchiveConflict, ArchivedAuditStore, InMemoryAuditArchive, InMemoryAuditStore


def ev(tenant: str, hours_ago: float, **kw: object) -> AuditEvent:
    return AuditEvent(tenant=tenant, agent="a", kind="turn", decision="ask", at=datetime.now(UTC) - timedelta(hours=hours_ago), **kw)  # type: ignore[arg-type]


def iceberg(tmp: Path) -> IcebergAuditArchive:
    return IcebergAuditArchive(sql_catalog(f"sqlite:///{tmp}/catalog.db", f"file://{tmp}/warehouse"))


def test_the_archive_table_has_every_audit_field() -> None:
    assert [name for name, *_ in FIELDS] == list(AuditEvent.model_fields)


@pytest.mark.parametrize("make", ["memory", "iceberg"])
async def test_export_moves_settled_records_and_reads_span_both(make: str, tmp_path: Path) -> None:
    hot = InMemoryAuditStore()
    archive = InMemoryAuditArchive() if make == "memory" else iceberg(tmp_path)
    store = ArchivedAuditStore(hot, archive, hot_days=1)
    old = [ev("t1", 50, ms=12.5, llm_calls=2), ev("t1", 30, ok=True), ev("t2", 30)]
    fresh = ev("t1", 0)  # inside the lag: a turn still running may write records stamped before it
    await store.append([*old, fresh])

    assert await store.export(first=datetime.now(UTC) - timedelta(days=90)) == 3
    assert await store.export(first=datetime.now(UTC) - timedelta(days=90)) == 0  # incremental: nothing new
    cursor = await archive.cursor()
    assert cursor is not None and old[0].at < cursor <= datetime.now(UTC)

    got = await store.query("t1", limit=10)
    assert [e.id for e in got] == [fresh.id, old[1].id, old[0].id]  # newest first, no duplicates
    assert got[2].ms == 12.5 and got[2].llm_calls == 2 and got[1].ok is True
    assert [e.id for e in await store.query("t1", limit=2)] == [fresh.id, old[1].id]
    assert [e.id for e in await store.query("t2")] == [old[2].id]

    # the hot store drops archived records older than hot_days, never unarchived ones
    await store.purge(datetime.now(UTC) - timedelta(days=90))
    assert [e.id for e in await hot.query("t1")] == [fresh.id]
    assert len(await store.query("t1")) == 3


async def test_a_second_exporter_cannot_archive_the_same_records_twice(tmp_path: Path) -> None:
    a, b = iceberg(tmp_path), iceberg(tmp_path)
    end = datetime.now(UTC)
    await a.write([ev("t1", 2)], start=None, end=end)
    with pytest.raises(ArchiveConflict):
        await b.write([ev("t1", 2)], start=None, end=end)  # b still thinks nothing was archived
    assert len(await b.query("t1")) == 1


async def test_retention_deletes_archived_records_and_their_files(tmp_path: Path) -> None:
    archive = iceberg(tmp_path)
    keep, gone = ev("t1", 24), ev("t1", 24 * 100, text="old snippet")
    await archive.write([keep, gone], start=None, end=datetime.now(UTC))
    old_day = gone.at.date().isoformat()  # data files are partitioned by tenant and day

    def old_files() -> list[Path]:
        return [p for p in (tmp_path / "warehouse").rglob("*.parquet") if old_day in str(p)]

    assert old_files()
    await archive.purge(datetime.now(UTC) - timedelta(days=90))

    assert [e.id for e in await archive.query("t1")] == [keep.id]
    assert old_files() == []  # gone from storage, not only from the table's current snapshot


async def test_the_server_archives_when_a_warehouse_is_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yachat.api.app import stores_from_env

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("AUDIT_ARCHIVE_WAREHOUSE", f"file://{tmp_path}/warehouse")
    monkeypatch.setenv("AUDIT_ARCHIVE_CATALOG_URI", f"sqlite:///{tmp_path}/catalog.db")
    monkeypatch.setenv("AUDIT_HOT_DAYS", "3")
    store = stores_from_env()["audit"]
    assert isinstance(store, ArchivedAuditStore) and isinstance(store.archive, IcebergAuditArchive) and store.hot_days == 3
