"""Where dataset sources may live: under YACHAT_DATA_ROOT on local disk, or in an allowed S3 bucket.
Checked when a dataset is registered, so a tenant cannot point a dataset at the service's own files."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from yachat.datasets.models import DatasetSpec, StructuredDataset


class SourceRejected(ValueError):
    pass


def data_root() -> Path | None:
    root = os.getenv("YACHAT_DATA_ROOT")
    return Path(root).resolve() if root else None


def allowed_buckets() -> set[str]:
    return {b.strip() for b in os.getenv("YACHAT_DATA_BUCKETS", "").split(",") if b.strip()}


def check_uri(uri: str, *, folders: bool = False) -> None:
    """`folders`: a local directory (or an S3 prefix ending in /) is allowed: every file in it is one table."""
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        if parsed.netloc not in allowed_buckets():
            raise SourceRejected(f"bucket {parsed.netloc!r} is not in YACHAT_DATA_BUCKETS")
        return
    if parsed.scheme not in ("", "file"):
        raise SourceRejected(f"unsupported scheme {parsed.scheme!r}; use a local path or s3://")
    root = data_root()
    if root is None:
        raise SourceRejected("local paths need YACHAT_DATA_ROOT")
    path = Path(parsed.path if parsed.scheme == "file" else uri).resolve()  # resolves .. and symlinks
    if not path.is_relative_to(root):
        raise SourceRejected(f"{uri!r} is outside YACHAT_DATA_ROOT")
    if not (path.is_file() or (folders and path.is_dir())):
        raise SourceRejected(f"{uri!r} does not exist" + ("" if folders or not path.is_dir() else " as a file"))


def check_sources(spec: DatasetSpec) -> None:
    if isinstance(spec, StructuredDataset):
        for t in spec.tables:
            check_uri(t.uri, folders=True)
    else:
        for d in spec.documents:
            check_uri(d.uri)


MAX_DOCUMENT_BYTES = 5_000_000


def read_bytes(uri: str, limit: int = MAX_DOCUMENT_BYTES) -> bytes:
    """Read one source (blocking; call in a thread). Re-checked here: the reading service enforces its own roots."""
    check_uri(uri)
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        import boto3

        body = boto3.client("s3", region_name=os.getenv("AWS_REGION")).get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))["Body"]
        data: bytes = body.read(limit + 1)
    else:
        with open(Path(parsed.path if parsed.scheme == "file" else uri).resolve(), "rb") as f:
            data = f.read(limit + 1)
    if len(data) > limit:
        raise SourceRejected(f"{uri!r} is larger than {limit} bytes")
    return data


def max_table_bytes() -> int:
    return int(os.getenv("YACHAT_MAX_TABLE_BYTES") or 1_000_000_000)


def local_copy(uri: str, scratch: Path, suffix: str) -> Path:
    """A local file or folder DuckDB can read (blocking). Local sources are used in place; S3 objects (or every `suffix`
    object under an S3 prefix ending in /) are downloaded to `scratch`. Folders are capped in total size."""
    check_uri(uri, folders=True)
    parsed = urlparse(uri)
    if parsed.scheme != "s3":
        path = Path(parsed.path if parsed.scheme == "file" else uri).resolve()
    else:
        import boto3

        s3 = boto3.client("s3", region_name=os.getenv("AWS_REGION"))
        key = parsed.path.lstrip("/")
        if not key.endswith("/"):
            path = scratch / Path(key).name
            s3.download_file(parsed.netloc, key, str(path))
        else:
            path = scratch / "prefix"
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=parsed.netloc, Prefix=key):
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith(suffix):
                        dest = path / obj["Key"][len(key) :]
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        s3.download_file(parsed.netloc, obj["Key"], str(dest))
    files = [p for p in path.rglob(f"*{suffix}") if p.is_file()] if path.is_dir() else [path]
    if not files:
        raise SourceRejected(f"{uri!r} has no {suffix} files")
    if sum(f.stat().st_size for f in files) > max_table_bytes():
        raise SourceRejected(f"{uri!r} is larger than YACHAT_MAX_TABLE_BYTES")
    return path
