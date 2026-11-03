"""One in-memory DuckDB database per dataset version. Tables are loaded, then the database is locked: no file,
network or extension access and no setting changes, so a query can only read the loaded tables."""

from __future__ import annotations

import asyncio
import datetime as dt
import decimal
import math
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from yachat.analytics.models import Column, Scalar
from yachat.datasets.models import Dataset, StructuredDataset
from yachat.datasets.sources import local_copy


class AnalyticsError(Exception):
    """`kind` is safe to return to callers; the message is for logs only."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(f"{kind}: {message}")


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: list[tuple[str, str]]
    rows: int
    description: str = ""
    notes: dict[str, str] = field(default_factory=dict)

    def ddl(self) -> str:
        cols = ",\n".join(f"  {_ident(c)} {t}" + (f"  -- {self.notes[c]}" if c in self.notes else "") for c, t in self.columns)
        head = f"-- {self.description}\n" if self.description else ""
        return f"{head}CREATE TABLE {_ident(self.name)} (\n{cols}\n);  -- {self.rows} rows"


@dataclass
class QueryResult:
    columns: list[Column]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    #: the result rows statistics were computed over (up to stats_cap)
    frame: pd.DataFrame
    stats_capped: bool


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def check_select(sql: str) -> str:
    """Exactly one SELECT statement (WITH ... SELECT included). Returns it without a trailing semicolon."""
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error as e:
        raise AnalyticsError("sql_invalid", str(e)) from e
    if len(statements) != 1:
        raise AnalyticsError("sql_invalid", f"expected one statement, got {len(statements)}")
    if statements[0].type != duckdb.StatementType.SELECT:
        raise AnalyticsError("sql_not_select", f"only SELECT is allowed, got {statements[0].type.name}")
    return sql.strip().rstrip(";").strip()


def plain(v: Any) -> Any:
    """A JSON-safe Python value for a DuckDB / pandas cell."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if hasattr(v, "item") and not isinstance(v, str | bytes):  # numpy scalar
        v = v.item()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, dt.datetime | dt.date | dt.time | pd.Timestamp):
        return v.isoformat()
    if isinstance(v, dt.timedelta | pd.Timedelta):
        return str(v)
    if isinstance(v, float) and math.isinf(v):
        return str(v)
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, int | float | str | bool):
        return v
    return str(v)


def summarize(frame: pd.DataFrame) -> dict[str, dict[str, Scalar]]:
    """Per column: count and nulls; numeric columns also sum, mean, std, min, max; others distinct count and top."""
    out: dict[str, dict[str, Scalar]] = {}
    for col in frame.columns:
        s = frame[col]
        stats: dict[str, Scalar] = {"count": int(s.notna().sum()), "nulls": int(s.isna().sum())}
        numeric = pd.to_numeric(s, errors="coerce") if s.dtype == object else s
        if pd.api.types.is_numeric_dtype(numeric) and not pd.api.types.is_bool_dtype(numeric) and numeric.notna().any():
            n = numeric.dropna().astype(float)
            stats |= {"sum": n.sum(), "mean": n.mean(), "min": n.min(), "max": n.max(), **({"std": n.std()} if len(n) > 1 else {})}
            stats = {k: (round(float(v), 6) if isinstance(v, float) else v) for k, v in stats.items()}
        else:
            stats |= {"distinct": int(s.nunique())}
            if s.notna().any():
                stats["top"] = plain(s.mode().iloc[0])
        out[str(col)] = stats
    return out


