// app/not-found.js: shown for any URL that doesn't match a page.
// A server component: no data, no JavaScript needed. Plain language: what
// happened, and where to go next.

import Link from "next/link";

export const metadata = { title: "Page not found" };

export default function NotFound() {
  return (
    <div className="card stack" style={{ maxWidth: 640 }}>
      <h1>That aisle doesn’t exist</h1>
      <p>We couldn’t find the page you asked for. The link may be old, or the address mistyped.</p>
      <p>
        <Link className="btn btn-primary" href="/">Browse all aisles</Link>{" "}
        <Link className="btn" href="/cart">Go to your cart</Link>
      </p>
    </div>
  );
}
