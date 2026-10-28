"use client";

import { useActionState } from "react";

import { addNote, assignCase, moveCase, type FormState } from "@/app/cases/actions";
import type { Move } from "@/lib/permissions";

function Result({ state }: { state: FormState }) {
  if (!state) {
    return null;
  }
  return (
    <p role={state.ok ? "status" : "alert"} className={state.ok ? "result ok" : "result error"}>
      {state.message}
    </p>
  );
}

/** Notes: the textarea and button are disabled, with the reason, for a person who can't write one. */
export function NoteForm({
  caseId,
  disabledReason,
}: {
  caseId: number;
  disabledReason: string | null;
}) {
  const [state, action, pending] = useActionState(addNote.bind(null, caseId), null);
  const disabled = disabledReason !== null || pending;
  return (
    <form action={action} className="stack" aria-labelledby="note-heading">
      <h3 id="note-heading">Add a note</h3>
      <label htmlFor="note-text">Note</label>
      <textarea
        id="note-text"
        name="text"
        rows={3}
        maxLength={4000}
        required
        disabled={disabled}
        defaultValue={state && !state.ok ? state.text : undefined}
      />
      <div>
        <button type="submit" disabled={disabled}>
          Add note
        </button>
      </div>
      <Result state={state} />
    </form>
  );
}

/** Only the moves the Case's status allows are offered; none for a closed Case. */
export function MoveForm({
  caseId,
  moves,
  disabledReason,
}: {
  caseId: number;
  moves: Move[];
  disabledReason: string | null;
}) {
  const [state, action, pending] = useActionState(moveCase.bind(null, caseId), null);
  if (moves.length === 0) {
    return null;
  }
  const disabled = disabledReason !== null || pending;
  return (
    <form action={action} className="stack" aria-labelledby="move-heading">
      <h3 id="move-heading">Move this Case</h3>
      <label htmlFor="move-comment">Comment (optional)</label>
      <textarea
        id="move-comment"
        name="comment"
        rows={2}
        maxLength={4000}
        disabled={disabled}
        defaultValue={state && !state.ok ? state.text : undefined}
      />
      <div className="row">
        {moves.map((move) => (
          <button
            key={move.to}
            type="submit"
            name="to"
            value={move.to}
            disabled={disabled}
            className={move.to === "closed_fraud" ? "danger" : undefined}
          >
            {move.label}
          </button>
        ))}
      </div>
      <Result state={state} />
    </form>
  );
}

/** Assignment is a Supervisor's; the page does not render this for anyone else. */
export function AssignForm({
  caseId,
  currentName,
}: {
  caseId: number;
  currentName: string | null;
}) {
  const [state, action, pending] = useActionState(assignCase.bind(null, caseId), null);
  return (
    <form action={action} className="stack" aria-labelledby="assign-heading">
      <h3 id="assign-heading">{currentName ? "Reassign this Case" : "Assign this Case"}</h3>
      <label htmlFor="assignee-id">Analyst ID</label>
      <input
        id="assignee-id"
        name="assignee_id"
        type="text"
        required
        maxLength={128}
        autoComplete="off"
        disabled={pending}
        defaultValue={state && !state.ok ? state.text : undefined}
        aria-describedby="assignee-hint"
      />
      <label htmlFor="assignee-name">Analyst name (shown on the Case)</label>
      <input
        id="assignee-name"
        name="assignee_name"
        type="text"
        maxLength={128}
        autoComplete="off"
        disabled={pending}
      />
      <p id="assignee-hint" className="hint">
        An Analyst finds their ID in the page header after signing in.
      </p>
      <div>
        <button type="submit" disabled={pending}>
          Assign
        </button>
      </div>
      <Result state={state} />
    </form>
  );
}
