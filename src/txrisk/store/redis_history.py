"""The history the features read, served from a Redis cache of recent Transactions first.

ADR 0006: workers keep each Wallet's and each Agent's last 30 days of Transactions in Redis sorted
sets, scored by event time, and Postgres stays the source of truth. The cache must never change a
score, so `RedisHistory` answers exactly as `PostgresHistory` (and so `ReplayHistory`) does, or
not at all.

Three sets are kept per party, each holding the Transactions one history call reads:

- `subject:<wallet>`: the Wallet's Transactions as Subject wallet (`subject_history`);
- `receiver:<wallet>`: Transfers into the Wallet (`incoming_transfers`);
- `agent:<agent>`: Cash-ins and Cash-outs at the Agent (`agent_transactions`).

Wallets' registration and SIM swaps are not cached: they are small, read by key, and read from
Postgres. So is the market's spend per hour (`market_hours`), which a trigger keeps and which is
the same for every Transaction of an hour.

**What a miss is.** Each set has a *watermark* beside it, the oldest event time the set is known
to hold every Transaction from: all Transactions of that party at or after the watermark are in
the set, and nothing is known about earlier ones. A set with no watermark has never been filled
from Postgres, which is also a miss. A watermark is `BEGINNING` while nothing has ever been
trimmed from a fully loaded set, meaning the set holds the party's whole history. A read is
answered from Redis only if the watermark covers what it asks for, and otherwise falls back to
Postgres, never to partial history:

- `incoming_transfers` and `agent_transactions` read [`since`, `before`), so they are hits when
  the watermark is at or before `since`. The features ask for the last day, well inside 30.
- `subject_history` reads the Wallet's *whole* history before `before`: the features count it,
  look for the first use of a device and for any earlier Counterparty, however old. So it is a
  hit only while the set is complete (`BEGINNING`), that is, for a Wallet whose entire history
  lies within 30 days of its newest Transaction. An older Wallet reads Postgres, which is what
  keeps its features equal to the offline ones.

**Writing.** After a Transaction is durably stored, `RedisHistoryCache.record` adds it to its
sets in one atomic script per set, which also trims entries older than 30 days of event time
before the set's newest entry (never a wall-clock TTL) and raises the watermark to that cutoff
if it removed anything. The member is the Transaction's canonical encoding, so adding it again
(redelivery, or the replay of a stored Transaction) changes nothing, and the sorted set keeps
each set in event-time order whatever order Transactions arrive in. A set with no watermark is
first filled from Postgres: its last 30 days up to its newest Transaction, with `BEGINNING` if
nothing is older.

**Failure.** A Redis error on read falls back to Postgres. One on write first invalidates the
affected sets (a set that is gone is a miss, where one missing an entry under a watermark that
says it is complete would change scores) and raises if even that fails, so the Transaction is
redelivered or retried and `record` runs again.

**Writers must share the cache.** Anything that writes Transactions to Postgres without calling
`record` (a second API or worker without `TXRISK_REDIS_URL`, a restore, `txrisk seed` after the
cache was filled) leaves sets that are missing entries under a complete-looking watermark. Run
every writer against the same Redis, or flush it after such a write: an empty Redis is always
safe, it is only slower. Between a Transaction's commit and its `record`, other workers read
Redis without it, a moment's lag of the kind that already exists between partitions (see
`events/worker.py`).
"""

import json
import logging
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import redis
import sqlalchemy as sa

from txrisk.features import SubjectHistory, Transaction, Wallet
from txrisk.observability.tracing import span
from txrisk.store.history import PostgresHistory, select_transactions, transaction_from_row
from txrisk.store.tables import transactions

log = logging.getLogger(__name__)

WINDOW = timedelta(days=30)
# The watermark of a set holding its party's whole history: older than any event time.
BEGINNING = -(2**53)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_AGENT_TYPES = ("cash_in", "cash_out")

Kind = Literal["subject", "receiver", "agent"]
_t = transactions.c

