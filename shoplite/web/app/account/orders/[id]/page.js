"use client";
// app/account/orders/[id]/page.js: one order with its items and payments,
// and Reorder (put the items back in the cart).
//
// The API only returns the signed-in customer's own orders; anyone else's
// order is a 404, shown here as "not found".
//
// Layout from docs/design-reference ("Order details"): a gold banner with the
// order number, dates, and a status badge; item and payment panels; and an
// order summary with the shipping address and Reorder. The site header stays,
// since its Account and Cart links already do what the design's header does.

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { ErrorNotice, Loading } from "../../../../components/Notice";
import ProductPhoto from "../../../../components/ProductPhoto";
import { getCustomer, getOrder, reorder } from "../../../../lib/api";
import { formatDateTime, formatMoney, formatShipping, formatStatus } from "../../../../lib/format";

// Badge colour for an order or payment status: green when it went through,
// red when it didn't, neutral while it's still pending.
const GOOD = ["paid", "shipped", "delivered", "approved"];
const BAD = ["payment_failed", "cancelled", "declined"];
function badgeTone(status) {
  if (GOOD.includes(status)) return "good";
  if (BAD.includes(status)) return "bad";
  return "neutral";
}

function StatusBadge({ status, testId }) {
  const tone = badgeTone(status);
  return (
    <span className={`order-badge order-badge-${tone}`}>
      {tone === "good" && <span aria-hidden="true">✓</span>}
      {/* The test hook holds just the status text ("Paid"), not the tick. */}
      <span data-testid={testId}>{formatStatus(status)}</span>
    </span>
  );
}

export default function OrderPage() {
  const { id } = useParams();

  const [signedIn, setSignedIn] = useState(null);
  const [order, setOrder] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [reordering, setReordering] = useState(false);
  const [reorderResult, setReorderResult] = useState(null); // { result } or { error }

  useEffect(() => {
    const customer = getCustomer();
    setSignedIn(Boolean(customer));
    if (!customer) return;
    let cancelled = false;
    getOrder(id)
      .then((data) => !cancelled && setOrder(data))
      .catch((err) => !cancelled && setLoadError(err));
    return () => {
      cancelled = true;
    };
  }, [id]);

  async function handleReorder() {
    setReordering(true);
    setReorderResult(null);
    try {
      setReorderResult({ result: await reorder(order.order_id) });
    } catch (err) {
      setReorderResult({ error: err });
    } finally {
      setReordering(false);
    }
  }

  if (signedIn === false) {
    return (
      <div className="card">
        <h1>Order</h1>
        <p><Link href="/signin">Sign in</Link> to see this order.</p>
      </div>
    );
  }
  if (loadError?.status === 404) {
    return (
      <div className="card">
        <h1>Order not found</h1>
        <p>We couldn’t find order #{id} in your account. <Link href="/account">See your orders</Link>.</p>
      </div>
    );
  }
  if (loadError) return <ErrorNotice error={loadError} title="We couldn’t load this order" />;
  if (!order) return <Loading label="Loading your order" />;

  const result = reorderResult?.result;

  return (
    <div className="order-page">
      <Link className="order-back" href="/account">All orders</Link>

      <section className="order-hero" aria-labelledby="order-title">
        <div>
          <h1 id="order-title">Order #{order.order_id}</h1>
          <dl>
            <div><dt>Placed</dt><dd>{formatDateTime(order.created_at)}</dd></div>
            <div><dt>Last updated</dt><dd>{formatDateTime(order.updated_at)}</dd></div>
          </dl>
        </div>
        <StatusBadge status={order.status} testId="order-status" />
      </section>

      <div className="order-layout">
        <div className="order-stack">
          <section className="order-panel" aria-labelledby="items-title">
            <h2 className="order-panel-title" id="items-title">Order items</h2>
            {order.items.map((item) => (
              <article className="order-item" key={item.product_id}>
                {/* Item-type photo or placeholder, decorative (the name says what it is). */}
                <div className="order-item-photo"><ProductPhoto product={item} size="thumb" /></div>
                <div>
                  <p className="order-item-name">
                    <Link href={`/products/${item.product_id}`}>{item.name}</Link>
                  </p>
                  {/* The price when ordered, not today's price. */}
                  <p className="order-meta">
                    <span className="mono">{item.sku}</span> · Qty {item.quantity} · {formatMoney(item.unit_price)} each
                  </p>
                </div>
                <p className="order-amount">{formatMoney(item.line_total)}</p>
              </article>
            ))}
          </section>

          <section className="order-panel" aria-labelledby="payment-title">
            <h2 className="order-panel-title" id="payment-title">Payment</h2>
            {order.payments.length === 0 ? (
              <p className="order-meta">No payment recorded yet.</p>
            ) : (
              order.payments.map((p, index) => (
                <div className="order-pay" key={index}>
                  <div>
                    <p><strong>{formatMoney(p.amount)}</strong> by {formatStatus(p.method).toLowerCase()}</p>
                    <p className="order-meta">{formatDateTime(p.created_at)}</p>
                  </div>
                  <StatusBadge status={p.status} />
                </div>
              ))
            )}
          </section>
        </div>

        <aside className="order-panel order-summary" aria-labelledby="summary-title">
          <h2 className="order-panel-title" id="summary-title">Order summary</h2>
          <dl>
            <div><dt>Subtotal</dt><dd>{formatMoney(order.subtotal)}</dd></div>
            <div><dt>Shipping</dt><dd>{formatShipping(order.shipping_cost)}</dd></div>
            <div className="order-total"><dt>Total</dt><dd data-testid="order-total">{formatMoney(order.total)}</dd></div>
          </dl>

          <div className="order-address">
            <h2>Ship to</h2>
            <address>
              {order.shipping.name}<br />
              {order.shipping.address_line1}<br />
              {order.shipping.city}, {order.shipping.state} {order.shipping.postal_code}
            </address>
          </div>

          <button type="button" className="order-reorder" onClick={handleReorder}
                  disabled={reordering} data-testid="reorder-button">
            {reordering ? "Adding to cart…" : "Reorder"}
          </button>

          <div data-testid="reorder-result" aria-live="polite" className="order-feedback">
            {result && (
              <p>
                {result.added > 0
                  ? <>Added {result.added} {result.added === 1 ? "item" : "items"} to your cart. <Link href="/cart">View cart</Link></>
                  : "None of these items could be added."}
              </p>
            )}
            {result?.skipped.length > 0 && (
              <ul>
                {result.skipped.map((s) => (
                  <li key={s.product_id}>{s.name}: {s.reason}</li>
                ))}
              </ul>
            )}
            {reorderResult?.error && <ErrorNotice error={reorderResult.error} title="Couldn’t reorder" />}
          </div>
        </aside>
      </div>
    </div>
  );
}
