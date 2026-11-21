// app/layout.js: the root layout. Every page renders inside it.
// It's a server component (no "use client"): it only arranges the frame.
// The interactive pieces (Header, CartDrawer, Toast) are client components.
//
// Fonts: the system font stack (see globals.css), so nothing is downloaded.

import CartDrawer from "../components/CartDrawer";
import Header from "../components/Header";
import Toast from "../components/Toast";
import "./globals.css";

export const metadata = {
  title: { default: "ShopLite Hardware", template: "%s · ShopLite Hardware" },
  description: "Browse tools, hardware, and supplies at ShopLite.",
};

export const viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>
        {/* First focusable element: keyboard users can jump past the header. */}
        <a className="skip-link" href="#main">Skip to content</a>
        <Header />
        <main id="main" className="container" tabIndex={-1}>
          {children}
        </main>
        <footer className="site-footer">
          <div className="container">
            <strong>SHOP<span className="brand-gold">LITE</span></strong>
            <p>
              Demo store with simulated shoppers. Sign-in is a demo, not real authentication;
              nothing is actually charged or shipped.
            </p>
          </div>
        </footer>
        {/* Mounted once for the whole site: the quick-view cart (opened from
            the header) and the "Added to cart" message. */}
        <CartDrawer />
        <Toast />
      </body>
    </html>
  );
}
