import type { Metadata } from "next";

export const metadata: Metadata = { title: "Signed out" };

export default async function SignedOut({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string }>;
}) {
  const { reason } = await searchParams;
  return (
    <section className="narrow">
      <h1>You are signed out</h1>
      <p role="status">
        {reason === "expired"
          ? "Your session ended and could not be renewed, so you were signed out."
          : "You signed out of the Case workbench and of Keycloak."}
      </p>
      <p>
        <a className="button" href="/auth/login">
          Sign in again
        </a>
      </p>
    </section>
  );
}
