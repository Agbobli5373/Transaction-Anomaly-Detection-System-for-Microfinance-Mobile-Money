-- The market's spend per clock hour (#47, ADR 0010): what the market spend index reads. Summing
-- every Transaction of eight days on each assessment would be slow, so a trigger keeps the hourly
-- sums as Transactions are stored. Nothing here is Ground truth (ADR 0007): it is the sum of what
-- the ledger already told us.

-- Many workers store Transactions at once, and each would otherwise wait for the one row of the
-- current hour. A Transaction adds to one of 16 rows of its hour, chosen by its ID, and a reader
-- sums them.
CREATE TABLE market_hours (
    -- The start of the clock hour, in UTC.
    hour   timestamptz NOT NULL,
    shard  smallint NOT NULL CHECK (shard BETWEEN 0 AND 15),
    amount numeric(16, 2) NOT NULL,
    PRIMARY KEY (hour, shard)
);

INSERT INTO market_hours (hour, shard, amount)
SELECT date_trunc('hour', occurred_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC',
       hashtext(transaction_id) & 15,
       sum(amount)
FROM transactions
GROUP BY 1, 2;

-- Runs as the table's owner, so the application role, which may only read market_hours, can still
-- have its stored Transactions counted. The transition table holds only the rows a statement
-- really inserted: a Transaction that is already stored (a redelivered event, which the insert
-- skips) is never counted twice. A statement adds up its own rows first, and takes the hour rows
-- in a fixed order, so a bulk insert is one upsert per hour and concurrent ones cannot deadlock.
CREATE FUNCTION market_hours_add() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
BEGIN
    INSERT INTO market_hours (hour, shard, amount)
    SELECT date_trunc('hour', occurred_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS hour,
           hashtext(transaction_id) & 15 AS shard,
           sum(amount)
    FROM new_rows
    GROUP BY 1, 2
    ORDER BY 1, 2
    ON CONFLICT (hour, shard) DO UPDATE SET amount = market_hours.amount + EXCLUDED.amount;
    RETURN NULL;
END;
$$;

CREATE TRIGGER transactions_add_to_market_hours
    AFTER INSERT ON transactions
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION market_hours_add();