# Adds Transactions to a set and trims it by event time, atomically. KEYS: the set, its
# watermark. ARGV: the window in microseconds; the watermark to set if there is none (empty to
# require one: returns 0 without writing when there is none); then (score, member) pairs.
_STORE = """
local window = tonumber(ARGV[1])
if ARGV[2] == '' then
  if redis.call('EXISTS', KEYS[2]) == 0 then return 0 end
else
  redis.call('SET', KEYS[2], ARGV[2], 'NX')
end
for i = 3, #ARGV, 2 do redis.call('ZADD', KEYS[1], ARGV[i], ARGV[i + 1]) end
local newest = redis.call('ZREVRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local cutoff = tonumber(newest[2]) - window
local removed = redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', '(' .. string.format('%d', cutoff))
if removed > 0 and tonumber(redis.call('GET', KEYS[2])) < cutoff then
  redis.call('SET', KEYS[2], string.format('%d', cutoff))
end
return 1
"""


def _micros(moment: datetime) -> int:
    return (moment - _EPOCH) // _MICROSECOND


def _encode(t: Transaction) -> bytes:
    return json.dumps(
        [
            t.transaction_id,
            t.occurred_at.isoformat(),
            t.transaction_type,
            t.amount,
            t.subject_wallet_id,
            t.direction,
            t.counterparty_id,
            t.region,
            t.subject_balance_before,
            t.device_id,
            t.channel,
        ],
        separators=(",", ":"),
    ).encode()


def _decode(member: bytes) -> Transaction:
    fields = json.loads(member)
    fields[1] = datetime.fromisoformat(fields[1])
    return Transaction(*fields)


def _in_order(found: Sequence[bytes]) -> list[Transaction]:
    # Redis orders equal scores by member bytes; the features' order is (time, transaction ID).
    return sorted(map(_decode, found), key=lambda t: (t.occurred_at, t.transaction_id))


def _keys_of(t: Transaction) -> list[tuple[Kind, str]]:
    keys: list[tuple[Kind, str]] = [("subject", t.subject_wallet_id)]
    if t.transaction_type == "transfer":
        keys.append(("receiver", t.counterparty_id))
    elif t.transaction_type in _AGENT_TYPES:
        keys.append(("agent", t.counterparty_id))
    return keys


def _belongs(kind: Kind, party: str) -> sa.ColumnElement[bool]:
    """The rows of `transactions` a set of this kind holds."""
    match kind:
        case "subject":
            return _t.subject_wallet_id == party
        case "receiver":
            return sa.and_(_t.transaction_type == "transfer", _t.counterparty_wallet_id == party)
        case "agent":
            return _t.agent_id == party


