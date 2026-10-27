# Synthetic generator as the primary dataset

We generate our own Ghana-style mobile money dataset (Wallets, Agents, Merchants, regions, devices, SIM swaps, injected Fraud scenarios and Benign anomalies) and use it as the primary dataset. PaySim is kept only as a secondary benchmark, run on the features it supports. PaySim lacks location, Agent, Merchant, device, SIM swap and clock-time attributes, so most of the planned features cannot be computed on it, and its fraud is trivially separable through zeroed balances.

## Consequences

Models risk learning the generator's own rules rather than fraud. We mitigate this by randomising every Fraud scenario's parameters and injecting Benign anomalies that look unusual but are legitimate, and the PaySim benchmark gives an external check on data we did not design.
