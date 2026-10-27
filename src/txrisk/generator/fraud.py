"""Fraud scenarios and SIM swaps injected into the simulated market."""

from dataclasses import dataclass
from datetime import UTC, datetime, time

import numpy as np
import pandas as pd

from txrisk.generator.config import FraudScenario, GeneratorConfig, Range, SimSwapTakeover
from txrisk.generator.market import (
    CASH_IN,
    CASH_OUT,
    TRANSFER,
    members_by_region,
    region_codes,
    region_weights,
)
from txrisk.generator.population import Population

_SCENARIOS = list(FraudScenario)


@dataclass(frozen=True)
class FraudContext:
    """What fraud planning needs to know about the Wallets after everyday activity."""

    contacts: np.ndarray
    registered: np.ndarray  # naive UTC datetime64 per Wallet
    single_limit: np.ndarray  # pesewas per Wallet
    max_balance: np.ndarray  # pesewas per Wallet
    usual_balance: np.ndarray  # median balance (pesewas) per Wallet in everyday activity
    everyday_transactions: int


@dataclass(frozen=True)
class FraudPlan:
    candidates: pd.DataFrame  # same columns as everyday candidates, with scenario and instance set
    sim_swaps: pd.DataFrame  # wallet (index) and occurred_at


def _period_start(config: GeneratorConfig) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(config.period.start, time(), tzinfo=UTC))


def _legitimate_sim_swaps(
    config: GeneratorConfig, context: FraudContext, rng: np.random.Generator
) -> pd.DataFrame:
    start = _period_start(config)
    n_wallets = len(context.registered)
    count = round(config.fraud.legitimate_sim_swap_share * n_wallets)
    wallets = rng.choice(n_wallets, count, replace=False)
    seconds = rng.integers(0, config.period.days * 86_400, count)
    swaps = pd.DataFrame(
        {"wallet": wallets, "occurred_at": start + pd.to_timedelta(seconds, unit="s")}
    )
    # A Wallet's SIM can only be swapped after it exists.
    occurred = swaps["occurred_at"].dt.tz_convert(None).to_numpy()
    after_registration: pd.DataFrame = swaps[occurred >= context.registered[wallets]]
    return after_registration


