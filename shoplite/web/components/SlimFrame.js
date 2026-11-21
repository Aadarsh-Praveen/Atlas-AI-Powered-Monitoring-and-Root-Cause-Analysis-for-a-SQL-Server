// components/SlimFrame.js: the page frame from the design-reference pages
// (Checkout, Your orders): a slim dark header with the brand and "Back to
// shop", the page content, and a one-line footer. Built on the .signin-page
// styles, so the site-wide header and footer are hidden while it shows.

import Link from "next/link";

export default function SlimFrame({ children, footer = "ShopLite demo store · No payments or shipments" }) {
  return (
    <div className="signin-page slim-page">
      <header className="signin-header">
        <div className="signin-shell signin-header-inner">
          <Link href="/" className="signin-brand" aria-label="ShopLite Hardware Supply, home">
            <span className="signin-mark" aria-hidden="true">S</span>
            <span>SHOP<strong>LITE</strong><small>HARDWARE SUPPLY</small></span>
          </Link>
          <Link className="signin-back" href="/">Back to shop</Link>
        </div>
      </header>
      <div className="slim-main signin-shell">{children}</div>
      <footer className="signin-footer">
        <div className="signin-shell">{footer}</div>
      </footer>
    </div>
  );
}
