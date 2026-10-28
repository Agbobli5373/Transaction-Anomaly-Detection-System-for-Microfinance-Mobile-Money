"""Scoped API keys for calling services: issued, rotated and revoked, and stored only as digests.

A key reads `txrisk_<key_id>_<random part>`. The key ID is public and finds the stored record; the
random part is 256 bits from `secrets`, so a plain SHA-256 digest of the whole key is enough to
check it (a slow password hash guards guessable secrets, which these are not).

Expiry and revocation are measured against the wall clock the caller passes in: keys expire in
real time, unlike the event time that drives scoring (ADR 0005).
"""

import hashlib
import hmac
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa

from txrisk.store.tables import api_keys

PREFIX = "txrisk"


class UnknownApiKey(Exception):
    """No API key is stored under this key ID."""


class InactiveApiKey(Exception):
    """The API key is revoked or expired, so it cannot be rotated."""


class AlreadyRotated(Exception):
    """The API key has already been rotated; rotate its replacement instead."""


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    name: str
    scopes: frozenset[str]
    rate_limit_per_minute: int
    created_at: datetime
    created_by: str
    expires_at: datetime | None
    revoked_at: datetime | None
    # The key this one was rotated from.
    replaces: str | None

    def active(self, now: datetime) -> bool:
        """Whether the key is accepted at `now`: not revoked, and not yet expired."""
        if self.revoked_at is not None and self.revoked_at <= now:
            return False
        return self.expires_at is None or now < self.expires_at


@dataclass(frozen=True)
class IssuedApiKey:
    record: ApiKeyRecord
    # The whole key, to hand to the service. It is never stored and cannot be shown again.
    key: str


def _digest(presented: str) -> bytes:
    return hashlib.sha256(presented.encode()).digest()


def _record(row: sa.Row[Any]) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_id=row.key_id,
        name=row.name,
        scopes=frozenset(row.scopes),
        rate_limit_per_minute=row.rate_limit_per_minute,
        created_at=row.created_at,
        created_by=row.created_by,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        replaces=row.replaces,
    )


def issue(
    connection: sa.Connection,
    *,
    name: str,
    scopes: Iterable[str],
    rate_limit_per_minute: int,
    created_by: str,
    now: datetime,
    expires_at: datetime | None = None,
    replaces: str | None = None,
) -> IssuedApiKey:
    """Store a new key's digest and return the key, the only time it is ever available."""
    key_id = secrets.token_hex(8)
    key = f"{PREFIX}_{key_id}_{secrets.token_urlsafe(32)}"
    row = connection.execute(
        sa.insert(api_keys)
        .values(
            key_id=key_id,
            name=name,
            key_digest=_digest(key),
            scopes=sorted(set(scopes)),
            rate_limit_per_minute=rate_limit_per_minute,
            created_at=now,
            created_by=created_by,
            expires_at=expires_at,
            replaces=replaces,
        )
        .returning(*api_keys.c)
    ).one()
    return IssuedApiKey(_record(row), key)


def get(connection: sa.Connection, key_id: str) -> ApiKeyRecord | None:
    row = connection.execute(sa.select(api_keys).where(api_keys.c.key_id == key_id)).one_or_none()
    return None if row is None else _record(row)


def list_keys(connection: sa.Connection) -> list[ApiKeyRecord]:
    """Every key ever issued, revoked and expired ones included, oldest first."""
    rows = connection.execute(
        sa.select(api_keys).order_by(api_keys.c.created_at, api_keys.c.key_id)
    )
    return [_record(row) for row in rows]


def authenticate(connection: sa.Connection, presented: str, now: datetime) -> ApiKeyRecord | None:
    """The key's record if `presented` is a key that is active at `now`, otherwise None."""
    prefix, _, rest = presented.partition("_")
    key_id, _, random_part = rest.partition("_")
    if prefix != PREFIX or not key_id or not random_part:
        return None
    row = connection.execute(sa.select(api_keys).where(api_keys.c.key_id == key_id)).one_or_none()
    if row is None or not hmac.compare_digest(row.key_digest, _digest(presented)):
        return None
    record = _record(row)
    return record if record.active(now) else None


def rotate(
    connection: sa.Connection,
    key_id: str,
    *,
    overlap: timedelta,
    created_by: str,
    now: datetime,
) -> IssuedApiKey:
    """Issue a replacement with the same name, scopes, rate limit and expiry, and let the old
    key run on for `overlap` so the service can switch over without downtime. Rotation replaces
    the key, not its lifetime: a key due to expire hands its expiry on, and an old key that would
    expire sooner than the overlap keeps its earlier expiry. A key has at most one replacement,
    so there is only ever one live successor.

    Raises `UnknownApiKey`, `InactiveApiKey` if the old key is already revoked or expired, or
    `AlreadyRotated` if it already has a replacement.
    """
    # Locking the old key serialises concurrent rotations of it; the unique index on `replaces`
    # backs that up.
    old_row = connection.execute(
        sa.select(api_keys).where(api_keys.c.key_id == key_id).with_for_update()
    ).one_or_none()
    if old_row is None:
        raise UnknownApiKey(key_id)
    old = _record(old_row)
    if not old.active(now):
        raise InactiveApiKey(key_id)
    successor = connection.execute(
        sa.select(api_keys.c.key_id).where(api_keys.c.replaces == key_id)
    ).first()
    if successor is not None:
        raise AlreadyRotated(key_id)
    ends = now + overlap
    if old.expires_at is None or ends < old.expires_at:
        connection.execute(
            sa.update(api_keys).where(api_keys.c.key_id == key_id).values(expires_at=ends)
        )
    return issue(
        connection,
        name=old.name,
        scopes=old.scopes,
        rate_limit_per_minute=old.rate_limit_per_minute,
        created_by=created_by,
        now=now,
        expires_at=old.expires_at,
        replaces=key_id,
    )


def revoke(connection: sa.Connection, key_id: str, *, now: datetime) -> ApiKeyRecord:
    """Stop accepting the key at once. Revoking a revoked key keeps its first revocation time.

    Raises `UnknownApiKey`.
    """
    connection.execute(
        sa.update(api_keys)
        .where(api_keys.c.key_id == key_id, api_keys.c.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    record = get(connection, key_id)
    if record is None:
        raise UnknownApiKey(key_id)
    return record
