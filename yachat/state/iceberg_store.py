"""The audit archive on Apache Iceberg: Parquet files on S3 (or a local path), partitioned by tenant and day; the table
catalog in Postgres (or any SQL database). DuckDB, Athena, Spark, Trino and Snowflake read the same table.

Exports are exactly-once: the archived-up-to cursor is a table property committed with the records, and a commit
whose table changed underneath fails instead of retrying (two exporters cannot both archive the same hour)."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import CommitFailedException
from pyiceberg.expressions import And, BooleanExpression, EqualTo, GreaterThanOrEqual, In, LessThan
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.transforms import DayTransform, IdentityTransform
from pyiceberg.types import BooleanType, DoubleType, IcebergType, LongType, NestedField, StringType, TimestamptzType

from yachat.logging import get_logger
from yachat.state.models import AuditEvent, AuditKind, now
from yachat.state.store import ArchiveConflict, AuditArchive

log = get_logger("audit_archive")

#: one column per AuditEvent field, in order: (name, type, required)
FIELDS: list[tuple[str, IcebergType, bool]] = [
    ("id", StringType(), True),
    ("at", TimestamptzType(), True),
    ("tenant", StringType(), True),
    ("agent", StringType(), True),
    ("policy_hash", StringType(), True),
    ("session_id", StringType(), False),
    ("turn_id", StringType(), False),
    ("kind", StringType(), True),
    ("decision", StringType(), True),
    ("reason", StringType(), True),
    ("step", StringType(), False),
    ("field", StringType(), False),
    ("ok", BooleanType(), False),
    ("ref", StringType(), False),
    ("actor", StringType(), True),
    ("text", StringType(), True),
    ("ms", DoubleType(), False),
    ("llm_calls", LongType(), False),
    ("tool_calls", LongType(), False),
    ("errors", LongType(), False),
    ("input_tokens", LongType(), False),
    ("output_tokens", LongType(), False),
    ("cached_tokens", LongType(), False),
    ("cost_usd", DoubleType(), False),
    ("subject", StringType(), False),  # added later: optional, so existing tables gain the column
    ("reply", StringType(), False),
    ("saved_tokens", LongType(), False),  # added later: optional
]
SCHEMA = Schema(*(NestedField(i, name, t, required=req) for i, (name, t, req) in enumerate(FIELDS, start=1)))
SPEC = PartitionSpec(
    PartitionField(source_id=3, field_id=1000, transform=IdentityTransform(), name="tenant"),
    PartitionField(source_id=2, field_id=1001, transform=DayTransform(), name="at_day"),
)
CURSOR = "yachat.archived-until"
PROPERTIES = {
    "commit.retry.num-retries": "0",  # a retried append would archive the same records twice
    # no min / max statistics of free text in the table's metadata files, which deletes do not rewrite
    "write.metadata.metrics.column.text": "none",
    "write.metadata.metrics.column.reason": "none",
    "write.metadata.delete-after-commit.enabled": "true",
    "write.metadata.previous-versions-max": "10",
}


def _pred(op: Any, column: str, value: Any) -> BooleanExpression:
    """pyiceberg's predicates are pydantic models whose constructors mypy misreads; `op(column, value)` is their API."""
    expr: BooleanExpression = op(column, value)
    return expr


