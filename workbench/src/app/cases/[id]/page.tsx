import type { Metadata } from "next";
import Link from "next/link";

import { BandBadge, StatusBadge } from "@/components/Badges";
import { AssignForm, MoveForm, NoteForm } from "@/components/CaseForms";
import { api, attempt, type Schemas } from "@/lib/api";
import {
  formatAmount,
  formatDateTime,
  formatReasonValue,
  formatScore,
  labelOf,
  maskPhoneNumbers,
} from "@/lib/format";
import { requireCaseUser } from "@/lib/guards";
import {
  STATUS_LABELS,
  canAssign,
  nextMoves,
  whyNotWorkable,
} from "@/lib/permissions";

type Params = Promise<{ id: string }>;

export const metadata: Metadata = { title: "Case" };

function EventLine({ event }: { event: Schemas["CaseEventOut"] }) {
  let what: string;
  if (event.kind === "assigned") {
    what = "assigned the Case";
  } else if (event.kind === "transition") {
    what = `moved the Case from ${STATUS_LABELS[event.from_status ?? "open"]} to ${
      STATUS_LABELS[event.to_status ?? "open"]
    }`;
  } else {
    what = "added a note";
  }
  return (
    <li>
      <p>
        <strong>{event.actor_name}</strong> {what}
        {event.kind === "assigned" && event.assignee_id ? (
          <>
            {" "}
            to <code>{event.assignee_id}</code>
          </>
        ) : null}{" "}
        <time dateTime={event.occurred_at}>{formatDateTime(event.occurred_at)}</time>
      </p>
      {event.body ? <p className="note">{maskPhoneNumbers(event.body)}</p> : null}
    </li>
  );
}

function AlertCard({ alert }: { alert: Schemas["CaseAlertOut"] }) {
  const a = alert.assessment;
  return (
    <article className="card" aria-labelledby={`alert-${alert.alert_id}`}>
      <h3 id={`alert-${alert.alert_id}`}>Alert {alert.alert_id}</h3>
      <p className="meta">
        Transaction <code>{alert.transaction_id}</code> · raised{" "}
        <time dateTime={alert.raised_at}>{formatDateTime(alert.raised_at)}</time>
      </p>
      <dl className="facts">
        <div>
          <dt>Risk score</dt>
          <dd data-testid="risk-score">{formatScore(a.risk_score)}</dd>
        </div>
        <div>
          <dt>Risk band</dt>
          <dd>
            <BandBadge band={a.risk_band} />
          </dd>
        </div>
        <div>
          <dt>Model</dt>
          <dd>
            {a.model_version}
            {a.degraded ? " (Degraded mode: Rule hits alone)" : ""}
          </dd>
        </div>
        {alert.disposition ? (
          <div>
            <dt>Disposition</dt>
            <dd>{alert.disposition === "confirmed_fraud" ? "Confirmed fraud" : "Dismissed"}</dd>
          </div>
        ) : null}
      </dl>
      <h4>Reasons</h4>
      {a.reasons.length === 0 ? (
        <p>No Reasons were recorded.</p>
      ) : (
        <ul className="reasons">
          {a.reasons.map((r, i) => (
            <li key={`${r.code}-${i}`}>
              <span>{r.message}</span>{" "}
              <span className="hint">
                ({labelOf(r.code)}: <span data-testid="reason-value">{formatReasonValue(r.value)}</span>)
              </span>
            </li>
          ))}
        </ul>
      )}
    </article>
  );
}

