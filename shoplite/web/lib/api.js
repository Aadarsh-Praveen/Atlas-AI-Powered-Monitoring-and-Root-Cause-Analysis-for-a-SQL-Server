// lib/api.js: the one place the storefront talks to the ShopLite API.
//
// Every page calls these functions instead of fetch() directly, so:
//   - the API address comes from one setting (NEXT_PUBLIC_API_URL),
//   - the X-Customer-Id / X-Admin-Token headers are added consistently,
//   - every failure becomes an ApiError with a plain-language message and the
//     API's request ID (shown to shoppers as a reference).
//
// It also remembers who is signed in. DEMO SIGN-IN, NOT REAL AUTHENTICATION:
// the customer ID sits in localStorage and anyone can change it.

// Next.js replaces this expression with the value at build time. It must be
// written out in full (process.env.NEXT_PUBLIC_API_URL) for that to work.
const API_URL = (process.env.NEXT_PUBLIC_API_URL || "").replace(/\/+$/, "");

const CUSTOMER_KEY = "shoplite.customer";      // localStorage: survives reloads
const ADMIN_TOKEN_KEY = "shoplite.adminToken"; // sessionStorage: this tab only

// Browser events other components listen for (the header, mainly).
export const CUSTOMER_EVENT = "shoplite:customer-changed";
export const CART_EVENT = "shoplite:cart-changed";

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(status, message, requestId = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;       // HTTP status, or 0 if the API couldn't be reached
    this.requestId = requestId; // X-Request-ID from the response, for support
  }
}

// FastAPI sends {"detail": "text"} for our errors, and {"detail": [ {...} ]}
// for its own 422 validation errors. Turn either into one readable sentence.
function messageFrom(body, status) {
  const detail = body && body.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  return `Something went wrong (HTTP ${status}). Please try again.`;
}

// ---------------------------------------------------------------------------
// Browser storage (guarded: pages also render once on the server, where
// window doesn't exist, and storage can be blocked in private browsing)
// ---------------------------------------------------------------------------

function readStorage(storage, key) {
  if (typeof window === "undefined") return null;
  try {
    return window[storage].getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(storage, key, value) {
  if (typeof window === "undefined") return;
  try {
    if (value === null) window[storage].removeItem(key);
    else window[storage].setItem(key, value);
  } catch {
    // Storage unavailable: sign-in simply won't be remembered.
  }
}

function announce(eventName, detail) {
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent(eventName, { detail }));
}

export function getCustomer() {
  const saved = readStorage("localStorage", CUSTOMER_KEY);
  try {
    return saved ? JSON.parse(saved) : null;
  } catch {
    return null;
  }
}

function setCustomer(customer) {
  writeStorage("localStorage", CUSTOMER_KEY, customer ? JSON.stringify(customer) : null);
  announce(CUSTOMER_EVENT, customer);
  announce(CART_EVENT, null); // a different customer means a different cart
}

export function signOut() {
  setCustomer(null);
}

export function getAdminToken() {
  return readStorage("sessionStorage", ADMIN_TOKEN_KEY);
}

export function adminSignOut() {
  writeStorage("sessionStorage", ADMIN_TOKEN_KEY, null);
}

// Tell the header the cart changed. Pass the new item count if known, or
// null to make it fetch the cart again.
function cartChanged(itemCount) {
  announce(CART_EVENT, itemCount);
}

// ---------------------------------------------------------------------------
// The request helper
// ---------------------------------------------------------------------------

// auth: "customer" adds X-Customer-Id, "admin" adds X-Admin-Token.
async function request(path, { method = "GET", params, body, auth, adminToken } = {}) {
  if (!API_URL) {
    throw new ApiError(0, "The storefront isn't configured: NEXT_PUBLIC_API_URL is not set.");
  }

  const url = new URL(API_URL + path);
  for (const [name, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(name, value);
  }

  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth === "customer") {
    const customer = getCustomer();
    if (!customer) throw new ApiError(401, "Please sign in first.");
    headers["X-Customer-Id"] = String(customer.customer_id);
  }
  if (auth === "admin") {
    const token = adminToken || getAdminToken();
    if (!token) throw new ApiError(401, "Admin sign-in required.");
    headers["X-Admin-Token"] = token;
  }

  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store", // stock and carts change constantly; never reuse old answers
    });
  } catch {
    // fetch() only throws when no response arrived at all: the API is down,
    // the network is offline, or the browser blocked the response (CORS).
    throw new ApiError(0, "Can't reach the store right now. Please check your connection and try again.");
  }

  const requestId = response.headers.get("X-Request-ID");
  let data = null;
  try {
    data = await response.json();
  } catch {
    // Some error responses have no JSON body; handled below.
  }

  if (!response.ok) {
    // The API no longer recognises this customer (for example after the
    // database was re-seeded): forget them so the header shows "Sign in".
    if (response.status === 401 && auth === "customer") setCustomer(null);
    throw new ApiError(response.status, messageFrom(data, response.status), requestId);
  }
  return data;
}

