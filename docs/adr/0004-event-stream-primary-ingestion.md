# Event stream as the primary ingestion path

Transactions reach the system as `transaction.created` events on a Redpanda topic, consumed by scoring workers that publish `score.produced`, because real transaction monitoring is fed by the ledger's event stream, not by per-transaction API calls. The synchronous `POST /v1/score` endpoint stays for callers that need an immediate answer, and both paths run the same scoring service. The versioned event schemas are the documented ledger contract (see ADR 0003).

## Considered Options

- **API only**: simpler, but would not show how the system integrates with a ledger, and couples ledger throughput to API latency.
- **Stream only**: drops the immediate-answer path that the sync check and the load-test latency claim rely on.
- **Kafka**: same protocol as Redpanda; Redpanda chosen because it runs as a single lightweight container in compose and kind.