export default async function CaseView({ params }: { params: Params }) {
  const user = await requireCaseUser();
  const { id } = await params;
  const caseId = Number(id);
  if (!Number.isInteger(caseId) || caseId <= 0) {
    return <Missing message="That is not a Case number." />;
  }

  const { client } = await api();
  const result = await attempt(() =>
    client.GET("/v1/cases/{case_id}", { params: { path: { case_id: caseId } } }),
  );
  if (!result.ok) {
    return <Missing message={result.message} />;
  }
  const { case: c, alerts, recent_transactions: transactions, events } = result.data;
  const moves = nextMoves(c.status);
  const blocked = whyNotWorkable(user, c);

  return (
    <>
      <p>
        <Link href="/cases">Back to Cases</Link>
      </p>
      <h1>Case {c.case_id}</h1>
      <dl className="facts">
        <div>
          <dt>Status</dt>
          <dd>
            <StatusBadge status={c.status} />
          </dd>
        </div>
        <div>
          <dt>Subject wallet</dt>
          <dd>{c.subject_wallet_id}</dd>
        </div>
        <div>
          <dt>Assignee</dt>
          <dd data-testid="assignee">{c.assignee_name ?? (c.assignee_id ? "Assigned" : "Unassigned")}</dd>
        </div>
        <div>
          <dt>Opened</dt>
          <dd>{formatDateTime(c.opened_at)}</dd>
        </div>
        {c.closed_at ? (
          <div>
            <dt>Closed</dt>
            <dd>
              {formatDateTime(c.closed_at)}
              {c.closed_by_name ? ` by ${c.closed_by_name}` : ""}
            </dd>
          </div>
        ) : null}
      </dl>

      <section aria-labelledby="alerts-heading">
        <h2 id="alerts-heading">Alerts</h2>
        {alerts.map((alert) => (
          <AlertCard key={alert.alert_id} alert={alert} />
        ))}
      </section>

      <section aria-labelledby="transactions-heading">
        <h2 id="transactions-heading">Recent Transactions of the Subject wallet</h2>
        {transactions.length === 0 ? (
          <p>No Transactions are stored for this wallet.</p>
        ) : (
          <div className="table-wrap" role="region" aria-label="Recent Transactions" tabIndex={0}>
            <table>
              <caption className="sr-only">The Subject wallet&apos;s latest Transactions</caption>
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Type</th>
                  <th scope="col" className="num">
                    Amount
                  </th>
                  <th scope="col">Region</th>
                  <th scope="col">Channel</th>
                  <th scope="col">Counterparty</th>
                  <th scope="col" className="num">
                    Risk score
                  </th>
                  <th scope="col">Risk band</th>
                </tr>
              </thead>
              <tbody>
                {transactions.map(({ transaction: t, assessment }) => (
                  <tr key={t.transaction_id}>
                    <td>{formatDateTime(t.occurred_at)}</td>
                    <td>{labelOf(t.transaction_type)}</td>
                    <td className="num">{formatAmount(t.amount)}</td>
                    <td>{t.region}</td>
                    <td>{t.channel}</td>
                    <td>{maskPhoneNumbers(t.counterparty_wallet_id ?? t.agent_id ?? "none")}</td>
                    <td className="num">{formatScore(assessment?.risk_score)}</td>
                    <td>{assessment ? <BandBadge band={assessment.risk_band} /> : "Not scored"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section aria-labelledby="work-heading" className="work">
        <h2 id="work-heading">Work this Case</h2>
        {blocked ? (
          <p className="hint" data-testid="work-blocked">
            {blocked}
          </p>
        ) : null}
        {canAssign(user, c) ? <AssignForm caseId={c.case_id} currentName={c.assignee_name} /> : null}
        {moves.length > 0 || !blocked ? (
          <>
            <NoteForm caseId={c.case_id} disabledReason={blocked} />
            <MoveForm caseId={c.case_id} moves={moves} disabledReason={blocked} />
          </>
        ) : null}
      </section>

      <section aria-labelledby="history-heading">
        <h2 id="history-heading">Notes and history</h2>
        {events.length === 0 ? (
          <p>Nothing has been recorded yet.</p>
        ) : (
          <ol className="timeline" data-testid="history">
            {events.map((e) => (
              <EventLine key={e.event_id} event={e} />
            ))}
          </ol>
        )}
      </section>
    </>
  );
}

function Missing({ message }: { message: string }) {
  return (
    <>
      <p>
        <Link href="/cases">Back to Cases</Link>
      </p>
      <h1>Case unavailable</h1>
      <p role="alert" className="result error">
        {message}
      </p>
    </>
  );
}