def _utc(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


class IcebergAuditArchive(AuditArchive):
    def __init__(self, catalog: Catalog, table: str = "yachat.audit_events") -> None:
        self.catalog, self.name = catalog, table
        self._ready = False

    def table(self) -> Table:
        """The table at its latest commit (created, or given new AuditEvent fields, the first time)."""
        if not self._ready:
            self.catalog.create_namespace_if_not_exists(self.name.rsplit(".", 1)[0])
            t = self.catalog.create_table_if_not_exists(self.name, schema=SCHEMA, partition_spec=SPEC, properties=PROPERTIES)
            if {f.name for f in t.schema().fields} != {name for name, *_ in FIELDS}:
                with t.update_schema() as update:
                    update.union_by_name(SCHEMA)
            self._ready = True
        return self.catalog.load_table(self.name)

    async def cursor(self) -> datetime | None:
        return await asyncio.to_thread(self._cursor)

    def _cursor(self) -> datetime | None:
        raw = self.table().properties.get(CURSOR)
        return datetime.fromisoformat(raw) if raw else None

    async def write(self, events: list[AuditEvent], *, start: datetime | None, end: datetime) -> None:
        await asyncio.to_thread(self._write, events, start, end)

    def _write(self, events: list[AuditEvent], start: datetime | None, end: datetime) -> None:
        t = self.table()
        raw = t.properties.get(CURSOR)
        if (datetime.fromisoformat(raw) if raw else None) != start:
            raise ArchiveConflict(f"archive cursor is {raw}, not {start}")
        try:
            with t.transaction() as tx:
                if events:
                    tx.append(pa.Table.from_pylist([e.model_dump() for e in events], schema=t.schema().as_arrow()))
                tx.set_properties({CURSOR: _utc(end)})
        except CommitFailedException as e:
            raise ArchiveConflict(str(e)) from e

    async def query(
        self,
        tenant: str,
        *,
        agent: str | None = None,
        session_id: str | None = None,
        kinds: list[AuditKind] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
    ) -> list[AuditEvent]:
        where = _pred(EqualTo, "tenant", tenant)
        for col, val in (("agent", agent), ("session_id", session_id)):
            if val is not None:
                where = And(where, _pred(EqualTo, col, val))
        if kinds:
            where = And(where, _pred(In, "kind", set(kinds)))
        if since is not None:
            where = And(where, _pred(GreaterThanOrEqual, "at", _utc(since)))
        if until is not None:
            where = And(where, _pred(LessThan, "at", _utc(until)))
        return await asyncio.to_thread(self._query, where, limit)

    def _query(self, where: BooleanExpression, limit: int) -> list[AuditEvent]:
        rows = self.table().scan(row_filter=where).to_arrow()
        rows = rows.sort_by([("at", "descending"), ("id", "descending")]).slice(0, limit)
        return [AuditEvent.model_validate({k: v for k, v in r.items() if v is not None}) for r in rows.to_pylist()]

    async def purge(self, before: datetime) -> None:
        await asyncio.to_thread(self._purge, before)

    def _purge(self, before: datetime) -> None:
        """Delete the rows, then the snapshots that still point at them, then the files no snapshot uses: retention
        removes the data from storage, not only from the table's current view."""
        t = self.table()
        t.delete(_pred(LessThan, "at", _utc(before)))
        t = self.table()
        files = set(t.inspect.all_files()["file_path"].to_pylist())
        t.maintenance.expire_snapshots().older_than(now()).commit()
        t = self.table()
        unused = files - set(t.inspect.all_files()["file_path"].to_pylist())
        for path in unused:
            t.io.delete(path)
        if unused:
            log.info("audit archive files deleted", files=len(unused))


def sql_catalog(uri: str, warehouse: str, **props: str) -> SqlCatalog:
    """An Iceberg catalog in a SQL database (SQLAlchemy URI); data files under `warehouse` (s3://… or file://…)."""
    return SqlCatalog("yachat", uri=uri, warehouse=warehouse, **props)


def archive_from_env() -> IcebergAuditArchive | None:
    """AUDIT_ARCHIVE_WAREHOUSE (s3://bucket/prefix) turns the archive on. The catalog is in AUDIT_ARCHIVE_CATALOG_URI
    (a SQLAlchemy URI), else in DATABASE_URL's Postgres. S3 credentials come from the usual AWS variables."""
    warehouse = os.getenv("AUDIT_ARCHIVE_WAREHOUSE")
    if not warehouse:
        return None
    uri = os.getenv("AUDIT_ARCHIVE_CATALOG_URI") or os.getenv("DATABASE_URL")
    if not uri:
        raise ValueError("AUDIT_ARCHIVE_WAREHOUSE is set but there is no catalog: set AUDIT_ARCHIVE_CATALOG_URI or DATABASE_URL")
    if uri.startswith(("postgres://", "postgresql://")):
        uri = "postgresql+psycopg2://" + uri.split("://", 1)[1]
    props: dict[str, Any] = {}
    if region := os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"):
        props["s3.region"] = region
    if endpoint := os.getenv("AWS_ENDPOINT_URL_S3") or os.getenv("AWS_ENDPOINT_URL"):
        props["s3.endpoint"] = endpoint
    return IcebergAuditArchive(sql_catalog(uri, warehouse, **props))
