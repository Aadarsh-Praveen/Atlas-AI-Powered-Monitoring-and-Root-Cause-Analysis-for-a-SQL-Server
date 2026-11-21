"use client";
// components/Header.js: brand, search, account links, and the Cart control.
//
// A client component because it reacts to the shopper: who is signed in and
// how many items are in the cart. It listens for the events lib/api.js sends
// (CUSTOMER_EVENT, CART_EVENT), so it updates the moment either changes.
//
// The Cart control is a real link to /cart (data-testid="nav-cart-link").
// A plain click opens the quick-view drawer instead; Cmd/Ctrl/Shift/middle
// click, or a browser where JavaScript didn't load, still goes to /cart.

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { CART_EVENT, CUSTOMER_EVENT, getCart, getCustomer, signOut } from "../lib/api";
import { openCartDrawer } from "./CartDrawer";

// The round profile button at the far right: the customer's first initial.
// A click opens a small menu with their name and Sign out. Esc, a click
// outside, or moving focus out of it closes the menu.
function ProfileMenu({ customer, onSignOut }) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef(null);
  const buttonRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (e) => { if (!boxRef.current?.contains(e.target)) setOpen(false); };
    const onKey = (e) => {
      if (e.key === "Escape") { setOpen(false); buttonRef.current?.focus(); }
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const initial = (customer.first_name || customer.email || "?").trim().charAt(0).toUpperCase();
  const fullName = `${customer.first_name} ${customer.last_name}`.trim();

  return (
    <div className="profile" ref={boxRef}
         onBlur={(e) => { if (!boxRef.current?.contains(e.relatedTarget)) setOpen(false); }}>
      <button type="button" ref={buttonRef} className="profile-button"
              aria-expanded={open} aria-controls="profile-menu"
              aria-label={`Account menu for ${fullName}`}
              onClick={() => setOpen((v) => !v)} data-testid="profile-button">
        {initial}
      </button>
      {open && (
        <div className="profile-menu" id="profile-menu">
          <p className="profile-name">{fullName}</p>
          <p className="profile-email">{customer.email}</p>
          <button type="button" className="profile-signout" onClick={onSignOut} data-testid="signout-button">
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

export default function Header() {
  const router = useRouter();
  const pathname = usePathname();

  // Start empty and fill in after mounting: the server can't see the
  // browser's storage, and the first render must match on both sides.
  const [customer, setCustomer] = useState(null);
  const [cartCount, setCartCount] = useState(0);

  // The count comes from the API's cart (item_count); never computed here.
  const refreshCartCount = useCallback(async () => {
    if (!getCustomer()) {
      setCartCount(0);
      return;
    }
    try {
      const cart = await getCart();
      setCartCount(cart.item_count);
    } catch {
      setCartCount(0); // the badge is a convenience; don't break the header
    }
  }, []);

  useEffect(() => {
    setCustomer(getCustomer());
    refreshCartCount();

    const onCustomer = (event) => setCustomer(event.detail);
    // detail is the new count when known, or null meaning "fetch it".
    const onCart = (event) =>
      typeof event.detail === "number" ? setCartCount(event.detail) : refreshCartCount();

    window.addEventListener(CUSTOMER_EVENT, onCustomer);
    window.addEventListener(CART_EVENT, onCart);
    return () => {
      window.removeEventListener(CUSTOMER_EVENT, onCustomer);
      window.removeEventListener(CART_EVENT, onCart);
    };
  }, [refreshCartCount]);

  function search(event) {
    event.preventDefault();
    const q = new FormData(event.currentTarget).get("q").toString().trim();
    // A new search starts at page 1 in all aisles.
    router.push(q ? `/?q=${encodeURIComponent(q)}` : "/");
  }

  function handleSignOut() {
    signOut();
    router.push("/");
  }

  // Plain left click: open the quick view. Anything else (new tab, new
  // window) keeps the link's normal behavior and goes to /cart.
  function openQuickView(event) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    openCartDrawer();
  }

  // aria-current tells screen readers (and the CSS) which page you're on.
  const current = (href) => (pathname === href ? "page" : undefined);

  return (
    <header className="site-header">
      <div className="container mast">
        <Link href="/" className="brand" aria-label="ShopLite Hardware Supply, home">
          <span className="brand-mark" aria-hidden="true">S</span>
          <span aria-hidden="true">
            SHOP<span className="brand-gold">LITE</span>
            <small className="brand-tagline">HARDWARE SUPPLY</small>
          </span>
        </Link>

        <form className="header-search" role="search" onSubmit={search}>
          <label htmlFor="header-search" className="visually-hidden">Search the catalog</label>
          <span className="search-icon" aria-hidden="true" />
          <input
            id="header-search"
            name="q"
            type="search"
            placeholder="Search products, categories or SKU"
            maxLength={100}
            data-testid="header-search-input"
          />
          <button type="submit" data-testid="header-search-submit">Search</button>
        </form>

        <nav className="header-nav" aria-label="Main">
          {customer ? (
            <Link href="/account" aria-current={current("/account")} data-testid="nav-account-link">Orders</Link>
          ) : (
            <Link href="/signin" aria-current={current("/signin")} data-testid="nav-signin-link">Sign in</Link>
          )}
          <Link href="/admin" aria-current={current("/admin")} data-testid="nav-admin-link">Admin</Link>
          <Link
            href="/cart"
            className="cart-trigger"
            onClick={openQuickView}
            aria-haspopup="dialog"
            data-testid="nav-cart-link"
          >
            Cart
            {/* cart-count holds only the number, for tests; the hidden text
                makes screen readers say "Cart 3 items". */}
            <span className="cart-badge" data-testid="cart-count">{cartCount}</span>
            <span className="visually-hidden"> items</span>
          </Link>
          {customer && <ProfileMenu customer={customer} onSignOut={handleSignOut} />}
        </nav>
      </div>
    </header>
  );
}
