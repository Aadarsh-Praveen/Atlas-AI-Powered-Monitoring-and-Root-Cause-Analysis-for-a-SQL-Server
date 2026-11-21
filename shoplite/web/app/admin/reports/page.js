"use client";
// app/admin/reports/page.js: the sales report (by day, product, or state)
// and top products, over one date range.
//
// Both reports are stored procedures in SQL Server (005_report_procedures.sql)
// and deliberately heavy. The procedures own the input rules (date order,
// grouping, top 1-100). This page does NOT pre-check them: a bad range comes
// back as the API's 400 with the procedure's own message, shown as-is.
//
// No totals row: adding up revenue here would be money arithmetic in
// JavaScript. Every amount shown is exactly what the procedure returned.

import Link from "next/link";
import { useEffect, useState } from "react";

import { ErrorNotice, Loading } from "../../../components/Notice";
import { adminSignOut, getAdminToken, getSalesReport, getTopProducts } from "../../../lib/api";
import { formatCalendarDate, formatMoney, todayUtc } from "../../../lib/format";

const GROUPINGS = [
  { value: "day", label: "Day" },
  { value: "product", label: "Product" },
  { value: "state", label: "State" },
];

// "YYYY-MM-DD" for (date - days), in UTC. Calendar arithmetic on dates is
// fine; it's money we never compute.
function daysBefore(yyyyMmDd, days) {
  const [y, m, d] = yyyyMmDd.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d - days)).toISOString().slice(0, 10);
}

// The first column of a sales row depends on the grouping.
function groupLabel(row, groupBy) {
  if (groupBy === "day") return formatCalendarDate(row.sales_date);   // a UTC calendar day: not shifted
  if (groupBy === "product") return `${row.sku} · ${row.product_name}`;
  return row.state;
}

export default function ReportsPage() {
  const [hasToken, setHasToken] = useState(null);
  const [form, setForm] = useState(() => {
    const end = todayUtc();
    return { start_date: daysBefore(end, 29), end_date: end, group_by: "day", top: "10" };
  });
  const [sales, setSales] = useState(null);
  const [top, setTop] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);

  async function run() {
    setRunning(true);
    setError(null);
    try {
      // Both reports at once; each is one stored procedure call.
      const [salesReport, topReport] = await Promise.all([
        getSalesReport({ start_date: form.start_date, end_date: form.end_date, group_by: form.group_by }),
        getTopProducts({ start_date: form.start_date, end_date: form.end_date, top: form.top }),
      ]);
      setSales(salesReport);
      setTop(topReport);
    } catch (err) {
      if (err.status === 401) {
        adminSignOut();
        setHasToken(false);
      } else {
        setError(err); // e.g. 400 "Invalid date range: end_date must be on or after start_date."
      }
    } finally {
      setRunning(false);
    }
  }

  useEffect(() => {
    const signedIn = Boolean(getAdminToken());
    setHasToken(signedIn);
    // Run the default (last 30 days) once on arrival. The empty dependency
    // list is deliberate: later runs happen only when the admin clicks
    // "Run reports", not every time a form field changes.
    if (signedIn) run();
  }, []);

  const setField = (field) => (e) => setForm((f) => ({ ...f, [field]: e.target.value }));

  if (hasToken === false) {
    return (
      <div className="card">
        <h1>Reports</h1>
        <p><Link href="/admin">Sign in to the admin area</Link> to run reports.</p>
      </div>
    );
  }

  return (
    <>
      <p className="small"><Link href="/admin">← Store admin</Link></p>
      <h1>Reports</h1>

      <form className="card toolbar" onSubmit={(e) => { e.preventDefault(); run(); }}>
        <div>
          <label htmlFor="start">From</label>
          <input id="start" className="input" type="date" value={form.start_date}
                 onChange={setField("start_date")} data-testid="report-start-date" />
        </div>
        <div>
          <label htmlFor="end">To</label>
          <input id="end" className="input" type="date" value={form.end_date}
                 onChange={setField("end_date")} data-testid="report-end-date" />
        </div>
        <div>
          <label htmlFor="group">Sales by</label>
          <select id="group" className="select" value={form.group_by}
                  onChange={setField("group_by")} data-testid="report-group-by">
            {GROUPINGS.map((g) => <option key={g.value} value={g.value}>{g.label}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor="top">Top products</label>
          <input id="top" className="input input-narrow" type="number" inputMode="numeric"
                 value={form.top} onChange={setField("top")} data-testid="top-n-input" />
        </div>
        <button type="submit" className="btn btn-primary" disabled={running} data-testid="report-run">
          {running ? "Running…" : "Run reports"}
        </button>
      </form>
      <p className="muted small">Dates are UTC calendar days. Long ranges can take a few seconds.</p>

      {error && <ErrorNotice error={error} title="The report couldn’t run" />}
      {running && <Loading label="Running reports" />}

      {sales && !running && (
        <section className="stack" style={{ marginTop: "var(--space-6)" }}>
          <h2>
            Sales by {sales.group_by}, {formatCalendarDate(sales.start_date)} – {formatCalendarDate(sales.end_date)}
          </h2>
          {sales.rows.length === 0 ? (
            <p className="muted">No sales in this range.</p>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <caption className="visually-hidden">Sales report</caption>
                <thead>
                  <tr>
                    <th scope="col">{GROUPINGS.find((g) => g.value === sales.group_by)?.label}</th>
                    <th scope="col" className="num">Orders</th>
                    <th scope="col" className="num">Units</th>
                    <th scope="col" className="num">Revenue</th>
                  </tr>
                </thead>
                <tbody>
                  {sales.rows.map((row, index) => (
                    <tr key={index} data-testid="report-row">
                      <td>{groupLabel(row, sales.group_by)}</td>
                      <td className="num">{row.order_count.toLocaleString()}</td>
                      <td className="num">{row.units_sold.toLocaleString()}</td>
                      <td className="num">{formatMoney(row.revenue)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {top && !running && (
        <section className="stack" style={{ marginTop: "var(--space-6)" }}>
          <h2>Top {top.top} products by units</h2>
          {top.rows.length === 0 ? (
            <p className="muted">No sales in this range.</p>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <caption className="visually-hidden">Top products</caption>
                <thead>
                  <tr>
                    <th scope="col" className="num">#</th>
                    <th scope="col">Product</th>
                    <th scope="col">Aisle</th>
                    <th scope="col" className="num">Units</th>
                    <th scope="col" className="num">Orders</th>
                    <th scope="col" className="num">Revenue</th>
                  </tr>
                </thead>
                <tbody>
                  {top.rows.map((row, index) => (
                    <tr key={row.product_id} data-testid="top-products-row">
                      <td className="num">{index + 1}</td>
                      <td>
                        <Link href={`/products/${row.product_id}`}>{row.product_name}</Link>
                        <div className="muted small mono">{row.sku}</div>
                      </td>
                      <td>{row.category_name}</td>
                      <td className="num">{row.units_sold.toLocaleString()}</td>
                      <td className="num">{row.order_count.toLocaleString()}</td>
                      <td className="num">{formatMoney(row.revenue)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </>
  );
}
