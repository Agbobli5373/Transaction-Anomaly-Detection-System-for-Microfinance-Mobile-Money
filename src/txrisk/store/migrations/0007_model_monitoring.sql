-- Model monitoring (#21). Two additive tables; neither holds Ground truth (ADR 0007).

-- The model inputs the Champion model scored each Transaction with, as it served them, so the
-- daily monitoring job can compare the live distribution of each input with the training
-- baseline. A key per input column (an empty feature is null), written with the Risk assessment.
-- Kept only as long as monitoring needs (config/retention.toml).
CREATE TABLE model_inputs (
    transaction_id text PRIMARY KEY,
    assessed_at    timestamptz NOT NULL,
    model_version  text NOT NULL,
    inputs         jsonb NOT NULL
);

CREATE INDEX model_inputs_by_time ON model_inputs (assessed_at);

-- What `txrisk monitor` computed for a simulated day: one row per metric and, for a metric with
-- dimensions, per feature, segment, Risk band or source. `dimension` and `member` are '' when the
-- metric has none. Running a day again replaces its rows. The latest day's rows are what reach
-- Prometheus (through the Pushgateway); this table is the history.
CREATE TABLE monitoring_metrics (
    day       date NOT NULL,
    metric    text NOT NULL,
    dimension text NOT NULL DEFAULT '',
    member    text NOT NULL DEFAULT '',
    value     double precision NOT NULL,
    PRIMARY KEY (day, metric, dimension, member)
);
