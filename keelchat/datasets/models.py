"""The DataSet concept. A dataset describes sources; it holds no data. Nodes name one in their config; the compiler
checks it exists with the right kind; the Tool Gateway resolves the name to the tenant's dataset at call time."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.fields import FieldInfo

DatasetKind = Literal["structured", "unstructured"]
NAME_PATTERN = r"^[a-z][a-z0-9_]{0,62}$"

_TABLE_FORMATS = {".csv": "csv", ".parquet": "parquet"}
_DOC_FORMATS = {".txt": "text", ".md": "markdown", ".markdown": "markdown"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _suffix(uri: str) -> str:
    return PurePosixPath(uri.split("?", 1)[0]).suffix.lower()


class TableSource(_Model):
    #: the SQL table name
    name: str = Field(pattern=NAME_PATTERN)
    #: a local path under KEELCHAT_DATA_ROOT or s3://<allowed bucket>/...
    uri: str = Field(min_length=1, max_length=2_000)
    format: Literal["csv", "parquet"] | None = None
    description: str = Field(default="", max_length=2_000)
    #: column notes that help the model write SQL, e.g. {"amount": "order total in USD"}
    columns: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _format(self) -> TableSource:
        if self.format is None:
            fmt = _TABLE_FORMATS.get(_suffix(self.uri))
            if fmt is None:
                raise ValueError(f"cannot infer the format of {self.uri!r}; set format: csv|parquet")
            object.__setattr__(self, "format", fmt)
        return self


class DocumentSource(_Model):
    uri: str = Field(min_length=1, max_length=2_000)
    title: str = Field(default="", max_length=500)
    format: Literal["text", "markdown"] | None = None

    @model_validator(mode="after")
    def _format(self) -> DocumentSource:
        if self.format is None:
            object.__setattr__(self, "format", _DOC_FORMATS.get(_suffix(self.uri), "text"))
        return self


class SqlExample(_Model):
    """A known-good question and its SQL: the Analyst uses these as examples when writing new queries."""

    question: str = Field(min_length=1, max_length=2_000)
    sql: str = Field(min_length=1, max_length=20_000)


class StructuredDataset(_Model):
    kind: Literal["structured"] = "structured"
    tables: list[TableSource] = Field(min_length=1, max_length=50)
    examples: list[SqlExample] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _unique(self) -> StructuredDataset:
        names = [t.name for t in self.tables]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate table names: {sorted({n for n in names if names.count(n) > 1})}")
        return self


class UnstructuredDataset(_Model):
    kind: Literal["unstructured"] = "unstructured"
    documents: list[DocumentSource] = Field(min_length=1, max_length=1_000)


DatasetSpec = Annotated[StructuredDataset | UnstructuredDataset, Field(discriminator="kind")]


class DatasetIn(_Model):
    """Registration body: PUT /v1/tenants/{tenant}/datasets/{name}."""

    description: str = Field(default="", max_length=2_000)
    spec: DatasetSpec


class Dataset(_Model):
    tenant: str
    name: str = Field(pattern=NAME_PATTERN)
    description: str = ""
    spec: DatasetSpec
    #: content hash of description + spec; changes whenever the sources change
    version: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def kind(self) -> DatasetKind:
        return self.spec.kind

    @property
    def ref(self) -> str:
        """Globally unique id services key their state by."""
        return f"{self.tenant}/{self.name}"

    @classmethod
    def create(cls, tenant: str, name: str, body: DatasetIn) -> Dataset:
        canon = json.dumps({"d": body.description, "s": body.spec.model_dump(mode="json")}, sort_keys=True)
        return cls(tenant=tenant, name=name, description=body.description, spec=body.spec, version=hashlib.sha256(canon.encode()).hexdigest()[:12])


@dataclass(frozen=True)
class DatasetRef:
    """Marks a node-config field as naming a dataset of `kind`. The compiler checks the reference at build."""

    kind: DatasetKind


StructuredRef = Annotated[str, DatasetRef("structured"), Field(pattern=NAME_PATTERN)]
UnstructuredRef = Annotated[str, DatasetRef("unstructured"), Field(pattern=NAME_PATTERN)]


def _ref_of(annotation: object) -> DatasetRef | None:
    """The DatasetRef on a field type, also inside `X | None`."""
    found = next((m for m in getattr(annotation, "__metadata__", ()) if isinstance(m, DatasetRef)), None)
    return found or next((r for a in get_args(annotation) if (r := _ref_of(a)) is not None), None)


def field_dataset_kind(info: FieldInfo) -> DatasetKind | None:
    """The kind of dataset a node-config field names, when it is a dataset reference."""
    ref = next((m for m in info.metadata if isinstance(m, DatasetRef)), None) or _ref_of(info.annotation)
    return ref.kind if ref is not None else None


def dataset_refs(model: type[BaseModel], settings: dict[str, object]) -> list[tuple[str, DatasetKind, str]]:
    """(field, kind, dataset name) for every DatasetRef field set in a node's settings."""
    out: list[tuple[str, DatasetKind, str]] = []
    for fname, info in model.model_fields.items():
        kind = field_dataset_kind(info)
        value = settings.get(fname)
        if kind is not None and isinstance(value, str):
            out.append((fname, kind, value))
    return out
