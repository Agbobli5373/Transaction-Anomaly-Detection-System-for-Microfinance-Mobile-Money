import Link from "next/link";

// Next.js's own 404 page styles itself inline, which the Content-Security-Policy refuses.
export default function NotFound() {
  return (
    <section className="narrow">
      <h1>Page not found</h1>
      <p>
        There is nothing at this address. <Link href="/">Back to the workbench</Link>.
      </p>
    </section>
  );
}
