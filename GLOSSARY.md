# Transaction Risk Scoring

Scores mobile money wallet transactions for risk and explains why, so that people can review unusual activity. The system flags; it never blocks a transaction or declares fraud.

## Money movement

**Wallet**:
A mobile money account held by a customer, from which or into which a Transaction moves money.
_Avoid_: Account, user

**Transaction**:
A single movement of money involving a Wallet, of exactly one Transaction type.
_Avoid_: Payment (that is one Transaction type), transfer (likewise)

**Subject wallet**:
The one customer Wallet whose behaviour a Transaction is assessed against: the receiving Wallet for a Cash-in, the sending Wallet for every other Transaction type.
_Avoid_: Sender, source wallet, owner

**Direction**:
Whether money moved into (in) or out of (out) the Subject wallet.
_Avoid_: Flow, sign

**Transaction type**:
The kind of money movement: Cash-in, Cash-out, Transfer, Payment, or Bill payment.

**Cash-in**:
A customer gives physical cash to an Agent and receives the same value in their Wallet.
_Avoid_: Deposit, top-up

**Cash-out**:
A customer moves value out of their Wallet to an Agent and receives physical cash.
_Avoid_: Withdrawal

**Transfer**:
Money moved from one customer's Wallet to another customer's Wallet.
_Avoid_: P2P, send money

**Payment**:
Money moved from a customer's Wallet to a Merchant.
_Avoid_: Purchase, merchant transaction

**Bill payment**:
Money moved from a customer's Wallet to a utility or service biller, including airtime.
_Avoid_: Debit

**Agent**:
A registered person or shop that exchanges physical cash for Wallet value on behalf of the operator.
_Avoid_: Vendor, merchant

**Merchant**:
A business that accepts Payments into its own Wallet.
_Avoid_: Agent, shop

**Biller**:
A utility or service provider that receives Bill payments, including airtime sellers.
_Avoid_: Merchant, payee

**Counterparty**:
The other party to a Transaction besides the Subject wallet: another Wallet, an Agent, a Merchant, or a Biller.
_Avoid_: Recipient (it is not always the receiver), beneficiary

**KYC tier**:
The verification level of a Wallet, which sets its transaction and balance limits.
_Avoid_: Account level, wallet class

**Channel**:
How a Transaction was initiated: USSD or the mobile App.
_Avoid_: Platform, medium

## Risk assessment

**Anomaly score**:
How unusual a Transaction looks relative to normal behaviour, as judged by an unsupervised model. It measures unusualness, not guilt.
_Avoid_: Fraud score, suspicion

**Rule hit**:
A deterministic check that fired for a Transaction, such as an amount far above the Wallet's usual range.
_Avoid_: Flag, trigger

**Critical rule**:
A rule whose hit puts a Transaction in the High Risk band regardless of the other components.
_Avoid_: Hard rule, override, block rule

**Reason**:
A human-readable explanation attached to a Risk score, naming a Rule hit or a feature that drove the score.
_Avoid_: Explanation code, cause

**Fraud label**:
The system's knowledge that a Transaction was fraudulent, existing only from its Fraud discovery onward. An unlabelled Transaction counts as legitimate only once it is older than the Label maturity window.
_Avoid_: Fraud flag, is-fraud

**Ground truth**:
The simulation's complete knowledge of which Transactions were fraudulent, used only to evaluate the system offline and never visible to it.
_Avoid_: Fraud label (that is what the system has discovered), true label

**Label maturity window**:
How old a Transaction must be before the absence of a Fraud label is taken to mean it was legitimate.
_Avoid_: Label delay, cooling-off period

**Risk score**:
The single number returned for a Transaction, combining its Rule hits, its Anomaly score and, where available, a supervised fraud probability.
_Avoid_: Fraud score, anomaly score

**Risk band**:
The Low, Medium, or High category a Risk score falls into.
_Avoid_: Risk level, decision

**Profile**:
The recent behaviour of one Wallet or Agent against which a new Transaction is compared.
_Avoid_: History, baseline

## Review