class _Planner:
    """Builds fraud candidates one scenario instance at a time."""

    def __init__(
        self,
        config: GeneratorConfig,
        population: Population,
        context: FraudContext,
        rng: np.random.Generator,
    ) -> None:
        self.config = config
        self.context = context
        self.rng = rng
        self.start = _period_start(config)
        self.regions = list(config.market.regions)
        self.region_weights = region_weights(config)
        self.agents_by_region = members_by_region(population, self.regions)["agent_id"]
        self.n_agents = len(population.agents)
        self.home = region_codes(population.wallets["home_region"], self.regions)
        self.agent_region = region_codes(population.agents["region"], self.regions)
        preexisting = context.registered < self.start.tz_convert(None).to_datetime64()
        self.preexisting = np.flatnonzero(preexisting)
        # Stolen money goes to Wallets that can hold a lot of it.
        roomy = context.max_balance == context.max_balance.max()
        self.receivers = np.flatnonzero(preexisting & roomy)
        # Fraudsters go after Wallets that usually hold money worth taking.
        worth_taking = context.usual_balance >= config.fraud.victim_min_median_balance * 100
        self.victims = np.flatnonzero(preexisting & worth_taking)
        self.rows: list[dict[str, object]] = []
        self.sim_swaps: list[dict[str, object]] = []
        self.instance = 0
        self.involved: set[int] = set()  # Wallets already used by an instance

    # Drawing parameters

    def uniform(self, value: Range) -> float:
        return float(self.rng.uniform(value.low, value.high))

    def count(self, value: Range) -> int:
        return int(self.rng.integers(round(value.low), round(value.high) + 1))

    def active_from(self, scenario: FraudScenario) -> float:
        """The day a scenario starts: the drift day for the new variant, otherwise day 0."""
        drift = self.config.fraud.drift
        return float(drift.day) if scenario == drift.new_scenario else 0.0

    def instances(
        self,
        scenario: FraudScenario,
        transactions_per_instance: float,
        last_day_margin: float = 1.0,
    ) -> int:
        """How many instances give this scenario its configured share of the fraud rate while it
        is active. A scenario that never becomes active in the period gets none."""
        fraud = self.config.fraud
        days = self.config.period.days - last_day_margin
        active = max(days - self.active_from(scenario), 0.0) / days
        if active == 0:
            return 0
        share = fraud.scenarios.share(scenario) * active
        target = fraud.rate * self.context.everyday_transactions * share
        return max(1, round(target / transactions_per_instance))

    def moment(self, scenario: FraudScenario, last_day_margin: float = 1.0) -> pd.Timestamp:
        """A random moment while the scenario is active, leaving room for it to play out."""
        first = self.active_from(scenario) * 86_400
        seconds = self.rng.uniform(first, (self.config.period.days - last_day_margin) * 86_400)
        return self.start + pd.Timedelta(seconds=seconds)

    def drifted(self, at: pd.Timestamp) -> bool:
        return at >= self.start + pd.Timedelta(days=self.config.fraud.drift.day)

    def sim_swap_params(self, at: pd.Timestamp) -> SimSwapTakeover:
        """SIM-swap takeovers that begin after the drift day may follow a shifted pattern."""
        drift = self.config.fraud.drift
        shifted = drift.sim_swap_takeover
        if (
            shifted is not None
            and drift.shifted_scenario == FraudScenario.SIM_SWAP_TAKEOVER
            and self.drifted(at)
        ):
            return shifted
        return self.config.fraud.scenarios.sim_swap_takeover

    def victim(self) -> int:
        """A Wallet worth defrauding that no other instance has targeted yet, where possible."""
        untouched = np.setdiff1d(self.victims, list(self.involved))
        wallet = int(self.rng.choice(untouched if untouched.size else self.victims))
        self.involved.add(wallet)
        return wallet

    def victims_except(self, count: int, excluded: set[int]) -> list[int]:
        """Distinct victims, none of them in `excluded`, preferring untargeted ones; falls back to
        any pre-existing Wallet when the market runs short."""
        chosen: list[int] = []
        for pool, avoid in (
            (self.victims, excluded | self.involved),
            (self.victims, excluded),
            (self.preexisting, excluded),
        ):
            available = np.setdiff1d(pool, list(avoid | set(chosen)))
            take = min(count - len(chosen), available.size)
            chosen += [int(w) for w in self.rng.choice(available, take, replace=False)]
            if len(chosen) == count:
                break
        self.involved.update(chosen)
        return chosen

    def accomplices(self, count: int) -> list[int]:
        """Distinct pre-existing Wallets no other instance has used, where possible."""
        unused = np.setdiff1d(self.preexisting, list(self.involved))
        pool = unused if unused.size >= count else self.preexisting
        chosen = [int(w) for w in self.rng.choice(pool, count, replace=False)]
        self.involved.update(chosen)
        return chosen

    def other_agent_in(self, region: int, avoid: int) -> int:
        for _ in range(10):
            agent = self.agent_in(region)
            if agent != avoid:
                return agent
        return (avoid + 1) % self.n_agents

    def strangers(self, wallet: int | None, count: int) -> list[int]:
        """Distinct high-capacity Wallets outside this Wallet's circle, preferring ones no other
        instance has used; falls back to any Wallet when the market runs short."""
        outside = set() if wallet is None else {wallet, *self.context.contacts[wallet].tolist()}
        chosen: list[int] = []
        for pool, excluded in (
            (self.receivers, outside | self.involved),
            (self.receivers, outside),
            (self.preexisting, outside),
        ):
            available = np.setdiff1d(pool, list(excluded | set(chosen)))
            take = min(count - len(chosen), available.size)
            chosen += [int(w) for w in self.rng.choice(available, take, replace=False)]
            if len(chosen) == count:
                break
        self.involved.update(chosen)
        return chosen

    def remote_region(self) -> int:
        return int(self.rng.choice(len(self.regions), p=self.region_weights))

    def agent_in(self, region: int) -> int:
        here = self.agents_by_region[region]
        return int(self.rng.choice(here)) if here.size else int(self.rng.integers(self.n_agents))

    def new_device(self) -> str:
        return f"D{int(self.rng.integers(0, 2**63 - 1)):016x}"

    # Recording

    def begin(self) -> int:
        self.instance += 1
        return self.instance

    def add(
        self,
        scenario: FraudScenario,
        at: pd.Timestamp,
        wallet: int,
        type_index: int,
        location: int,
        *,
        amount_pesewas: int = 0,
        drain_share: float = np.nan,
        receiver: int = 0,
        agent: int = 0,
        device: str | None = None,
        channel: str | None = None,
        require_exact: bool = False,
        fraudulent: bool = True,
    ) -> None:
        self.rows.append(
            {
                "occurred_at": at,
                "subject": wallet,
                "type_index": type_index,
                "intended_pesewas": amount_pesewas,
                "counterparty_wallet": receiver,
                "location": location,
                "agent_id": agent,
                "merchant_id": 0,
                "biller_id": 0,
                "scenario": _SCENARIOS.index(scenario) if fraudulent else -1,
                "instance": self.instance if fraudulent else -1,
                "device": device,
                "channel": channel,
                "drain_share": drain_share,
                "require_exact": require_exact,
            }
        )

    def sim_swap(self, wallet: int, at: pd.Timestamp) -> None:
        self.sim_swaps.append({"wallet": wallet, "occurred_at": at})

    # Scenarios

    def sim_swap_takeover(self) -> None:
        scenario = FraudScenario.SIM_SWAP_TAKEOVER
        params = self.config.fraud.scenarios.sim_swap_takeover
        for _ in range(self.instances(scenario, params.transactions.mean)):
            self.begin()
            wallet, swapped = self.victim(), self.moment(scenario)
            params = self.sim_swap_params(swapped)
            self.sim_swap(wallet, swapped)
            # The fraudster now receives the Wallet's PINs and OTPs on their own phone.
            device, channel = self.new_device(), str(self.rng.choice(["ussd", "app"]))
            at = swapped + pd.Timedelta(hours=self.uniform(params.delay_hours))
            for _ in range(self.count(params.transactions)):
                region = self.remote_region()
                cash_out = self.rng.random() < 0.5
                self.add(
                    scenario,
                    at,
                    wallet,
                    CASH_OUT if cash_out else TRANSFER,
                    region,
                    agent=self.agent_in(region) if cash_out else 0,
                    receiver=0 if cash_out else self.strangers(wallet, 1)[0],
                    drain_share=self.uniform(params.drain_share),
                    device=device,
                    channel=channel,
                )
                at += pd.Timedelta(minutes=self.uniform(Range(low=2, high=30)))

    def account_takeover_drain(self) -> None:
        scenario = FraudScenario.ACCOUNT_TAKEOVER_DRAIN
        params = self.config.fraud.scenarios.account_takeover_drain
        for _ in range(self.instances(scenario, params.transactions.mean)):
            self.begin()
            wallet, at = self.victim(), self.moment(scenario)
            count = self.count(params.transactions)
            burst = pd.Timedelta(minutes=self.uniform(params.burst_minutes))
            offsets = np.sort(self.rng.uniform(0, 1, count))
            offsets -= offsets[0]  # the burst starts with its first Transfer
            device, region = self.new_device(), self.remote_region()
            for offset, receiver in zip(offsets, self.strangers(wallet, count), strict=True):
                self.add(
                    scenario,
                    at + burst * float(offset),
                    wallet,
                    TRANSFER,
                    region,
                    receiver=receiver,
                    drain_share=self.uniform(params.drain_share),
                    device=device,
                    channel="app",
                )

    def mule_fan_in(self) -> None:
        scenario = FraudScenario.MULE_FAN_IN
        params = self.config.fraud.scenarios.mule_fan_in
        per_instance = params.senders.mean + params.cash_outs.mean
        for _ in range(self.instances(scenario, per_instance, last_day_margin=2.0)):
            self.begin()
            start = self.moment(scenario, last_day_margin=2.0)
            window = pd.Timedelta(hours=self.uniform(params.window_hours))
            senders = self.count(params.senders)
            # The mule is a stranger to every sender: scam victims paying who they were told to.
            mule = self.strangers(None, 1)[0]
            last = start
            for sender in self.victims_except(senders, {mule}):
                at = start + window * float(self.rng.uniform(0, 1))
                last = max(last, at)
                amount = round(self.uniform(params.sent_amount) * 100)
                region = int(self.home[sender])
                self.add(
                    scenario, at, sender, TRANSFER, region, amount_pesewas=amount, receiver=mule
                )
            at = last + pd.Timedelta(hours=self.uniform(params.cash_out_delay_hours))
            cash_outs = self.count(params.cash_outs)
            region = int(self.home[mule])
            for remaining in range(cash_outs, 0, -1):
                # Split what has arrived evenly over the remaining Cash-outs.
                self.add(
                    scenario,
                    at,
                    mule,
                    CASH_OUT,
                    region,
                    agent=self.agent_in(region),
                    drain_share=1 / remaining,
                )
                at += pd.Timedelta(minutes=self.uniform(Range(low=5, high=40)))

    def agent_structuring(self) -> None:
        scenario = FraudScenario.AGENT_STRUCTURING
        params = self.config.fraud.scenarios.agent_structuring
        # Each structured Cash-out is preceded by the Cash-in that loads it.
        for _ in range(self.instances(scenario, 2 * params.cash_outs.mean)):
            self.begin()
            agent = int(self.rng.integers(self.n_agents))
            agent_region = int(self.agent_region[agent])
            start = self.moment(scenario)
            window = pd.Timedelta(hours=self.uniform(params.window_hours))
            for wallet in self.accomplices(self.count(params.cash_outs)):
                at = start + window * float(self.rng.uniform(0, 1))
                amount = round(self.uniform(params.limit_share) * self.context.single_limit[wallet])
                home = int(self.home[wallet])
                loaded = at - pd.Timedelta(minutes=self.uniform(Range(low=10, high=180)))
                self.add(
                    scenario,
                    loaded,
                    wallet,
                    CASH_IN,
                    home,
                    agent=self.other_agent_in(home, agent),
                    amount_pesewas=amount,
                )
                self.add(
                    scenario,
                    at,
                    wallet,
                    CASH_OUT,
                    agent_region,
                    agent=agent,
                    amount_pesewas=amount,
                    require_exact=True,
                )

    def reversal_scam(self) -> None:
        scenario = FraudScenario.REVERSAL_SCAM
        params = self.config.fraud.scenarios.reversal_scam
        for _ in range(self.instances(scenario, 2)):
            self.begin()
            victim, at = self.victim(), self.moment(scenario)
            fraudster = self.strangers(victim, 1)[0]
            bait = round(self.uniform(params.bait_amount) * 100)
            # "Sorry, I sent that to you by mistake. Please send it back"... and more besides.
            self.add(
                scenario,
                at,
                fraudster,
                TRANSFER,
                int(self.home[fraudster]),
                amount_pesewas=bait,
                receiver=victim,
            )
            self.add(
                scenario,
                at + pd.Timedelta(minutes=self.uniform(params.reply_minutes)),
                victim,
                TRANSFER,
                int(self.home[victim]),
                amount_pesewas=round(bait * self.uniform(params.return_multiple)),
                receiver=fraudster,
            )

    def social_engineering_push(self) -> None:
        scenario = FraudScenario.SOCIAL_ENGINEERING_PUSH
        params = self.config.fraud.scenarios.social_engineering_push
        for _ in range(self.instances(scenario, 1)):
            self.begin()
            # The victim is persuaded and acts from their own phone: no device or Channel override.
            wallet, at = self.victim(), self.moment(scenario)
            home = int(self.home[wallet])
            top_up = round(
                self.uniform(params.top_up_multiple) * self.context.usual_balance[wallet]
            )
            topped_up = at - pd.Timedelta(minutes=self.uniform(params.top_up_lead_minutes))
            # The top-up is the victim's own legitimate Cash-in; only the push is fraud.
            self.add(
                scenario,
                topped_up,
                wallet,
                CASH_IN,
                home,
                agent=self.agent_in(home),
                amount_pesewas=top_up,
                fraudulent=False,
            )
            self.add(
                scenario,
                at,
                wallet,
                TRANSFER,
                home,
                receiver=self.strangers(wallet, 1)[0],
                drain_share=self.uniform(params.drain_share),
            )


def plan_fraud(
    config: GeneratorConfig,
    population: Population,
    context: FraudContext,
    rng: np.random.Generator,
) -> FraudPlan:
    planner = _Planner(config, population, context, rng)
    planner.sim_swap_takeover()
    planner.account_takeover_drain()
    planner.mule_fan_in()
    planner.agent_structuring()
    planner.social_engineering_push()
    planner.reversal_scam()

    swaps = pd.concat(
        [
            _legitimate_sim_swaps(config, context, rng),
            pd.DataFrame(planner.sim_swaps, columns=["wallet", "occurred_at"]),
        ],
        ignore_index=True,
    )
    return FraudPlan(
        candidates=pd.DataFrame(planner.rows),
        sim_swaps=swaps.sort_values(["occurred_at", "wallet"], kind="stable", ignore_index=True),
    )
