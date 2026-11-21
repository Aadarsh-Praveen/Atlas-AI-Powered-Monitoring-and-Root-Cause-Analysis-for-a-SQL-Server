"use client";
// app/admin/inventory/page.js: stock for every product, optionally only the
// items at or below their reorder level (most urgent first).
// Filter and page live in the URL: /admin/inventory?low=1&page=2

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { ErrorNotice, Loading } from "../../../components/Notice";
import Pagination from "../../../components/Pagination";
import { adminSignOut, getAdminToken, getInventory } from "../../../lib/api";
import { formatDateTime } from "../../../lib/format";

function inventoryHref(lowOnly, page) {
  const params = new URLSearchParams();
  if (lowOnly) params.set("low", "1");
  if (page > 1) params.set("page", String(page));
  const query = params.toString();
  return query ? `/admin/inventory?${query}` : "/admin/inventory";
}

function stockStatus(item) {
  if (item.quantity_on_hand === 0) return { text: "Out of stock", className: "stock-out" };
  if (item.low_stock) return { text: "Low", className: "stock-low" };
  return { text: "OK", className: "stock-in" };
}

function Inventory() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const lowOnly = searchParams.get("low") === "1";
  const page = Math.max(1, Number.parseInt(searchParams.get("page") || "1", 10) || 1);

  const [hasToken, setHasToken] = useState(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!getAdminToken()) {
      setHasToken(false);
      return;
    }
    setHasToken(true);
    let cancelled = false;
    setData(null);
    setError(null);
    getInventory({ low_stock_only: lowOnly, page })
      .then((result) => !cancelled && setData(result))
      .catch((err) => {
        if (cancelled) return;
        if (err.status === 401) {
          adminSignOut(); // the token was changed or is wrong: sign in again
          setHasToken(false);
        } else {
          setError(err);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [lowOnly, page]);

  if (hasToken === false) {
    return (
      <div className="card">
        <h1>Inventory</h1>
        <p><Link href="/admin">Sign in to the admin area</Link> to see inventory.</p>
      </div>
    );
  }

  return (
    <>
      <p className="small"><Link href="/admin">← Store admin</Link></p>
      <div className="page-header">
        <h1>Inventory</h1>
        <label className="small" style={{ display: "flex", alignItems: "center", gap: "var(--space-2)" }}>
          <input
            type="checkbox"
            checked={lowOnly}
            onChange={(e) => router.push(inventoryHref(e.target.checked, 1))}
            data-testid="low-stock-toggle"
          />
          Low stock only
        </label>
      </div>

      {error && <ErrorNotice error={error} title="We couldn’t load inventory" />}
      {!error && !data && <Loading label="Counting the stockroom" />}

      {data && (
        <>
          <p className="muted">
            {data.total_items.toLocaleString()} {lowOnly ? "products at or below their reorder level" : "products"}
          </p>
          <div className="table-wrap">
            <table className="table">
              <caption className="visually-hidden">Inventory</caption>
              <thead>
                <tr>
                  <th scope="col">SKU</th>
                  <th scope="col">Product</th>
                  <th scope="col">Aisle</th>
                  <th scope="col" className="num">On hand</th>
                  <th scope="col" className="num">Reorder at</th>
                  <th scope="col">Status</th>
                  <th scope="col">Updated</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => {
                  const status = stockStatus(item);
                  return (
                    <tr key={item.product_id} data-testid="inventory-row" data-product-id={item.product_id}>
                      <td className="mono">{item.sku}</td>
                      <td><Link href={`/products/${item.product_id}`}>{item.name}</Link></td>
                      <td>{item.category_name}</td>
                      <td className="num">{item.quantity_on_hand}</td>
                      <td className="num">{item.reorder_level}</td>
                      <td className={status.className}>{status.text}</td>
                      <td className="small">{formatDateTime(item.updated_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pagination page={data.page} totalPages={data.total_pages} hrefFor={(n) => inventoryHref(lowOnly, n)} />
        </>
      )}
    </>
  );
}

export default function InventoryPage() {
  return (
    <Suspense fallback={<Loading />}>
      <Inventory />
    </Suspense>
  );
}
