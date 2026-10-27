# Splits follow label maturity

The first Champion model goes live on day 90 and may use only labels known then: Fraud discoveries before day 90, with an undiscovered Transaction counting as legitimate only once it is 30 days old (ADR 0007). That makes only days 0–60 fully labelled on day 90. So everything that needs labels is fitted inside that window: training on days 0–45 and calibration (and the stacker, #11) on days 45–60. Thresholds need only the distribution of scores, not labels, so they are set on the most recent days, 60–90. Evaluation stays on days 90–120.

## Considered Options

- **Calibrate on days 60–75 with immature labels.** Simpler, but it treats undiscovered fraud as legitimate and so under-counts fraud, which biases the probabilities low.
- **Calibrate with Ground truth.** It gives perfect labels, but no real operation has them, which breaks ADR 0007.
