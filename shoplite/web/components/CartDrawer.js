"use client";
// components/CartDrawer.js: the quick-view cart that slides in from the right.
// Mounted once in app/layout.js; opened from anywhere with openCartDrawer().
//
// Built on the native <dialog> element opened with showModal(), which gives
// the accessibility behavior for free: focus moves into the drawer and is
// trapped there, Esc closes it, the page behind is inert, and a backdrop
// covers it. We add: closing on a backdrop click, and returning focus to the
// control that opened it.
//
// The cart is the API's. Every change redraws from the cart the API returns;
// line totals, subtotal, shipping, and the estimated total are the API's
// amounts, shown as-is. The /cart page still exists for the full view.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { CART_EVENT, getCart, getCustomer, removeCartItem, updateCartItem } from "../lib/api";
import { formatMoney, formatShipping } from "../lib/format";
import { ErrorNotice, Loading } from "./Notice";
import ProductPhoto from "./ProductPhoto";

const OPEN_EVENT = "shoplite:open-cart-drawer";
const MAX_PER_ITEM = 99; // the API's limit per product per cart

// Call from anywhere (the header's Cart control) to open the drawer.
export function openCartDrawer() {
  window.dispatchEvent(new Event(OPEN_EVENT));
}

export default function CartDrawer() {
  const dialogRef = useRef(null);
  const openerRef = useRef(null);   // element to give focus back to on close

  const [signedIn, setSignedIn] = useState(false);
  const [cart, setCart] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [changeError, setChangeError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const customer = getCustomer();
    setSignedIn(Boolean(customer));
    setLoadError(null);
    if (!customer) return;
    setCart(null);
    try {
      setCart(await getCart());
    } catch (err) {
      setLoadError(err);
    }
  }, []);

  useEffect(() => {
    const dialog = dialogRef.current;

    function open() {
      if (dialog.open) return;
      openerRef.current = document.activeElement;
      setChangeError(null);
      dialog.showModal();
      load(); // always show the current cart, not a stale copy
    }

    // "close" fires for every way of closing: our button, Esc, backdrop.
    function onClose() {
      const opener = openerRef.current;
      if (opener && document.contains(opener)) opener.focus();
    }

    // Something else changed the cart without telling us the new count
    // (e.g. a different customer signed in): reload if we're showing.
    function onCartChanged(event) {
      if (dialog.open && event.detail === null) load();
    }

    window.addEventListener(OPEN_EVENT, open);
    window.addEventListener(CART_EVENT, onCartChanged);
    dialog.addEventListener("close", onClose);
    return () => {
      window.removeEventListener(OPEN_EVENT, open);
      window.removeEventListener(CART_EVENT, onCartChanged);
      dialog.removeEventListener("close", onClose);
    };
  }, [load]);

  const close = () => dialogRef.current?.close();

  // A click on the backdrop lands on the <dialog> element itself (its
  // content fills the drawer), so that's how we detect "clicked outside".
  function onDialogClick(event) {
    if (event.target === dialogRef.current) close();
  }

  // Run a cart change, then show the cart the API sent back.
  async function change(action) {
    setBusy(true);
    setChangeError(null);
    try {
      setCart(await action());
    } catch (err) {
      setChangeError(err); // e.g. 409 "Only 3 of ... in stock"
    } finally {
      setBusy(false);
    }
  }

  function renderBody() {
    if (!signedIn) {
      return (
        <p>
          <Link href="/signin" onClick={close}>Sign in</Link> to see your cart and start shopping.
        </p>
      );
    }
    if (loadError) return <ErrorNotice error={loadError} title="We couldn’t load your cart" onRetry={load} />;
    if (!cart) return <Loading label="Loading your cart" />;
    if (cart.items.length === 0) {
      return (
        <div data-testid="cart-drawer-empty">
          <p>Your cart is empty.</p>
          <Link className="btn" href="/" onClick={close}>Browse the aisles</Link>
        </div>
      );
    }

    return (
      <>
        <ErrorNotice error={changeError} title="Couldn’t update your cart" />
        <ul className="drawer-lines">
          {cart.items.map((item) => {
            // Unit counts (integers) may be compared here; money never is.
            const atMax = item.quantity >= Math.min(MAX_PER_ITEM, item.stock_quantity);
            return (
              <li key={item.product_id} className="drawer-line line-with-thumb"
                  data-testid="cart-drawer-item" data-product-id={item.product_id}>
                <div className="line-thumb"><ProductPhoto product={item} size="thumb" /></div>
                <div className="drawer-line-body">
                  <Link href={`/products/${item.product_id}`} onClick={close}><strong>{item.name}</strong></Link>
                  <p>{formatMoney(item.price)} each · {formatMoney(item.line_total)}</p>
                  <div className="qty-stepper">
                    <button type="button" className="icon-button" disabled={busy}
                            aria-label={item.quantity === 1 ? `Remove ${item.name}` : `Remove one ${item.name}`}
                            onClick={() => change(() => item.quantity === 1
                              ? removeCartItem(item.product_id)
                              : updateCartItem(item.product_id, item.quantity - 1))}
                            data-testid="cart-drawer-decrease">
                      −
                    </button>
                    <output aria-label={`Quantity of ${item.name}`}>{item.quantity}</output>
                    <button type="button" className="icon-button" disabled={busy || atMax}
                            aria-label={`Add one ${item.name}`}
                            onClick={() => change(() => updateCartItem(item.product_id, item.quantity + 1))}
                            data-testid="cart-drawer-increase">
                      +
                    </button>
                    <button type="button" className="btn-link small" disabled={busy}
                            onClick={() => change(() => removeCartItem(item.product_id))}
                            data-testid="cart-drawer-remove">
                      Remove<span className="visually-hidden"> {item.name}</span>
                    </button>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>

        <div className="drawer-summary">
          <div className="summary-row">
            <span>Subtotal</span>
            <span data-testid="cart-drawer-subtotal">{formatMoney(cart.subtotal)}</span>
          </div>
          <div className="summary-row">
            <span>Shipping</span>
            <span>{formatShipping(cart.shipping_cost)}</span>
          </div>
          <div className="summary-row total">
            <span>Estimated total</span>
            <span data-testid="cart-drawer-estimated-total">{formatMoney(cart.estimated_total)}</span>
          </div>
          <div className="drawer-actions">
            <Link className="btn btn-primary" href="/checkout" onClick={close} data-testid="cart-drawer-checkout">
              Checkout
            </Link>
            <Link className="btn" href="/cart" onClick={close} data-testid="cart-drawer-view-cart">
              View full cart
            </Link>
          </div>
        </div>
      </>
    );
  }

  return (
    <dialog ref={dialogRef} className="cart-drawer" aria-labelledby="cart-drawer-title"
            onClick={onDialogClick} data-testid="cart-drawer">
      <div className="drawer">
        <div className="drawer-head">
          <h2 id="cart-drawer-title">Your cart</h2>
          <button type="button" className="icon-button" onClick={close} aria-label="Close cart"
                  data-testid="cart-drawer-close">
            ×
          </button>
        </div>
        {renderBody()}
      </div>
    </dialog>
  );
}
