"""Artifact store port: the one place files are kept (uploaded content, knowledge documents, slide decks).

Callers name a file by a key (`uploads/acme/bot/01-faq.md`) and never touch S3 or the disk themselves, so the backend
switches by configuration: S3 in production, a local folder in development, memory in tests. A file is never
overwritten by accident: `put` refuses an existing key unless the caller says it replaces it (a re-indexed document).

`put` returns a reference other components can read: `s3://bucket/key` on S3, the absolute path on disk."""

from __future__ import annotations

import asyncio
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

#: keys are relative, slash-separated and made of safe characters; no "..", no leading slash
_KEY = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.?(?:/|$))[A-Za-z0-9._@=+-]+(?:/[A-Za-z0-9._@=+-]+)*$")


class ArtifactExists(Exception):
    """The key already holds a file and the caller did not ask to replace it."""


def check_key(key: str) -> str:
    if len(key) > 900 or not _KEY.match(key):
        raise ValueError(f"invalid artifact key: {key!r}")
    return key


class ArtifactStore(ABC):
    @abstractmethod
    async def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream", replace: bool = False) -> str:
        """Store bytes under a key and return a reference to them. Raises ArtifactExists when the key is taken and
        `replace` is False."""

    @abstractmethod
    async def get(self, key: str) -> bytes | None:
        """The bytes under a key, or None when there are none."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Remove the file under a key; removing one that isn't there is not an error."""

    @abstractmethod
    async def keys(self, prefix: str, *, recursive: bool = True) -> list[str]:
        """Keys starting with `prefix`, sorted. With `recursive=False`, only those directly under it (no further `/`)."""


def _under(keys: list[str], prefix: str, recursive: bool) -> list[str]:
    return sorted(k for k in keys if k.startswith(prefix) and (recursive or "/" not in k[len(prefix) :]))


class InMemoryArtifactStore(ArtifactStore):
    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream", replace: bool = False) -> str:
        if check_key(key) in self.files and not replace:
            raise ArtifactExists(key)
        self.files[key] = (data, content_type)
        return f"memory://{key}"

    async def get(self, key: str) -> bytes | None:
        found = self.files.get(check_key(key))
        return found[0] if found else None

    async def delete(self, key: str) -> None:
        self.files.pop(check_key(key), None)

    async def keys(self, prefix: str, *, recursive: bool = True) -> list[str]:
        return _under(list(self.files), prefix, recursive)


class LocalArtifactStore(ArtifactStore):
    """Files under a folder; for development and single-machine installs."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / check_key(key)).resolve()
        if self.root not in path.parents:
            raise ValueError(f"invalid artifact key: {key!r}")
        return path

    async def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream", replace: bool = False) -> str:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "wb" if replace else "xb") as f:  # "x" fails if the file exists
                f.write(data)

        try:
            await asyncio.to_thread(_write)
        except FileExistsError:
            raise ArtifactExists(key) from None
        return str(path)

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            return None

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, missing_ok=True)

    async def keys(self, prefix: str, *, recursive: bool = True) -> list[str]:
        def _walk() -> list[str]:
            if not self.root.is_dir():
                return []
            return [p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()]

        return _under(await asyncio.to_thread(_walk), prefix, recursive)


class S3ArtifactStore(ArtifactStore):
    """One object per key in a bucket. boto3 is synchronous; calls run in a thread so the event loop never blocks.
    "Never overwrite" uses S3's conditional write (If-None-Match), so two servers can't both win the same key."""

    def __init__(self, bucket: str, client: Any = None):
        self.bucket = bucket
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=os.getenv("AWS_REGION"))
        self.s3 = client

    async def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream", replace: bool = False) -> str:
        from botocore.exceptions import ClientError

        kw: dict[str, Any] = {"Bucket": self.bucket, "Key": check_key(key), "Body": data, "ContentType": content_type}
        if not replace:
            kw["IfNoneMatch"] = "*"
        try:
            await asyncio.to_thread(lambda: self.s3.put_object(**kw))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise ArtifactExists(key) from None
            raise
        return f"s3://{self.bucket}/{key}"

    async def get(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        def _get() -> bytes | None:
            try:
                body: bytes = self.s3.get_object(Bucket=self.bucket, Key=check_key(key))["Body"].read()
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                    return None
                raise
            return body

        return await asyncio.to_thread(_get)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(lambda: self.s3.delete_object(Bucket=self.bucket, Key=check_key(key)))

    async def keys(self, prefix: str, *, recursive: bool = True) -> list[str]:
        def _list() -> list[str]:
            kw: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if not recursive:
                kw["Delimiter"] = "/"
            out: list[str] = []
            for page in self.s3.get_paginator("list_objects_v2").paginate(**kw):
                out += [item["Key"] for item in page.get("Contents", [])]
            return sorted(out)

        return await asyncio.to_thread(_list)


def artifact_store_from_env(bucket_var: str = "YACHAT_UPLOADS_BUCKET") -> ArtifactStore | None:
    """S3 when the bucket variable is set, else a folder under YACHAT_DATA_ROOT; None when neither is set."""
    if bucket := os.getenv(bucket_var):
        return S3ArtifactStore(bucket)
    if root := os.getenv("YACHAT_DATA_ROOT"):
        return LocalArtifactStore(Path(root))
    return None
