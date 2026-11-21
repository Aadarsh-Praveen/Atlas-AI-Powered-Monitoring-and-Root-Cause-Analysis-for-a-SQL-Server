"use client";
// app/signin/page.js: demo sign-in.
//
// DEMO, NOT REAL AUTHENTICATION: no passwords. Anyone who types a seeded
// customer's email becomes that customer. The Phase 3 simulated shoppers use
// "Sign in as a demo customer".
//
// The design is docs/design-reference's ShopLite sign-in page, as-is: its own
// slim header (brand + "Back to shop"), a two-panel card (workshop photo and
// tagline, then the form), and a one-line footer. globals.css hides the
// site-wide header and footer while this page is showing (.signin-page).

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { getCustomer, login, signInAsDemoCustomer, signOut } from "../../lib/api";

const DEMO_EMAIL = "james.smith1@example.com";

export default function SignInPage() {
  const router = useRouter();
  const [current, setCurrent] = useState(null);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState(null); // { message, isError }

  useEffect(() => setCurrent(getCustomer()), []);

  async function signIn(action) {
    setBusy(true);
    setFeedback(null);
    try {
      const customer = await action();
      setFeedback({ message: `Signed in as ${customer?.email ?? "a demo customer"}.`, isError: false });
      router.push("/"); // back to the aisles, signed in
    } catch (err) {
      // The API's own words, e.g. "No customer with that email".
      setFeedback({ message: err.message, isError: true });
      setBusy(false);
    }
  }

  function handleSubmit(event) {
    event.preventDefault();
    const address = email.trim().toLowerCase();
    // Seeded customers all use @example.com; catch typos before calling the API.
    if (!address.endsWith("@example.com")) {
      setFeedback({ message: "Use an @example.com address for this demo.", isError: true });
      event.currentTarget.elements.email.focus();
      return;
    }
    signIn(() => login(address));
  }

  return (
    <div className="signin-page">
      <header className="signin-header">
        <div className="signin-shell signin-header-inner">
          <Link href="/" className="signin-brand" aria-label="ShopLite Hardware Supply, home">
            <span className="signin-mark" aria-hidden="true">S</span>
            <span>SHOP<strong>LITE</strong><small>HARDWARE SUPPLY</small></span>
          </Link>
          <Link className="signin-back" href="/">Back to shop</Link>
        </div>
      </header>

      <div className="signin-main signin-shell">
        <section className="signin" aria-labelledby="signin-title">
          <div className="signin-workshop">
            <p>ShopLite Hardware</p>
            <h2>Built for your next project.</h2>
          </div>

          <div className="signin-form">
            {current ? (
              <>
                <h1 id="signin-title">You’re signed in</h1>
                <p className="signin-intro">
                  Signed in as <strong>{current.first_name} {current.last_name}</strong> ({current.email}).
                </p>
                <Link className="signin-button signin-primary" href="/">Continue shopping</Link>
                <div className="signin-divider">or</div>
                <button type="button" className="signin-button signin-secondary"
                        onClick={() => { signOut(); setCurrent(null); }}>
                  Sign out
                </button>
              </>
            ) : (
              <>
                <h1 id="signin-title">Sign in</h1>
                <p className="signin-intro">Continue to the ShopLite demo as a customer.</p>
                <p className="signin-note" id="demo-note">
                  <strong>Demo store</strong>
                  Use a sample email address or try a demo customer. No password required.
                </p>
                <form onSubmit={handleSubmit}>
                  <label htmlFor="email">Email address</label>
                  <input
                    type="email"
                    id="email"
                    name="email"
                    placeholder={DEMO_EMAIL}
                    autoComplete="off"
                    spellCheck={false}
                    required
                    maxLength={256}
                    aria-describedby="demo-note"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    data-testid="signin-email-input"
                  />
                  <button className="signin-button signin-primary" type="submit" disabled={busy}
                          data-testid="signin-submit">
                    {busy ? "Signing in…" : "Sign in"}
                  </button>
                </form>
                <div className="signin-divider">or</div>
                <button className="signin-button signin-secondary" type="button" disabled={busy}
                        onClick={() => signIn(signInAsDemoCustomer)} data-testid="signin-demo-button">
                  Sign in as a demo customer
                </button>
              </>
            )}
            <p className="signin-feedback" role="status" aria-live="polite"
               data-error={feedback?.isError ? "" : undefined}>
              {feedback?.message}
            </p>
          </div>
        </section>
      </div>

      <footer className="signin-footer">
        <div className="signin-shell">ShopLite demo store · No payments or shipments</div>
      </footer>
    </div>
  );
}
