-- The parties, the history the features read, and the Risk assessments and Alerts scoring writes.
-- Ground truth and Fraud discoveries from the generator never come here (ADR 0007).

CREATE TABLE wallets (
    -- The HMAC pseudonym of the phone number; the number itself is never stored.
    wallet_id     text PRIMARY KEY,
    kyc_tier      text NOT NULL,
    home_region   text NOT NULL,
    registered_at timestamptz NOT NULL
);

CREATE TABLE agents (
    agent_id text PRIMARY KEY,
    region   text NOT NULL
);

CREATE TABLE merchants (
    merchant_id text PRIMARY KEY,
    category    text NOT NULL,
    region      text NOT NULL
);

CREATE TABLE billers (
    biller_id text PRIMARY KEY,
    category  text NOT NULL
);

CREATE TABLE sim_swaps (
    wallet_id   text NOT NULL REFERENCES wallets,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (wallet_id, occurred_at)
);

CREATE TABLE transactions (
    transaction_id         text PRIMARY KEY,
    occurred_at            timestamptz NOT NULL,
    transaction_type       text NOT NULL
        CHECK (transaction_type IN ('cash_in', 'cash_out', 'transfer', 'payment', 'bill_payment')),
    amount                 numeric(14, 2) NOT NULL CHECK (amount > 0),
    subject_wallet_id      text NOT NULL REFERENCES wallets,
    -- Money moves into the Subject wallet for a Cash-in only.
    direction              text NOT NULL CHECK ((direction = 'in') = (transaction_type = 'cash_in')),
    subject_balance_before numeric(14, 2) NOT NULL CHECK (subject_balance_before >= 0),
    channel                text NOT NULL CHECK (channel IN ('ussd', 'app')),
    device_id              text NOT NULL,
    region                 text NOT NULL,
    -- Exactly one Counterparty, of the kind the Transaction type deals with.
    counterparty_wallet_id text REFERENCES wallets,
    agent_id               text REFERENCES agents,
    merchant_id            text REFERENCES merchants,
    biller_id              text REFERENCES billers,
    CHECK ((counterparty_wallet_id IS NOT NULL) = (transaction_type = 'transfer')),
    CHECK ((agent_id IS NOT NULL) = (transaction_type IN ('cash_in', 'cash_out'))),
    CHECK ((merchant_id IS NOT NULL) = (transaction_type = 'payment')),
    CHECK ((biller_id IS NOT NULL) = (transaction_type = 'bill_payment'))
);

-- One index per history the features read: the Subject wallet's own Transactions, Transfers into
-- a Wallet, and Cash-ins and Cash-outs at an Agent, each by event time.
CREATE INDEX transactions_by_subject_wallet ON transactions (subject_wallet_id, occurred_at);
CREATE INDEX transfers_by_receiving_wallet ON transactions (counterparty_wallet_id, occurred_at)
    WHERE transaction_type = 'transfer';
CREATE INDEX transactions_by_agent ON transactions (agent_id, occurred_at)
    WHERE agent_id IS NOT NULL;

-- The Champion model's assessment of a Transaction; at most one each.
CREATE TABLE risk_assessments (
    transaction_id   text PRIMARY KEY REFERENCES transactions,
    risk_score       double precision NOT NULL CHECK (risk_score BETWEEN 0 AND 100),
    risk_band        text NOT NULL CHECK (risk_band IN ('Low', 'Medium', 'High')),
    -- [{"code": ..., "message": ..., "value": ...}], at most three, in order.
    reasons          jsonb NOT NULL,
    -- {"rules": ..., "anomaly": ..., "supervised": ...}, each 0-1 or null.
    component_scores jsonb NOT NULL,
    model_version    text NOT NULL,
    degraded         boolean NOT NULL,
    -- The simulation clock when it was scored: the Transaction's event time (ADR 0005).
    assessed_at      timestamptz NOT NULL
);

-- A Transaction whose Risk band reached High; at most one per Transaction.
CREATE TABLE alerts (
    alert_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id    text NOT NULL UNIQUE REFERENCES risk_assessments,
    subject_wallet_id text NOT NULL REFERENCES wallets,
    raised_at         timestamptz NOT NULL
);

CREATE INDEX alerts_by_subject_wallet ON alerts (subject_wallet_id, alert_id);
