import type { Metadata } from "next";
import Link from "next/link";

import { StatusBadge } from "@/components/Badges";
import { api, attempt } from "@/lib/api";
import { formatDateTime, formatScore } from "@/lib/format";
import { requireCaseUser, single, withParams } from "@/lib/guards";
import { STATUS_LABELS, isSupervisor, type CaseStatus } from "@/lib/permissions";

export const metadata: Metadata = { title: "Cases" };

type Search = Promise<Record<string, string | string[] | undefined>>;

const STATUSES = Object.keys(STATUS_LABELS) as CaseStatus[];

export default async function CaseQueue({ searchParams }: { searchParams: Search }) {
  const user = await requireCaseUser();
  const query = await searchParams;
  const statusParam = single(query.status);
  const status = STATUSES.find((s) => s === statusParam);
  const assignee = single(query.assignee);
  const wallet = single(query.wallet);
  const before = Number(single(query.before));

  const { client } = await api();
  const result = await attempt(() =>
    client.GET("/v1/cases", {
      params: {
        query: {
          status,
          assignee,
          wallet,
          before: Number.isInteger(before) && before > 0 ? before : undefined,
          limit: 25,
        },
      },
    }),
  );
  const filters = { status, assignee, wallet };

  return (
    <>
      <h1>Cases</h1>
      <p className="hint">
        {isSupervisor(user)
          ? "All Cases, newest first."
          : "Cases assigned to you and the unassigned open ones, newest first."}
      </p>

      <form method="get" action="/cases" className="filters" aria-label="Filter Cases">
        <div>
          <label htmlFor="f-status">Status</label>
          <select id="f-status" name="status" defaultValue={status ?? ""}>
            <option value="">Any</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {STATUS_LABELS[s]}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="f-assignee">Assignee</label>
          <input
            id="f-assignee"
            name="assignee"
            type="text"
            list="assignee-choices"
            defaultValue={assignee ?? ""}
            placeholder="me, or an Analyst ID"
            autoComplete="off"
          />
          <datalist id="assignee-choices">
            <option value="me">Assigned to me</option>
          </datalist>
        </div>
        <div>
          <label htmlFor="f-wallet">Wallet</label>
          <input
            id="f-wallet"
            name="wallet"
            type="text"
            defaultValue={wallet ?? ""}
            autoComplete="off"
          />
        </div>
        <div className="actions">
          <button type="submit">Filter</button>
          <Link className="button secondary" href="/cases">
            Clear
          </Link>
        </div>
      </form>

      {!result.ok ? (
        <p role="alert" className="result error">
          {result.message}
        </p>
      ) : result.data.cases.length === 0 ? (
        <p>No Cases match.</p>
      ) : (
        <>
          <div className="table-wrap" role="region" aria-label="Cases" tabIndex={0}>
            <table>
              <caption className="sr-only">Cases, newest first</caption>
              <thead>
                <tr>
                  <th scope="col">Case</th>
                  <th scope="col">Subject wallet</th>
                  <th scope="col">Status</th>
                  <th scope="col">Assignee</th>
                  <th scope="col" className="num">
                    Alerts
                  </th>
                  <th scope="col" className="num">
                    Top Risk score
                  </th>
                  <th scope="col">Opened</th>
                </tr>
              </thead>
              <tbody>
                {result.data.cases.map((c) => (
                  <tr key={c.case_id}>
                    <th scope="row">
                      <Link href={`/cases/${c.case_id}`}>Case {c.case_id}</Link>
                    </th>
                    <td>{c.subject_wallet_id}</td>
                    <td>
                      <StatusBadge status={c.status} />
                    </td>
                    <td>{c.assignee_name ?? (c.assignee_id ? "Assigned" : "Unassigned")}</td>
                    <td className="num">{c.alert_count}</td>
                    <td className="num">{formatScore(c.top_risk_score)}</td>
                    <td>{formatDateTime(c.opened_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <nav className="pager" aria-label="Pages of Cases">
            {before ? (
              <Link href={withParams("/cases", filters)}>Back to the newest</Link>
            ) : null}
            {result.data.next_before ? (
              <Link rel="next" href={withParams("/cases", { ...filters, before: String(result.data.next_before) })}>
                Older Cases
              </Link>
            ) : null}
          </nav>
        </>
      )}
    </>
  );
}
