# Production-grade engineering on synthetic data

We build the system to production standard in everything we control: authentication and audit, observability, model registry with shadow scoring and gated promotion, drift monitoring, case management, degraded-mode fallback, privacy controls, CI/CD and deployment manifests. Anything that depends on an outside party is either an interface we define or a documented limitation, never faked as done. This replaces the earlier "portfolio project" framing.

## Consequences

- **Interface only:** ledger integration is a documented event schema consumed from a topic, with replay standing in for the ledger; suspicious transaction reporting is an export file, never submitted.
- **Out of our control, documented as limitations:** real operator data, regulatory approval, an institution's model risk sign-off, external penetration testing, and contractual SLAs.
