import type { Metadata } from "next";
import Link from "next/link";

import { BandBadge } from "@/components/Badges";
import { api, attempt } from "@/lib/api";
import { formatAmount, formatDateTime, formatScore, labelOf, maskPhoneNumbers } from "@/lib/format";
import { requireCaseUser, single, withParams } from "@/lib/guards";

export const metadata: Metadata = { title: "Medium watch list" };

type Search = Promise<Record<string, string | string[] | undefined>>;

const TYPES = ["cash_in", "cash_out", "transfer", "payment", "bill_payment"] as const;

export default async function Watchlist({ searchParams }: { searchParams: Search }) {
  await requireCaseUser();
  const query = await searchParams;
  const wallet = single(query.wallet);
  const region = single(query.region);
  const typeParam = single(query.transaction_type);
  const transactionType = TYPES.find((t) => t === typeParam);
  const before = single(query.before);

  const { client } = await api();
  const result = await attempt(() =>
    client.GET("/v1/watchlist", {
      params: {
        query: { wallet, region, transaction_type: transactionType, before, limit: 25 },
      },
    }),
  );
  const filters = { wallet, region, transaction_type: transactionType };

  return (
    <>
      <h1>Medium watch list</h1>
      <p className="hint">
        Medium-band Transactions, newest first. They raised no Alert; they are here to watch.
      </p>

      <form method="get" action="/watchlist" className="filters" aria-label="Filter the watch list">
        <div>
          <label htmlFor="w-wallet">Wallet</label>
          <input id="w-wallet" name="wallet" type="text" defaultValue={wallet ?? ""} autoComplete="off" />
        </div>
        <div>
          <label htmlFor="w-region">Region</label>
          <input id="w-region" name="region" type="text" defaultValue={region ?? ""} autoComplete="off" />
        </div>
        <div>
          <label htmlFor="w-type">Transaction type</label>
          <select id="w-type" name="transaction_type" defaultValue={transactionType ?? ""}>
            <option value="">Any</option>
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {labelOf(t)}
              </option>
            ))}
          </select>
        </div>
        <div className="actions">
          <button type="submit">Filter</button>
          <Link className="button secondary" href="/watchlist">
            Clear
          </Link>
        </div>
      </form>

      {!result.ok ? (
        <p role="alert" className="result error">
          {result.message}
        </p>
      ) : result.data.items.length === 0 ? (
        <p>No Medium Transactions match.</p>
      ) : (
        <>
          <div className="table-wrap" role="region" aria-label="Medium watch list" tabIndex={0}>
            <table>
              <caption className="sr-only">Medium-band Transactions, newest first</caption>
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Subject wallet</th>
                  <th scope="col">Type</th>
                  <th scope="col" className="num">
                    Amount
                  </th>
                  <th scope="col">Region</th>
                  <th scope="col" className="num">
                    Risk score
                  </th>
                  <th scope="col">Risk band</th>
                  <th scope="col">Reasons</th>
                </tr>
              </thead>
              <tbody>
                {result.data.items.map(({ transaction: t, assessment: a }) => (
                  <tr key={t.transaction_id}>
                    <td>{formatDateTime(t.occurred_at)}</td>
                    <td>
                      <Link href={withParams("/watchlist", { wallet: t.subject_wallet_id })}>
                        {t.subject_wallet_id}
                      </Link>
                    </td>
                    <td>{labelOf(t.transaction_type)}</td>
                    <td className="num">{formatAmount(t.amount)}</td>
                    <td>{t.region}</td>
                    <td className="num">{formatScore(a.risk_score)}</td>
                    <td>
                      <BandBadge band={a.risk_band} />
                    </td>
                    <td>
                      {a.reasons.length === 0
                        ? "None"
                        : a.reasons.map((r) => maskPhoneNumbers(r.message)).join("; ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <nav className="pager" aria-label="Pages of the watch list">
            {before ? <Link href={withParams("/watchlist", filters)}>Back to the newest</Link> : null}
            {result.data.next_before ? (
              <Link
                rel="next"
                href={withParams("/watchlist", { ...filters, before: result.data.next_before })}
              >
                Older Transactions
              </Link>
            ) : null}
          </nav>
        </>
      )}
    </>
  );
}
