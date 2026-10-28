"""The history the features read, served from Postgres.

It answers exactly as the offline replay (`ReplayHistory`) does: every window is by event time and
sees nothing at or after `before`, so served features equal the features training used.
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa

from txrisk.features import SubjectHistory, Transaction, Wallet
from txrisk.observability.tracing import traced
from txrisk.store.tables import sim_swaps, transactions, wallets

_t = transactions.c
# Columns in the order `Transaction` takes them; the Counterparty is whichever column is set.
_TRANSACTION_COLUMNS = (
    _t.transaction_id,
    _t.occurred_at,
    _t.transaction_type,
    _t.amount,
    _t.subject_wallet_id,
    _t.direction,
    sa.func.coalesce(_t.counterparty_wallet_id, _t.agent_id, _t.merchant_id, _t.biller_id),
    _t.region,
    _t.subject_balance_before,
    _t.device_id,
    _t.channel,
)
_ORDER = (_t.occurred_at, _t.transaction_id)


def transaction_from_row(row: Sequence[object]) -> Transaction:
    """A row of `_TRANSACTION_COLUMNS` as the features see it."""
    return Transaction(*row)  # type: ignore[arg-type]


def select_transactions() -> sa.Select[tuple[object, ...]]:
    return sa.select(*_TRANSACTION_COLUMNS)


class PostgresHistory:
    """History as of whatever is committed in Postgres, read over one connection.

    Use one per assessment: it remembers the Wallets and Subject wallet histories it has read, as
    the features ask for some of them more than once.
    """

    def __init__(self, connection: sa.Connection) -> None:
        self._connection = connection
        self._wallets: dict[str, Wallet] = {}
        self._subject: dict[tuple[str, datetime], SubjectHistory] = {}

    @traced("history.postgres.wallet")
    def wallet(self, wallet_id: str) -> Wallet:
        if wallet_id not in self._wallets:
            row = self._connection.execute(
                sa.select(wallets.c.registered_at, wallets.c.kyc_tier).where(
                    wallets.c.wallet_id == wallet_id
                )
            ).one_or_none()
            if row is None:
                raise KeyError(wallet_id)
            self._wallets[wallet_id] = Wallet(registered_at=row[0], kyc_tier=row[1])
        return self._wallets[wallet_id]

    @traced("history.postgres.subject_history")
    def subject_history(self, wallet_id: str, before: datetime) -> SubjectHistory:
        key = (wallet_id, before)
        if key not in self._subject:
            rows = self._connection.execute(
                select_transactions()
                .where(_t.subject_wallet_id == wallet_id, _t.occurred_at < before)
                .order_by(*_ORDER)
            )
            self._subject[key] = SubjectHistory(
                registered_at=self.wallet(wallet_id).registered_at,
                transactions=[transaction_from_row(row) for row in rows],
            )
        return self._subject[key]

    @traced("history.postgres.incoming_transfers")
    def incoming_transfers(
        self, wallet_id: str, since: datetime, before: datetime
    ) -> Sequence[Transaction]:
        rows = self._connection.execute(
            select_transactions()
            .where(
                _t.transaction_type == "transfer",
                _t.counterparty_wallet_id == wallet_id,
                _t.occurred_at >= since,
                _t.occurred_at < before,
            )
            .order_by(*_ORDER)
        )
        return [transaction_from_row(row) for row in rows]

    @traced("history.postgres.agent_transactions")
    def agent_transactions(
        self, agent_id: str, since: datetime, before: datetime
    ) -> Sequence[Transaction]:
        # The features look up each Cash-out's Wallet next, so read those Wallets in passing.
        rows = self._connection.execute(
            sa.select(*_TRANSACTION_COLUMNS, wallets.c.registered_at, wallets.c.kyc_tier)
            .join(wallets, wallets.c.wallet_id == _t.subject_wallet_id)
            .where(_t.agent_id == agent_id, _t.occurred_at >= since, _t.occurred_at < before)
            .order_by(*_ORDER)
        )
        found = []
        for row in rows:
            transaction = transaction_from_row(row[: len(_TRANSACTION_COLUMNS)])
            self._wallets.setdefault(
                transaction.subject_wallet_id, Wallet(registered_at=row[-2], kyc_tier=row[-1])
            )
            found.append(transaction)
        return found

    @traced("history.postgres.last_sim_swap")
    def last_sim_swap(self, wallet_id: str, before: datetime) -> datetime | None:
        latest: datetime | None = self._connection.execute(
            sa.select(sa.func.max(sim_swaps.c.occurred_at)).where(
                sim_swaps.c.wallet_id == wallet_id, sim_swaps.c.occurred_at < before
            )
        ).scalar_one()
        return latest
