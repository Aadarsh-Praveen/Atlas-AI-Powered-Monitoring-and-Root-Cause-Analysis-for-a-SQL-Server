"use client";
// app/cart/page.js: the open cart. Change quantities, remove items, see the
// subtotal, go to checkout.
//
// Every change sends one request, and the page redraws from the cart the API
// returns. Line totals, subtotal, shipping, and the estimated total are the
// API's numbers, shown as-is: no money arithmetic happens in the browser.

import Link from "next/link";
import { useEffect, useState } from "react";

import { ErrorNotice, Loading, Notice } from "../../components/Notice";
import { getCart, getCustomer, removeCartItem, updateCartItem } from "../../lib/api";
import ProductPhoto from "../../components/ProductPhoto";
import { formatMoney, formatShipping } from "../../lib/format";

const MAX_PER_ITEM = 99;

// Quantity choices for one line: 1 up to what's on the shelf (max 99), plus
// the current quantity if stock has since dropped below it, so the menu can
// still show what's in the cart.
function quantityChoices(item) {
  const top = Math.max(1, Math.min(MAX_PER_ITEM, item.stock_quantity));
  const choices = Array.from({ length: top }, (_, i) => i + 1);
  if (item.quantity > top) choices.push(item.quantity);
  return choices;
}

export default function CartPage() {
  const [signedIn, setSignedIn] = useState(null); // null until we've checked
  const [cart, setCart] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [changeError, setChangeError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const customer = getCustomer();
    setSignedIn(Boolean(customer));
    if (customer) getCart().then(setCart).catch(setLoadError);
  }, []);

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

  if (signedIn === false) {
    return (
      <div className="card">
        <h1>Your cart</h1>
        <p><Link href="/signin">Sign in</Link> to see your cart and start shopping.</p>
      </div>
    );
  }
  if (loadError) return <ErrorNotice error={loadError} title="We couldn’t load your cart" />;
  if (!cart) return <Loading label="Loading your cart" />;

  if (cart.items.length === 0) {
    return (
      <div className="card" data-testid="cart-empty">
        <h1>Your cart is empty</h1>
        <p>Nothing here yet. <Link href="/">Browse the aisles</Link> to find what you need.</p>
      </div>
    );
  }

  return (
    <>
      <div className="page-header">
        <h1>Your cart</h1>
        <span className="muted">{cart.item_count} {cart.item_count === 1 ? "item" : "items"}</span>
      </div>

      <ErrorNotice error={changeError} title="Couldn’t update your cart" />

      <div className="two-column">
        <ul className="stack" style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {cart.items.map((item) => {
            return (
            <li key={item.product_id} className="card" data-testid="cart-item" data-product-id={item.product_id}>
              <div className="line-with-thumb" style={{ marginBottom: "var(--space-3)" }}>
                {/* Item-type photo or placeholder, decorative (the name next to it says what it is). */}
                <div className="line-thumb"><ProductPhoto product={item} size="thumb" /></div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="summary-row" style={{ padding: 0 }}>
                    <Link href={`/products/${item.product_id}`} className="product-tile-link"
                          style={{ minHeight: 0, margin: 0 }}>
                      {item.name}
                    </Link>
                    <strong>{formatMoney(item.line_total)}</strong>
                  </div>
                  <p className="muted small" style={{ margin: "var(--space-1) 0 0" }}>
                    <span className="mono">{item.sku}</span> · {formatMoney(item.price)} each
                  </p>
                </div>
              </div>

              {/* Comparing unit counts (integers) is fine; it's money we never compute. */}
              {item.quantity > item.stock_quantity && (
                <Notice kind="error">
                  {item.stock_quantity === 0
                    ? "This item is now out of stock. Remove it to check out."
                    : `Only ${item.stock_quantity} left. Lower the quantity to check out.`}
                </Notice>
              )}

              <div className="toolbar" style={{ marginBottom: 0 }}>
                <div>
                  <label htmlFor={`qty-${item.product_id}`}>Quantity</label>
                  <select
                    id={`qty-${item.product_id}`}
                    className="select"
                    value={item.quantity}
                    disabled={busy}
                    onChange={(e) => change(() => updateCartItem(item.product_id, Number(e.target.value)))}
                    data-testid="cart-item-quantity"
                  >
                    {quantityChoices(item).map((n) => (
                      <option key={n} value={n}>{n}</option>
                    ))}
                  </select>
                </div>
                <button
                  type="button"
                  className="btn"
                  disabled={busy}
                  onClick={() => change(() => removeCartItem(item.product_id))}
                  data-testid="cart-item-remove"
                >
                  Remove<span className="visually-hidden"> {item.name}</span>
                </button>
              </div>
            </li>
            );
          })}
        </ul>

        <aside className="card stack" aria-label="Cart summary">
          <div className="summary-row">
            <span>Subtotal</span>
            <span data-testid="cart-subtotal">{formatMoney(cart.subtotal)}</span>
          </div>
          <div className="summary-row">
            <span>Shipping</span>
            <span data-testid="cart-shipping">{formatShipping(cart.shipping_cost)}</span>
          </div>
          <div className="summary-row total">
            <span>Estimated total</span>
            <span data-testid="cart-estimated-total">{formatMoney(cart.estimated_total)}</span>
          </div>
          <p className="muted small">The final total is confirmed when you place your order.</p>
          <Link className="btn btn-primary" href="/checkout" data-testid="checkout-button">
            Go to checkout
          </Link>
        </aside>
      </div>
    </>
  );
}
