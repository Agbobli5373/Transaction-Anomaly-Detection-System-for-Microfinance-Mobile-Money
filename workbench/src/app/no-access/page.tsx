import type { Metadata } from "next";

import { current } from "@/lib/session";
import { redirect } from "next/navigation";

export const metadata: Metadata = { title: "No Case screens" };

export default async function NoAccess() {
  const session = await current();
  if (session.state !== "active") {
    redirect("/");
  }
  return (
    <section className="narrow">
      <h1>No Case screens for your role</h1>
      <p>
        You are signed in as {session.user.name}
        {session.user.roles.length ? ` (${session.user.roles.join(", ")})` : ""}. Cases and the Medium
        watch list are for Analysts and Supervisors. Admins manage API keys and models through the API.
      </p>
    </section>
  );
}
