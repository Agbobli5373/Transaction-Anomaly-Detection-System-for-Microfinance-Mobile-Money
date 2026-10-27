"""A small hand-built market for scoring tests: one Subject wallet with a known routine."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from txrisk.features import FeatureConfig, ReplayHistory, Transaction, Wallet
from txrisk.scoring import FixedClock, RiskAssessment, ScoringService, load_rules_config

RULES_CONFIG = Path(__file__).resolve().parents[1] / "config" / "rules.toml"
NOW = datetime(2030, 6, 1, 14, 0, tzinfo=UTC)
REGISTERED = datetime(2030, 1, 1, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
SUBJECT = "w-subject"
PHONE = "D-own-phone"
FEATURES = FeatureConfig(kyc_limits={"minimum": 1000.0, "enhanced": 15000.0})

_ids = iter(range(1_000_000))


def tx(
    at: datetime = NOW,
    transaction_type: str = "transfer",
    *,
    amount: float = 50.0,
    wallet: str = SUBJECT,
    counterparty: str = "w-friend",
    region: str = "Ashanti",
    balance: float = 1000.0,
    device: str = PHONE,
) -> Transaction:
    return Transaction(
        transaction_id=f"T{next(_ids):09d}",
        occurred_at=at,
        transaction_type=transaction_type,
        amount=amount,
        subject_wallet_id=wallet,
        direction="in" if transaction_type == "cash_in" else "out",
        counterparty_id=counterparty,
        region=region,
        subject_balance_before=balance,
        device_id=device,
    )


def routine(days: int = 6) -> list[Transaction]:
    """The Subject wallet's normal life: a 50 GHS Transfer to a friend at 14:00 each day."""
    return [tx(NOW - DAY * d) for d in range(days, 0, -1)]


class _Wallets(dict[str, Wallet]):
    """Every Wallet is registered on 1 January, on the minimum tier unless told otherwise."""

    def __init__(self, tiers: dict[str, str]) -> None:
        super().__init__()
        self.tiers = tiers

    def __missing__(self, wallet_id: str) -> Wallet:
        return Wallet(registered_at=REGISTERED, kyc_tier=self.tiers.get(wallet_id, "minimum"))


class Market:
    def __init__(self, *, tiers: dict[str, str] | None = None) -> None:
        self.history = ReplayHistory(_Wallets(tiers or {}))

    def record(self, *transactions: Transaction) -> "Market":
        for t in transactions:
            self.history.record(t)
        return self

    def sim_swap(self, at: datetime) -> "Market":
        self.history.record_sim_swap(SUBJECT, at)
        return self

    def assess(self, transaction: Transaction) -> RiskAssessment:
        service = ScoringService(
            self.history,
            FixedClock(transaction.occurred_at),
            features=FEATURES,
            rules=load_rules_config(RULES_CONFIG),
        )
        return service.assess(transaction)

    def codes(self, transaction: Transaction) -> list[str]:
        return [r.code for r in self.assess(transaction).reasons]