class DatasetDB:
    """Loads a structured dataset's tables into an in-memory DuckDB, then locks it."""

    def __init__(self, dataset: Dataset, *, memory_limit: str | None = None, threads: int | None = None):
        if not isinstance(dataset.spec, StructuredDataset):
            raise AnalyticsError("dataset_kind", f"{dataset.ref} is {dataset.kind}; analytics needs a structured dataset")
        self.dataset: Dataset | None = dataset
        self._open(memory_limit, threads)
        with tempfile.TemporaryDirectory(prefix="analytics-") as scratch:
            for t in dataset.spec.tables:
                suffix = ".parquet" if t.format == "parquet" else ".csv"
                path = local_copy(t.uri, Path(scratch), suffix)
                reader = "read_parquet" if t.format == "parquet" else "read_csv_auto"
                source = f"{path}/**/*{suffix}" if path.is_dir() else str(path)  # a folder: every file is one table
                try:
                    self.con.execute(f"CREATE TABLE {_ident(t.name)} AS SELECT * FROM {reader}({_literal(source)})")
                except duckdb.Error as e:
                    raise AnalyticsError("source_unreadable", f"{t.name}: {e}") from e
                self._describe(t.name, t.description, dict(t.columns))
        self._seal()

    @classmethod
    def from_frames(cls, frames: list[tuple[str, pd.DataFrame, str, dict[str, str]]], *, memory_limit: str | None = None) -> DatasetDB:
        """Tables already in memory: (name, rows, description, column notes) each; locked the same way."""
        db = cls.__new__(cls)
        db.dataset = None
        db._open(memory_limit, None)
        for name, frame, description, notes in frames:
            db.con.register("_frame", frame)
            db.con.execute(f"CREATE TABLE {_ident(name)} AS SELECT * FROM _frame")
            db.con.unregister("_frame")
            db._describe(name, description, notes)
        db._seal()
        return db

    def _open(self, memory_limit: str | None, threads: int | None) -> None:
        self.con = duckdb.connect(":memory:")
        self.con.execute(f"SET memory_limit={_literal(memory_limit or os.getenv('ANALYTICS_MEMORY_LIMIT') or '2GB')}")
        self.con.execute(f"SET threads={int(threads or os.getenv('ANALYTICS_THREADS') or 2)}")
        self.tables: list[TableInfo] = []

    def _describe(self, name: str, description: str, notes: dict[str, str]) -> None:
        cols = [(str(r[0]), str(r[1])) for r in self.con.execute(f"DESCRIBE {_ident(name)}").fetchall()]
        [(n,)] = self.con.execute(f"SELECT count(*) FROM {_ident(name)}").fetchall()
        self.tables.append(TableInfo(name, cols, int(n), description, notes))

    def _seal(self) -> None:
        """No file, network or extension access and no setting changes from here on."""
        self.con.execute("SET enable_external_access=false")
        self.con.execute("SET autoinstall_known_extensions=false")
        self.con.execute("SET autoload_known_extensions=false")
        self.con.execute("SET lock_configuration=true")
        self._lock = threading.Lock()

    def ddl(self) -> list[str]:
        return [t.ddl() for t in self.tables]

    def tables_in(self, sql: str) -> list[str]:
        lowered = sql.lower()
        return [t.name for t in self.tables if t.name.lower() in lowered]

    def _run(self, sql: str, row_cap: int, stats_cap: int, cursor: duckdb.DuckDBPyConnection) -> QueryResult:
        try:  # the cursor is closed by the thread that used it, also after an interrupt
            rel = cursor.execute(f"SELECT * FROM ({sql}) AS q LIMIT {int(stats_cap) + 1}")
            cols = [Column(name=str(d[0]), type=str(d[1])) for d in rel.description or []]
            frame = rel.df()
        finally:
            cursor.close()
        stats_capped = len(frame) > stats_cap
        frame = frame.iloc[:stats_cap]
        rows = [[plain(v) for v in r] for r in frame.iloc[:row_cap].itertuples(index=False, name=None)]
        return QueryResult(cols, rows, len(frame), len(frame) > row_cap, frame, stats_capped)

    async def query(self, sql: str, *, row_cap: int = 200, stats_cap: int = 100_000, timeout_s: float = 20.0) -> QueryResult:
        sql = check_select(sql)
        with self._lock:
            cursor = self.con.cursor()
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._run, sql, row_cap, stats_cap, cursor), timeout=timeout_s)
        except TimeoutError:
            cursor.interrupt()
            raise AnalyticsError("sql_timeout", f"query exceeded {timeout_s}s") from None
        except duckdb.Error as e:
            raise AnalyticsError("sql_error", str(e)) from e

    def close(self) -> None:
        self.con.close()
