"use client";
// app/account/page.js: the signed-in customer's order history, newest first.
// Paged through the URL (/account?page=2), like the catalog.
//
// Layout from docs/design-reference ("Your orders"): slim header and footer
// (components/SlimFrame.js), a gold banner, status filter buttons, and one
// card-style row per order with a coloured status badge.
//
// The filters narrow the orders on THIS page only (the API pages the full
// history 20 at a time and has no status filter), and the count says so.

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { ErrorNotice, Loading } from "../../components/Notice";
import Pagination from "../../components/Pagination";
import SlimFrame from "../../components/SlimFrame";
import { getCustomer, getOrders } from "../../lib/api";
import { formatDateTime, formatMoney, formatStatus } from "../../lib/format";

const FILTERS = [
  { value: "all", label: "All orders" },
  { value: "paid", label: "Paid" },
  { value: "delivered", label: "Delivered" },
];

// Badge colour per status: delivered/shipped green, paid gold (on its way),
// failed or cancelled red, pending neutral.
function badgeClass(status) {
  if (status === "delivered" || status === "shipped") return "orders-badge";
  if (status === "paid") return "orders-badge orders-badge-paid";
  if (status === "payment_failed" || status === "cancelled") return "orders-badge orders-badge-bad";
  return "orders-badge orders-badge-neutral";
}

function OrdersHero({ customer }) {
  return (
    <section className="orders-hero" aria-labelledby="orders-title">
      <h1 id="orders-title">Your orders</h1>
      {customer && <p>Order history for {customer.first_name} {customer.last_name}</p>}
    </section>
  );
}

function OrderHistory() {
  const searchParams = useSearchParams();
  const page = Math.max(1, Number.parseInt(searchParams.get("page") || "1", 10) || 1);

  const [customer, setCustomer] = useState(undefined); // undefined = not checked yet
  const [orders, setOrders] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState("all");

  useEffect(() => {
    const current = getCustomer();
    setCustomer(current);
    if (!current) return;
    let cancelled = false;
    setOrders(null);
    setError(null);
    getOrders(page)
      .then((data) => !cancelled && setOrders(data))
      .catch((err) => !cancelled && setError(err));
    return () => {
      cancelled = true;
    };
  }, [page]);

  if (customer === null) {
    return (
      <SlimFrame>
        <OrdersHero />
        <p className="orders-empty"><Link href="/signin">Sign in</Link> to see your order history.</p>
      </SlimFrame>
    );
  }
  if (error) return <SlimFrame><ErrorNotice error={error} title="We couldn’t load your orders" /></SlimFrame>;
  if (!orders) return <SlimFrame><Loading label="Loading your orders" /></SlimFrame>;

  const visible = orders.items.filter((o) => filter === "all" || o.status === filter);
  const multiPage = orders.total_pages > 1;

  return (
    <SlimFrame>
      <OrdersHero customer={customer} />

      {orders.total_items === 0 ? (
        <div className="orders-empty" data-testid="orders-empty">
          <h2>No orders yet</h2>
          <p>When you place an order, it shows up here. <Link href="/">Start shopping</Link>.</p>
        </div>
      ) : (
        <>
          <div className="orders-toolbar">
            <div className="orders-filters" role="group" aria-label="Filter by status">
              {FILTERS.map((f) => (
                <button key={f.value} type="button" aria-pressed={filter === f.value}
                        onClick={() => setFilter(f.value)} data-testid="orders-filter" data-filter={f.value}>
                  {f.label}
                </button>
              ))}
            </div>
            <p className="orders-count" role="status">
              {visible.length} {visible.length === 1 ? "order" : "orders"}
              {multiPage && ` on this page (of ${orders.total_items} in all)`}
            </p>
          </div>

          <div className="orders-list" aria-label="Order history">
            {visible.map((order) => (
              <article className="orders-row" key={order.order_id}
                       data-testid="order-row" data-order-id={order.order_id}>
                <p className="orders-number">#{order.order_id}</p>
                {/* UTC from the API, shown in the shopper's local time. */}
                <p className="orders-date">{formatDateTime(order.created_at)}</p>
                <p className="orders-badge-wrap">
                  <span className={badgeClass(order.status)} data-testid="order-status">
                    {formatStatus(order.status)}
                  </span>
                </p>
                <p className="orders-price" data-testid="order-total">{formatMoney(order.total)}</p>
                <Link className="orders-view" href={`/account/orders/${order.order_id}`}
                      aria-label={`View order #${order.order_id}`} data-testid="order-link">
                  View order
                </Link>
              </article>
            ))}
            {visible.length === 0 && (
              <p className="orders-empty">No {filter} orders on this page.</p>
            )}
          </div>
        </>
      )}

      <Pagination page={orders.page} totalPages={orders.total_pages} hrefFor={(n) => `/account?page=${n}`} />
    </SlimFrame>
  );
}

// useSearchParams needs a Suspense boundary (see app/page.js).
export default function AccountPage() {
  return (
    <Suspense fallback={<Loading />}>
      <OrderHistory />
    </Suspense>
  );
}
