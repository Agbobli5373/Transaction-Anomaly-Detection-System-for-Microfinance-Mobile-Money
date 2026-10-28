import { NextResponse } from "next/server";

import { api, attempt } from "@/lib/api";

type Params = Promise<{ id: string }>;

/**
 * The approved report's file, fetched from the API as the signed-in person (their access JWT stays
 * on the server) and handed to the browser as a download. The API refuses an Analyst, and a report
 * that is still a draft; its refusal is shown as text with the same status.
 */
export async function GET(_: Request, { params }: { params: Params }) {
  const caseId = Number((await params).id);
  if (!Number.isInteger(caseId) || caseId <= 0) {
    return new NextResponse("That is not a Case number.", { status: 404 });
  }
  const { client } = await api();
  const result = await attempt(() =>
    client.GET("/v1/cases/{case_id}/report/export", {
      params: { path: { case_id: caseId } },
      parseAs: "text",
    }),
  );
  if (!result.ok) {
    return new NextResponse(result.message, { status: result.status });
  }
  return new NextResponse(result.data as string, {
    headers: {
      "Content-Type": "application/json",
      "Content-Disposition": `attachment; filename="suspicious-transaction-report-case-${caseId}.json"`,
      "Cache-Control": "no-store",
    },
  });
}
