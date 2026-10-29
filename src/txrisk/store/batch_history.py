"""The history a batch of Transactions is scored from, read from Postgres in a few queries.

Scoring a Transaction on its own asks Postgres a handful of questions about its Subject wallet, its
receiving Wallet or Agent, its SIM swaps and the market's spend. Scoring hundreds at once would
ask hundreds of times that, one round trip each. `batch_history` asks once for everything the
whole batch will read, and answers from memory: it is the offline `ReplayHistory` filled with what
Postgres holds, so it answers exactly as `PostgresHistory` and the replay do (every window is by
event time and sees nothing at or after `before`).

The caller scores the batch in order and `record`s each Transaction after its features are
computed. A later Transaction of the batch then sees the earlier ones, as it would if they had
been stored one by one, and never itself or the ones after it. What Postgres held when the batch
was read is the committed history; the Transactions of other workers stored since are a moment's
lag, of the kind that already exists between partitions (see `events/worker.py`).
"""

from collections.abc import Sequence
from dataclasses import fields
from datetime import datetime
from typing import Protocol

import sqlalchemy as sa

from txrisk.features import FeatureConfig, ReplayHistory, Transaction, Wallet
from txrisk.features.market import spend_window
from txrisk.features.transaction import RECENT_WINDOW
from txrisk.store.history import _TRANSACTION_COLUMNS, transaction_from_row
from txrisk.store.records import any_of
from txrisk.store.redis_history import Kind
from txrisk.store.tables import market_hours, sim_swaps, transactions, wallets

_t = transactions.c
_AGENT_TYPES = ("cash_in", "cash_out")


_FIELDS = tuple(f.name for f in fields(Transaction))


def _transaction(row: Sequence[object]) -> Transaction:
    """`transaction_from_row`, for the tens of thousands of rows a batch reads: the same
    Transaction, built without the frozen dataclass's field-by-field initialiser."""
    transaction = object.__new__(Transaction)
    transaction.__dict__.update(zip(_FIELDS, row, strict=True))
    return transaction


class SetReader(Protocol):
    """The history cache, as a batch reads it: `RedisHistoryCache`."""

    def read_many(
        self, requests: Sequence[tuple[Kind, str, datetime | None, datetime]]
    ) -> list[list[Transaction] | None]: ...


def batch_history(
    connection: sa.Connection,
    batch: Sequence[Transaction],
    *,
    config: FeatureConfig,
    cache: SetReader | None = None,
) -> ReplayHistory:
    """A history holding everything stored that the features of `batch` read, before the latest
    Transaction of the batch. With a `cache`, what its sets can answer comes from it, in one
    round trip, and only the rest is read from Postgres (ADR 0006); either way a few statements
    read it all."""
    if not batch:
        return ReplayHistory({})
    latest = max(t.occurred_at for t in batch)
    earliest = min(t.occurred_at for t in batch)
    since = earliest - RECENT_WINDOW
    subjects = sorted({t.subject_wallet_id for t in batch})
    # Wallets whose incoming Transfers the features read: the receiver of a Transfer, and the
    # Subject wallet of a Cash-out (its own fan-in).
    receivers = sorted(
        {t.counterparty_id for t in batch if t.transaction_type == "transfer"}
        | {t.subject_wallet_id for t in batch if t.transaction_type == "cash_out"}
    )
    agents = sorted({t.counterparty_id for t in batch if t.transaction_type in _AGENT_TYPES})

    asked: list[tuple[Kind, str, datetime | None, datetime]] = [
        *(("subject", wallet, None, latest) for wallet in subjects),
        *(("receiver", wallet, since, latest) for wallet in receivers),
        *(("agent", agent, since, latest) for agent in agents),
    ]
    answers = iter(cache.read_many(asked) if cache is not None else [None] * len(asked))
    from_cache = {(kind, party): next(answers) for kind, party, _, _ in asked}

    known: dict[str, Wallet] = {}
    history = ReplayHistory(known)
    wanted = set(subjects)

    # Every Transaction of each Subject wallet before the batch's last one: the features look at
    # the whole of it (counts, first use of a device, any earlier Counterparty). A set the cache
    # holds complete is its whole history; the other Wallets are read from Postgres.
    unread = []
    for wallet in subjects:
        cached = from_cache[("subject", wallet)]
        if cached is None:
            unread.append(wallet)
        else:
            history.load_subject(wallet, cached)
    past: dict[str, list[Transaction]] = {}
    if unread:
        for row in connection.execute(
            sa.select(*_TRANSACTION_COLUMNS)
            .where(any_of(_t.subject_wallet_id, unread), _t.occurred_at < latest)
            .order_by(_t.occurred_at, _t.transaction_id)
        ):
            loaded = _transaction(row)
            past.setdefault(loaded.subject_wallet_id, []).append(loaded)
        for wallet in unread:
            history.load_subject(wallet, past.get(wallet, []))

    unread = []
    for wallet in receivers:
        cached = from_cache[("receiver", wallet)]
        if cached is None:
            unread.append(wallet)
        else:
            for found in cached:
                history.add_received(found)
    if unread:
        for row in connection.execute(
            sa.select(*_TRANSACTION_COLUMNS)
            .where(
                _t.transaction_type == "transfer",
                any_of(_t.counterparty_wallet_id, unread),
                _t.occurred_at >= since,
                _t.occurred_at < latest,
            )
            .order_by(_t.occurred_at, _t.transaction_id)
        ):
            history.add_received(transaction_from_row(row))

    unread = []
    for agent in agents:
        cached = from_cache[("agent", agent)]
        if cached is None:
            unread.append(agent)
        else:
            for found in cached:
                history.add_at_agent(found)
                wanted.add(found.subject_wallet_id)
    if unread:
        # The features look up each Cash-out's Wallet next (its KYC tier), so read those too.
        for row in connection.execute(
            sa.select(*_TRANSACTION_COLUMNS, wallets.c.registered_at, wallets.c.kyc_tier)
            .join(wallets, wallets.c.wallet_id == _t.subject_wallet_id)
            .where(any_of(_t.agent_id, unread), _t.occurred_at >= since, _t.occurred_at < latest)
            .order_by(_t.occurred_at, _t.transaction_id)
        ):
            found = transaction_from_row(row[: len(_TRANSACTION_COLUMNS)])
            history.add_at_agent(found)
            known.setdefault(found.subject_wallet_id, Wallet(row[-2], row[-1]))

    for wallet_id, registered_at, kyc_tier in connection.execute(
        sa.select(wallets.c.wallet_id, wallets.c.registered_at, wallets.c.kyc_tier).where(
            any_of(wallets.c.wallet_id, wanted - known.keys())
        )
    ):
        known[wallet_id] = Wallet(registered_at, kyc_tier)

    for wallet_id, occurred_at in connection.execute(
        sa.select(sim_swaps.c.wallet_id, sim_swaps.c.occurred_at).where(
            any_of(sim_swaps.c.wallet_id, subjects), sim_swaps.c.occurred_at < latest
        )
    ):
        history.record_sim_swap(wallet_id, occurred_at)

    windows = [spend_window(t.occurred_at, config=config) for t in batch]
    first, last = min(w[0] for w in windows), max(w[1] for w in windows)
    for hour, amount in connection.execute(
        sa.select(market_hours.c.hour, sa.func.sum(market_hours.c.amount))
        .where(market_hours.c.hour >= first, market_hours.c.hour < last)
        .group_by(market_hours.c.hour)
    ):
        history.set_market_spend(hour, float(amount))
    return history