class RedisHistoryCache:
    """The Redis sets, the writer that keeps them and the history that reads them."""

    def __init__(
        self,
        client: redis.Redis,
        *,
        window: timedelta = WINDOW,
        prefix: str = "txrisk:history",
    ) -> None:
        self._redis = client
        self._window = window
        self._prefix = prefix
        self._store: Any = client.register_script(_STORE)
        # How reads were answered, for tests and operators: "hit" or "fallback".
        self.reads: Counter[str] = Counter()

    @classmethod
    def from_url(cls, url: str) -> "RedisHistoryCache":
        return cls(redis.Redis.from_url(url))

    def history(self, connection: sa.Connection) -> "RedisHistory":
        """A history over this cache, falling back to Postgres over `connection`."""
        return RedisHistory(self, PostgresHistory(connection))

    def record(self, connection: sa.Connection, transaction: Transaction) -> None:
        """Add a Transaction that is durably stored to its sets. Idempotent."""
        member = _encode(transaction)
        score = str(_micros(transaction.occurred_at))
        for kind, party in _keys_of(transaction):
            try:
                self._add(connection, kind, party, score, member)
            except redis.RedisError:
                log.warning("cache write failed; invalidating %s %s", kind, party, exc_info=True)
                self._redis.delete(*self._names(kind, party))

    def flush(self) -> None:
        """Forget every set. Always safe: every read then falls back to Postgres."""
        names = list(self._redis.scan_iter(f"{self._prefix}:*"))
        if names:
            self._redis.delete(*names)

    def read(
        self, kind: Kind, party: str, since: datetime | None, before: datetime
    ) -> list[Transaction] | None:
        """The set's Transactions in [`since`, `before`), or None if it cannot answer that:
        `since` None asks for everything before `before`, which only a complete set holds."""
        key, watermark_key = self._names(kind, party)
        low = "-inf" if since is None else str(_micros(since))
        pipe = self._redis.pipeline(transaction=True)
        pipe.get(watermark_key)
        pipe.zrangebyscore(key, low, f"({_micros(before)}")
        watermark, found = pipe.execute()
        if watermark is not None:
            covered_from = int(watermark)
            if covered_from == BEGINNING or (since is not None and covered_from <= _micros(since)):
                return _in_order(found)
        return None

    def _names(self, kind: Kind, party: str) -> tuple[str, str]:
        key = f"{self._prefix}:{kind}:{party}"
        return key, f"{key}:from"

    def _add(
        self, connection: sa.Connection, kind: Kind, party: str, score: str, member: bytes
    ) -> None:
        window = str(self._window // _MICROSECOND)
        if self._store(keys=self._names(kind, party), args=[window, "", score, member]):
            return
        self._fill(connection, kind, party, window)

    def _fill(self, connection: sa.Connection, kind: Kind, party: str, window: str) -> None:
        """Load a set from Postgres: its last window up to its newest Transaction."""
        belongs = _belongs(kind, party)
        newest = connection.execute(sa.select(sa.func.max(_t.occurred_at)).where(belongs)).scalar()
        if newest is None:
            return
        cutoff = newest - self._window
        rows = connection.execute(
            select_transactions().where(belongs, _t.occurred_at >= cutoff)
        ).all()
        older = connection.execute(
            sa.select(sa.exists().where(belongs, _t.occurred_at < cutoff))
        ).scalar_one()
        args: list[str | bytes] = [window, str(_micros(cutoff) if older else BEGINNING)]
        for row in rows:
            t = transaction_from_row(row)
            args += [str(_micros(t.occurred_at)), _encode(t)]
        self._store(keys=self._names(kind, party), args=args)


class RedisHistory:
    """History from Redis where its sets cover the question, else from Postgres.

    Use one per assessment, like `PostgresHistory`, whose reads it remembers in the same way.
    """

    def __init__(self, cache: RedisHistoryCache, postgres: PostgresHistory) -> None:
        self._cache = cache
        self._postgres = postgres
        self._subject: dict[tuple[str, datetime], SubjectHistory] = {}

    def wallet(self, wallet_id: str) -> Wallet:
        return self._postgres.wallet(wallet_id)

    def subject_history(self, wallet_id: str, before: datetime) -> SubjectHistory:
        key = (wallet_id, before)
        if key not in self._subject:
            found = self._cached("subject", wallet_id, None, before)
            if found is None:
                self._subject[key] = self._postgres.subject_history(wallet_id, before)
            else:
                self._subject[key] = SubjectHistory(
                    registered_at=self.wallet(wallet_id).registered_at, transactions=found
                )
        return self._subject[key]

    def incoming_transfers(
        self, wallet_id: str, since: datetime, before: datetime
    ) -> Sequence[Transaction]:
        found = self._cached("receiver", wallet_id, since, before)
        if found is None:
            return self._postgres.incoming_transfers(wallet_id, since, before)
        return found

    def agent_transactions(
        self, agent_id: str, since: datetime, before: datetime
    ) -> Sequence[Transaction]:
        found = self._cached("agent", agent_id, since, before)
        if found is None:
            return self._postgres.agent_transactions(agent_id, since, before)
        return found

    def last_sim_swap(self, wallet_id: str, before: datetime) -> datetime | None:
        return self._postgres.last_sim_swap(wallet_id, before)

    def market_hourly_spend(self, since: datetime, before: datetime) -> list[float]:
        return self._postgres.market_hourly_spend(since, before)

    def _cached(
        self, kind: Kind, party: str, since: datetime | None, before: datetime
    ) -> list[Transaction] | None:
        with span("history.redis", party_kind=kind) as read:
            try:
                found = self._cache.read(kind, party, since, before)
            except redis.RedisError:
                log.warning("cache read failed; reading Postgres", exc_info=True)
                found = None
            read.set_attribute("cache.hit", found is not None)
        self._cache.reads["fallback" if found is None else "hit"] += 1
        return found
