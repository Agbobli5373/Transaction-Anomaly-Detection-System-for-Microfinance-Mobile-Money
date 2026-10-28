"use server";

import { revalidatePath } from "next/cache";

import { api, attempt } from "@/lib/api";

/**
 * The Case actions. Each is a server action: the form posts to the Next.js server, which calls the
 * API as the signed-in person, so the person's access JWT never reaches the browser. The API
 * decides whether the person may; its refusal (403, 409, 404) comes back as the message shown.
 */

export type FormState = { ok: boolean; message: string; text?: string } | null;

function field(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value.trim() : "";
}

function refreshed(caseId: number): void {
  revalidatePath(`/cases/${caseId}`);
}

export async function addNote(caseId: number, _: FormState, form: FormData): Promise<FormState> {
  const text = field(form, "text");
  if (!text) {
    return { ok: false, message: "Write a note first.", text };
  }
  const { client } = await api();
  const result = await attempt(() =>
    client.POST("/v1/cases/{case_id}/notes", {
      params: { path: { case_id: caseId } },
      body: { text },
    }),
  );
  if (!result.ok) {
    return { ok: false, message: result.message, text };
  }
  refreshed(caseId);
  return { ok: true, message: "Note added." };
}

const MOVES = ["investigating", "closed_fraud", "closed_legitimate"] as const;

export async function moveCase(caseId: number, _: FormState, form: FormData): Promise<FormState> {
  const to = MOVES.find((m) => m === field(form, "to"));
  const comment = field(form, "comment");
  if (!to) {
    return { ok: false, message: "Choose a move.", text: comment };
  }
  const { client } = await api();
  const result = await attempt(() =>
    client.POST("/v1/cases/{case_id}/transition", {
      params: { path: { case_id: caseId } },
      body: { to, comment: comment || null },
    }),
  );
  if (!result.ok) {
    return { ok: false, message: result.message, text: comment };
  }
  refreshed(caseId);
  return { ok: true, message: "Case updated." };
}

export async function assignCase(
  caseId: number,
  _: FormState,
  form: FormData,
): Promise<FormState> {
  const assignee = field(form, "assignee_id");
  const name = field(form, "assignee_name");
  if (!assignee) {
    return { ok: false, message: "Enter the Analyst's ID.", text: assignee };
  }
  const { client } = await api();
  const result = await attempt(() =>
    client.POST("/v1/cases/{case_id}/assign", {
      params: { path: { case_id: caseId } },
      body: { assignee_id: assignee, assignee_name: name || null },
    }),
  );
  if (!result.ok) {
    return { ok: false, message: result.message, text: assignee };
  }
  refreshed(caseId);
  return { ok: true, message: "Case assigned." };
}

export async function draftReport(
  caseId: number,
  _: FormState,
  form: FormData,
): Promise<FormState> {
  const summary = field(form, "summary");
  if (!summary) {
    return { ok: false, message: "Write the summary first.", text: summary };
  }
  const { client } = await api();
  const result = await attempt(() =>
    client.POST("/v1/cases/{case_id}/report", {
      params: { path: { case_id: caseId } },
      body: { summary },
    }),
  );
  if (!result.ok) {
    return { ok: false, message: result.message, text: summary };
  }
  refreshed(caseId);
  return { ok: true, message: "Report draft saved." };
}

export async function approveReport(caseId: number): Promise<FormState> {
  const { client } = await api();
  const result = await attempt(() =>
    client.POST("/v1/cases/{case_id}/report/approve", {
      params: { path: { case_id: caseId } },
    }),
  );
  if (!result.ok) {
    return { ok: false, message: result.message };
  }
  refreshed(caseId);
  return { ok: true, message: "Report approved." };
}