// ---------------------------------------------------------------------------
// Catalog
// ---------------------------------------------------------------------------

export const getCategories = () => request("/api/categories");

export const searchProducts = ({ q, category_id, sort, page, page_size } = {}) =>
  request("/api/products", { params: { q, category_id, sort, page, page_size } });

export const getProduct = (productId) => request(`/api/products/${encodeURIComponent(productId)}`);

// ---------------------------------------------------------------------------
// Account (demo)
// ---------------------------------------------------------------------------

export async function login(email) {
  const customer = await request("/api/login", { method: "POST", body: { email } });
  setCustomer(customer);
  return customer;
}

export async function signInAsDemoCustomer() {
  const customer = await request("/api/demo-account");
  setCustomer(customer);
  return customer;
}

// ---------------------------------------------------------------------------
// Cart (every change returns the updated cart)
// ---------------------------------------------------------------------------

export const getCart = () => request("/api/cart", { auth: "customer" });

async function changeCart(path, method, body) {
  const cart = await request(path, { method, body, auth: "customer" });
  cartChanged(cart.item_count);
  return cart;
}

export const addToCart = (productId, quantity = 1) =>
  changeCart("/api/cart/items", "POST", { product_id: productId, quantity });

export const updateCartItem = (productId, quantity) =>
  changeCart(`/api/cart/items/${encodeURIComponent(productId)}`, "PATCH", { quantity });

export const removeCartItem = (productId) =>
  changeCart(`/api/cart/items/${encodeURIComponent(productId)}`, "DELETE");

// ---------------------------------------------------------------------------
// Checkout and orders
// ---------------------------------------------------------------------------

export async function placeOrder({ payment_method, shipping }) {
  const result = await request("/api/checkout", {
    method: "POST",
    body: { payment_method, shipping },
    auth: "customer",
  });
  cartChanged(0); // the cart became an order
  return result;
}

export const getOrders = (page = 1) => request("/api/orders", { params: { page }, auth: "customer" });

export const getOrder = (orderId) => request(`/api/orders/${encodeURIComponent(orderId)}`, { auth: "customer" });

export async function reorder(orderId) {
  const result = await request(`/api/orders/${encodeURIComponent(orderId)}/reorder`, {
    method: "POST",
    auth: "customer",
  });
  cartChanged(result.cart.item_count);
  return result;
}

// ---------------------------------------------------------------------------
// Admin (demo token)
// ---------------------------------------------------------------------------

// Check a token by making a tiny admin request with it; keep it only if the
// API accepts it.
export async function adminSignIn(token) {
  await request("/api/admin/inventory", { params: { page_size: 1 }, auth: "admin", adminToken: token });
  writeStorage("sessionStorage", ADMIN_TOKEN_KEY, token);
}

export const getInventory = ({ low_stock_only, page } = {}) =>
  request("/api/admin/inventory", { params: { low_stock_only, page }, auth: "admin" });

export const getSalesReport = ({ start_date, end_date, group_by } = {}) =>
  request("/api/admin/reports/sales", { params: { start_date, end_date, group_by }, auth: "admin" });

export const getTopProducts = ({ start_date, end_date, top } = {}) =>
  request("/api/admin/reports/top-products", { params: { start_date, end_date, top }, auth: "admin" });
