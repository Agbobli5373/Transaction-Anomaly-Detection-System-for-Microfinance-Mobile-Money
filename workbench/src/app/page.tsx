import { redirect } from "next/navigation";

import { hasCaseScreens } from "@/lib/permissions";
import { current } from "@/lib/session";

export default async function Home() {
  const session = await current();
  if (session.state === "expired") {
    redirect("/auth/logout?reason=expired");
  }
  if (session.state === "active") {
    redirect(hasCaseScreens(session.user) ? "/cases" : "/no-access");
  }
  return (
    <section className="narrow">
      <h1>Case workbench</h1>
      <p>
        Analysts and Supervisors work the Cases that Transaction risk scoring raises. Sign in with your
        organisation account to continue.
      </p>
      <p>
        <a className="button" href="/auth/login">
          Sign in
        </a>
      </p>
    </section>
  );
}
