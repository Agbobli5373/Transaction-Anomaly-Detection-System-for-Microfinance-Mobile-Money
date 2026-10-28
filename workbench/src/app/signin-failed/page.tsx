import type { Metadata } from "next";

export const metadata: Metadata = { title: "Sign-in failed" };

export default function SignInFailed() {
  return (
    <section className="narrow">
      <h1>Sign-in did not complete</h1>
      <p role="alert">
        Keycloak did not confirm your sign-in, or it took too long. Try again.
      </p>
      <p>
        <a className="button" href="/auth/login">
          Try again
        </a>
      </p>
    </section>
  );
}