**Audit log**:
The append-only record of who viewed, changed, issued or exported what, and when. The application can add to it and read it, never alter or delete an entry.
_Avoid_: Activity log, history (a Wallet's history is its past Transactions)

**Alert**:
A Transaction whose Risk band reached High, queued for a person to review.
_Avoid_: Flag, suspicious transaction

**Case**:
The unit of investigation that groups the open Alerts of one Subject wallet. A Case is open, investigating, closed as fraud, or closed as legitimate.
_Avoid_: Ticket, investigation, incident

**Alert disposition**:
The verdict on an Alert, confirmed fraud or dismissed, taken from how its Case closed.
_Avoid_: Resolution, outcome, feedback

**Alert budget**:
The number of Alerts reviewers can handle per day.
_Avoid_: Capacity, quota

**Suspicious transaction report**:
A supervisor-approved export describing a Case closed as fraud, in the form a regulator expects. The system produces it; it never files it.
_Avoid_: SAR, STR filing

**Analyst**:
A person who investigates Cases and records findings.
_Avoid_: Reviewer, investigator, user

**Supervisor**:
A person who assigns Cases to Analysts and approves Suspicious transaction reports.
_Avoid_: Manager, approver

**Fraud discovery**:
The moment a past Transaction becomes known to be fraudulent, either through a customer complaint or chargeback, or through a Case closed as fraud. Only Alerted Transactions can be discovered through a Case.
_Avoid_: Label arrival, feedback

## Model governance

**Champion model**:
The model version whose Risk scores are acted on.
_Avoid_: Production model, live model

**Challenger model**:
A candidate model version that scores the same Transactions in shadow, with its scores recorded but never acted on.
_Avoid_: Candidate, test model

**Retraining**:
Training a new bundle from the Transactions and Fraud labels known at the simulated time, honouring the Label maturity window, and registering it as the Challenger model. It runs weekly, or early on a drift action alert.
_Avoid_: Refresh, update

**Promotion**:
Making a Challenger model the Champion model, allowed only when it passes its promotion gates.
_Avoid_: Deployment, release

**Training baseline**:
The distribution of every model input, and of the Risk score, on the rows a Champion model was built from, kept inside its bundle for monitoring to compare live Transactions with. Never made from later data.
_Avoid_: Reference data, Profile (that is a Wallet's recent behaviour)

**Drift**:
A Champion model's live inputs or Risk scores moving away from its Training baseline, measured by the population stability index (PSI): 0.1 is a warning, 0.25 needs action.
_Avoid_: Decay, staleness

**Degraded mode**:
Scoring with Rule hits alone because the Champion model is unavailable. Risk scores produced this way are marked and later re-scored.
_Avoid_: Fallback mode, safe mode

## Behaviour patterns

**Fraud scenario**:
A recognisable pattern of fraudulent behaviour, such as an account takeover draining a Wallet through Transfers.
_Avoid_: Fraud type, attack

**SIM swap**:
The moment a Wallet's phone number is moved onto a new SIM card.
_Avoid_: SIM change, number port

**SIM-swap takeover**:
A Fraud scenario in which a fraudster performs a SIM swap and then drains the Wallet within hours.

**Account takeover drain**:
A Fraud scenario in which a fraudster gains access from a new device and moves money out in a burst of Transfers to new counterparties.
_Avoid_: ATO

**Mule fan-in**:
A Fraud scenario in which many unrelated Wallets send Transfers to one Wallet, which quickly cashes the money out.
_Avoid_: Money mule (that is the Wallet's holder, not the pattern)

**Agent structuring**:
A Fraud scenario in which an Agent processes many Cash-outs just under a limit to avoid scrutiny.
_Avoid_: Smurfing, agent collusion

**Social-engineering push**:
A Fraud scenario in which a deceived customer willingly sends an unusually large Transfer to a first-time recipient.
_Avoid_: Scam, authorised push payment

**Reversal scam**:
A Fraud scenario in which a stranger sends a small Transfer as bait, claims it was a mistake, and gets the victim to send back more than they received.
_Avoid_: Wrong-number scam, refund fraud

**Benign anomaly**:
Unusual but legitimate behaviour, such as a salary-day spike or a one-off large Payment.
_Avoid_: False positive (that is a scoring outcome, not a behaviour)
