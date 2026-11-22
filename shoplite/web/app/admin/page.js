"use client";
// app/admin/page.js: admin sign-in, and links to the admin pages.
//
// DEMO ADMIN ACCESS, NOT REAL AUTHENTICATION: one shared token (the API's
// ADMIN_TOKEN: its .env on the VM, the root .env locally). It's checked against the API before being kept, and
// kept only in this browser tab (sessionStorage), so closing the tab signs
// the admin out. It's never in the storefront's code or configuration.
//
// The sign-in screen is docs/design-reference's "Store admin" page, as-is,
// built on the same .signin-page styles as /signin (its own slim header and
// footer; the site-wide ones are hidden while it shows). Once signed in, the
// admin links render in the normal site layout.

import Link from "next/link";
import { useEffect, useState } from "react";

import { adminSignIn, adminSignOut, getAdminToken } from "../../lib/api";

export default function AdminPage() {
  const [signedIn, setSignedIn] = useState(null);
  const [token, setToken] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null); // message string

  useEffect(() => setSignedIn(Boolean(getAdminToken())), []);

  async function handleSignIn(event) {
    event.preventDefault();
    setShowToken(false); // hide the token again before sending it
    setBusy(true);
    setError(null);
    try {
      await adminSignIn(token.trim());
      setToken(""); // don't keep the secret in page state longer than needed
      setSignedIn(true);
    } catch (err) {
      setError(err.status === 401 ? "That admin token isn’t right." : err.message);
      event.target.elements["admin-token"].focus();
    } finally {
      setBusy(false);
    }
  }

  if (signedIn === null) return null; // checking this tab's storage

  if (signedIn) {
    return (
      <>
        <div className="page-header">
          <h1>Store admin</h1>
          <button type="button" className="btn" data-testid="admin-signout"
                  onClick={() => { adminSignOut(); setSignedIn(false); }}>
            Sign out of admin
          </button>
        </div>
        <ul className="product-grid">
          <li className="card stack">
            <h2>Inventory</h2>
            <p>Stock for every product, with a filter for items at or below their reorder level.</p>
            <Link className="btn btn-primary" href="/admin/inventory">Open inventory</Link>
          </li>
          <li className="card stack">
            <h2>Reports</h2>
            <p>Sales by day, product, or state, and best sellers, over any date range.</p>
            <Link className="btn btn-primary" href="/admin/reports">Open reports</Link>
          </li>
        </ul>
      </>
    );
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
            <h2>Store operations.</h2>
          </div>

          <div className="signin-form">
            <h1 id="signin-title">Store admin</h1>
            <p className="signin-intro">Enter your admin token to continue.</p>
            {/* The real token is a secret in the API's settings, so the page
                says where it comes from instead of showing one. */}
            <p className="signin-note" id="demo-note">
              <strong>Demo access</strong>
              Use the admin token from the API’s settings (<code className="demo-token">ADMIN_TOKEN</code>).
              It’s kept only in this browser tab.
            </p>
            <form onSubmit={handleSignIn}>
              <label htmlFor="admin-token">Admin token</label>
              <div className="token-field">
                <input
                  type={showToken ? "text" : "password"}
                  id="admin-token"
                  name="admin-token"
                  placeholder="Enter admin token"
                  autoComplete="off"
                  spellCheck={false}
                  required
                  aria-describedby="demo-note admin-feedback"
                  aria-invalid={error ? "true" : undefined}
                  value={token}
                  onChange={(e) => { setToken(e.target.value); setError(null); }}
                  data-testid="admin-token-input"
                />
                <button className="reveal" type="button" aria-controls="admin-token"
                        aria-label={showToken ? "Hide admin token" : "Show admin token"}
                        onClick={() => setShowToken((v) => !v)}>
                  {showToken ? "Hide" : "Show"}
                </button>
              </div>
              <button className="signin-button signin-primary" type="submit" disabled={busy}
                      data-testid="admin-signin-submit">
                {busy ? "Checking…" : "Sign in"}
              </button>
            </form>
            <p id="admin-feedback" className="signin-feedback" role="status" aria-live="polite"
               data-error={error ? "" : undefined}>
              {error}
            </p>
          </div>
        </section>
      </div>

      <footer className="signin-footer">
        <div className="signin-shell">ShopLite demo store · Admin access preview</div>
      </footer>
    </div>
  );
}
