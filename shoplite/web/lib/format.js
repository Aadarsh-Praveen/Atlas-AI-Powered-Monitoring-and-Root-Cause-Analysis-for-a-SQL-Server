// lib/format.js: how money, times, and statuses are shown.
//
// MONEY IS DISPLAY-ONLY. The API sends exact decimal strings ("1234.5",
// "24.99"). These functions only rearrange the characters; they never turn
// money into a JavaScript number, because JavaScript numbers are binary
// floating point (0.1 + 0.2 === 0.30000000000000004). Every total the
// storefront shows was calculated by the API.

// "1234.5" -> "$1,234.50", "-3" -> "-$3.00", "0.00" -> "$0.00"
export function formatMoney(value) {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value).trim();
  const match = /^(-)?(\d+)(?:\.(\d*))?$/.exec(text);
  // Not a plain decimal string: show it unchanged rather than guess.
  if (!match) return text;

  const [, sign = "", whole, fraction = ""] = match;
  // Amounts have at most two decimals (DECIMAL(..., 2) in SQL Server).
  // Pad short fractions ("5" -> "50"); never round.
  const cents = (fraction + "00").slice(0, 2);
  // Insert a comma before every group of three digits from the right.
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${sign}$${grouped}.${cents}`;
}

// Shipping from the API: "Free" when the amount is zero, else the amount.
// Zero is recognised from the characters ("0.00", "0"), not by converting
// to a number, so this stays within the display-only rule.
export function formatShipping(value) {
  return /^0+(\.0+)?$/.test(String(value ?? "").trim()) ? "Free" : formatMoney(value);
}

// UTC timestamp from the API ("2026-11-21T14:03:22.123Z") -> the shopper's
// local date and time, in their own language format. The "Z" tells the
// browser the value is UTC, so it converts to the local time zone.
export function formatDateTime(iso) {
  if (!iso) return "—";
  const moment = new Date(iso);
  if (Number.isNaN(moment.getTime())) return iso;
  return moment.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// A calendar day from the API ("2026-11-21", e.g. a sales report row) -> a
// readable date WITHOUT time-zone conversion. new Date("2026-11-21") would
// mean midnight UTC, which is still the 20th in the Americas, so build the
// date from its parts in local time instead.
export function formatCalendarDate(yyyyMmDd) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(yyyyMmDd || "");
  if (!match) return yyyyMmDd || "—";
  const [, year, month, day] = match;
  return new Date(Number(year), Number(month) - 1, Number(day))
    .toLocaleDateString(undefined, { dateStyle: "medium" });
}

// Today's date in UTC as "YYYY-MM-DD" (report date pickers default to it,
// matching the API, whose report dates are UTC days).
export function todayUtc() {
  return new Date().toISOString().slice(0, 10);
}

// "payment_failed" -> "Payment failed"
export function formatStatus(status) {
  if (!status) return "";
  const words = status.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
