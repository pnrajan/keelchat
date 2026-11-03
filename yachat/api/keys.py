"""API keys: who a request acts as, and what it may do.

Issued keys look like `chk_<id>_<secret>`. Only a SHA-256 of the whole key is stored (keys are long and random, so a
fast hash is enough); the key itself is shown once, when it is issued. A key belongs to one tenant, has scopes, may
expire, and can be revoked at once. Lookups go by the id; the hash is compared in constant time.

Scopes:
- `build`: everything for its tenant, including issuing keys with the same or narrower scopes.
- `read`: GET requests only.
- `runtime`: talk to agents (open sessions, send turns, read a session's state): the key for a website backend.

JWT keys: with YACHAT_JWT_SECRET set (32+ characters), new keys are signed JWTs (HS256) whose claims the server
sets at creation: `sub` the tenant, `scope` the scopes, `jti` the key id, `exp` (always; a year by default), plus a fixed
issuer and audience. The claims are signed, not encrypted: they name the tenant and scopes, never a secret. A JWT is
accepted only if its signature, issuer, audience and expiry check out *and* it is the exact token issued (hash match on
the stored record), so revoking still works at once. Opaque `chk_` keys keep working either way.

`YACHAT_API_KEY` is the admin key (every tenant, every scope). `YACHAT_TENANT_KEYS="acme:key,..."`
gives fixed `build` keys for development; issued keys are for everything else."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

Scope = Literal["build", "read", "runtime"]
SCOPES: tuple[Scope, ...] = ("build", "read", "runtime")
#: what each scope may do besides itself (build covers the others)
COVERS: dict[str, set[str]] = {"build": {"build", "read", "runtime"}, "read": {"read"}, "runtime": {"runtime"}}
KEY = re.compile(r"^chk_([a-z0-9]{12})_([A-Za-z0-9_-]{32,})$")
#: JWT keys: who issued them and for what (both checked on every request), and their lifetime when none is given
KEY_ISSUER, KEY_AUDIENCE = "yachat", "yachat-api"
JWT_DEFAULT_DAYS = 365
#: a key's last use is recorded at most this often (one write per key per minute, not per request)
TOUCH_EVERY = timedelta(minutes=1)


class ApiKey(BaseModel):
    id: str
    tenant: str
    name: str = Field(min_length=1, max_length=80)
    scopes: list[Scope]
    hash: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    def view(self) -> dict[str, object]:
        """What the API shows: never the hash."""
        return self.model_dump(mode="json", exclude={"hash"})


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class KeyConfigError(Exception):
    """YACHAT_JWT_SECRET is set but too short to sign keys safely."""


def jwt_secret() -> str | None:
    secret = os.getenv("YACHAT_JWT_SECRET")
    if secret and len(secret) < 32:
        raise KeyConfigError("YACHAT_JWT_SECRET must be at least 32 characters")
    return secret or None


def new_key(tenant: str, name: str, scopes: list[Scope], expires_in_days: int | None = None) -> tuple[str, ApiKey]:
    """A fresh key and its record: a signed JWT when YACHAT_JWT_SECRET is set, else an opaque `chk_` key. The key
    is returned once; only its hash is kept."""
    kid, now, scopes = secrets.token_hex(6), datetime.now(UTC), sorted(set(scopes))
    expires = now + timedelta(days=expires_in_days) if expires_in_days else None
    if secret := jwt_secret():
        import jwt

        expires = expires or now + timedelta(days=JWT_DEFAULT_DAYS)
        claims = {
            "iss": KEY_ISSUER,
            "aud": KEY_AUDIENCE,
            "sub": tenant,
            "scope": " ".join(scopes),
            "jti": kid,
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        }
        raw = jwt.encode(claims, secret, algorithm="HS256")
    else:
        raw = f"chk_{kid}_{secrets.token_urlsafe(32)}"
    return raw, ApiKey(id=kid, tenant=tenant, name=name, scopes=scopes, hash=digest(raw), expires_at=expires)


def parse_key(raw: str) -> tuple[str, str] | None:
    m = KEY.match(raw)
    return (m.group(1), raw) if m else None


def parse_jwt_key(raw: str) -> tuple[str, str, dict[str, object]] | None:
    """(key id, token, claims) of a JWT key signed by this server and still valid, else None."""
    secret = jwt_secret()
    if not secret or raw.count(".") != 2:
        return None
    import jwt

    try:
        claims = jwt.decode(
            raw, secret, algorithms=["HS256"], audience=KEY_AUDIENCE, issuer=KEY_ISSUER, options={"require": ["exp", "iat", "sub", "jti", "scope"]}
        )
    except jwt.PyJWTError:
        return None
    jti = claims.get("jti")
    return (jti, raw, claims) if isinstance(jti, str) and re.fullmatch(r"[a-z0-9]{12}", jti) else None


class ApiKeyStore(ABC):
    @abstractmethod
    async def add(self, key: ApiKey) -> None: ...
    @abstractmethod
    async def get(self, key_id: str) -> ApiKey | None: ...
    @abstractmethod
    async def list(self, tenant: str) -> list[ApiKey]: ...
    @abstractmethod
    async def revoke(self, tenant: str, key_id: str) -> bool: ...
    @abstractmethod
    async def touch(self, key_id: str) -> None:
        """Record that the key was used now."""


class InMemoryApiKeyStore(ApiKeyStore):
    def __init__(self) -> None:
        self._k: dict[str, ApiKey] = {}

    async def add(self, key: ApiKey) -> None:
        self._k[key.id] = key

    async def get(self, key_id: str) -> ApiKey | None:
        return self._k.get(key_id)

    async def list(self, tenant: str) -> list[ApiKey]:
        return sorted((k for k in self._k.values() if k.tenant == tenant), key=lambda k: k.created_at)

    async def revoke(self, tenant: str, key_id: str) -> bool:
        k = self._k.get(key_id)
        if k is None or k.tenant != tenant or k.revoked_at is not None:
            return False
        k.revoked_at = datetime.now(UTC)
        return True

    async def touch(self, key_id: str) -> None:
        if k := self._k.get(key_id):
            k.last_used_at = datetime.now(UTC)


ADMIN = "*"


@dataclass(frozen=True)
class Principal:
    """Who a request acts as. `tenant` is ADMIN for the admin key (or an open server)."""

    tenant: str
    scopes: frozenset[str] = field(default_factory=lambda: frozenset(SCOPES))
    key_id: str | None = None

    @property
    def admin(self) -> bool:
        return self.tenant == ADMIN

    def may(self, scope: str) -> bool:
        return self.admin or any(scope in COVERS[s] for s in self.scopes)


def _env_tenant_keys() -> dict[str, str]:
    out = {}
    for part in (os.getenv("YACHAT_TENANT_KEYS") or "").split(","):
        tenant, _, key = part.strip().partition(":")
        if tenant and key:
            out[key.strip()] = tenant.strip()
    return out


def bearer(authorization: str | None) -> str | None:
    """The token of a `Bearer <token>` header (scheme case-insensitive, one space), else None."""
    scheme, sep, token = (authorization or "").partition(" ")
    return token if sep and scheme.lower() == "bearer" and token and " " not in token else None


async def resolve(authorization: str | None, store: ApiKeyStore) -> Principal | None:
    """The caller, or None when the key is missing, unknown, expired or revoked. With no YACHAT_API_KEY and no
    YACHAT_TENANT_KEYS the server is open (development): every caller is admin, and keys cannot be issued."""
    admin, fixed = os.getenv("YACHAT_API_KEY"), _env_tenant_keys()
    if not admin and not fixed:
        return Principal(ADMIN)
    token = bearer(authorization)
    if token is None:
        return None
    if admin and hmac.compare_digest(token, admin):
        return Principal(ADMIN)
    for key, tenant in fixed.items():
        if hmac.compare_digest(token, key):
            return Principal(tenant, frozenset({"build"}))
    parsed = parse_key(token)
    claims: dict[str, object] | None = None
    if parsed is None and (signed := parse_jwt_key(token)) is not None:
        parsed, claims = signed[:2], signed[2]
    if parsed is None:
        return None
    rec = await store.get(parsed[0])
    now = datetime.now(UTC)
    if rec is None or not hmac.compare_digest(rec.hash, digest(token)) or rec.revoked_at or (rec.expires_at and rec.expires_at <= now):
        return None
    if claims is not None and (claims.get("sub") != rec.tenant or claims.get("scope") != " ".join(rec.scopes)):
        return None  # the signed claims must say what the record says
    if rec.last_used_at is None or now - rec.last_used_at >= TOUCH_EVERY:
        await store.touch(rec.id)
    return Principal(rec.tenant, frozenset(rec.scopes), rec.id)


#: requests a runtime key may make: talk to agents, nothing else
_RUNTIME = re.compile(r"^/v1/sessions(/[^/]+/(turns|state))?$")


def scope_for(method: str, path: str) -> str | None:
    """The scope a request needs; None: any valid key (who am I)."""
    if path == "/v1/whoami":
        return None
    if _RUNTIME.match(path):
        return "runtime"
    return "read" if method in ("GET", "HEAD") else "build"


def require_service_key(request: Request) -> None:
    """For the data services (Knowledge, Analytics), which only the API calls: YACHAT_API_KEY, when set, is
    required as the bearer token (health excepted). Tenant and issued keys are for the API, not for these."""
    expected = os.getenv("YACHAT_API_KEY")
    if request.url.path == "/health" or not expected:
        return
    token = bearer(request.headers.get("authorization"))
    if token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "missing or invalid service key")
