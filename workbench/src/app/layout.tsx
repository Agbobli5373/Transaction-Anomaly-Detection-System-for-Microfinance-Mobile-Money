import type { Metadata, Viewport } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { hasCaseScreens } from "@/lib/permissions";
import { current } from "@/lib/session";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Case workbench", template: "%s | Case workbench" },
  description: "Analysts and Supervisors work Cases raised by Transaction risk scoring.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

// Every page depends on who is signed in.
export const dynamic = "force-dynamic";

export default async function RootLayout({ children }: { children: ReactNode }) {
  const session = await current();
  const user = session.state === "active" ? session.user : null;
  return (
    <html lang="en">
      <body>
        <a className="skip-link" href="#main">
          Skip to content
        </a>
        <header className="site-header">
          <div className="bar">
            <Link href="/" className="brand">
              Case workbench
            </Link>
            {user && hasCaseScreens(user) ? (
              <nav aria-label="Main">
                <Link href="/cases">Cases</Link>
                <Link href="/watchlist">Medium watch list</Link>
              </nav>
            ) : null}
            {user ? (
              <div className="who">
                <span data-testid="signed-in-as">
                  {user.name} <span className="roles">({user.roles.join(", ") || "no role"})</span>
                </span>
                {user.roles.includes("analyst") ? (
                  <span className="hint" data-testid="user-id">
                    Your ID: <code>{user.sub}</code>
                  </span>
                ) : null}
                <form action="/auth/logout" method="post">
                  <button type="submit" className="secondary">
                    Sign out
                  </button>
                </form>
              </div>
            ) : null}
          </div>
        </header>
        <main id="main" tabIndex={-1}>
          {children}
        </main>
      </body>
    </html>
  );
}
