# shoplite/web

The **ShopLite storefront**: Next.js (App Router) in JavaScript. Pages that show data call the
ShopLite API **directly from the browser**, so every click is a real request from browser to API to
SQL Server: the traffic Atlas will eventually watch. Built in Phase 1; deployed to Vercel in Phase 2.

> **Demo sign-in, not real authentication.** Shoppers "sign in" with the email of a seeded customer
> (or as a random demo customer); the storefront remembers the customer ID in the browser and sends
> it as `X-Customer-Id`. The admin token is typed into the admin page and kept only for that browser
> tab. See `shoplite/api/README.md`.

## Running locally

Prerequisites: Node.js 20, and the ShopLite API running on <http://localhost:8000>
(`shoplite/api/README.md`).

```bash
# once, from the repo root: the storefront's photos
mkdir -p shoplite/web/public/images
cp docs/design-reference/ShopLite/*.webp shoplite/web/public/images/

cd shoplite/web
cp .env.example .env.local     # NEXT_PUBLIC_API_URL=http://localhost:8000
npm install
npm run dev
```

Open <http://localhost:3000>. The API only accepts requests from the origins in its
`ALLOWED_ORIGINS` setting, which includes `http://localhost:3000`.

In development, React's strict mode runs effects twice, so you'll see some API calls twice in the
browser's Network tab. Production builds call once.

## Pages

| Route | Page |
|---|---|
| `/` | Catalog: aisles, search, sort, pagination (`?q=`, `?aisle=`, `?sort=`, `?page=`) |
| `/products/[id]` | Product: details, shelf tag with stock, quantity, add to cart, often bought together |
| `/signin` | Sign in by email, or as a random demo customer |
| `/cart` | Cart: change quantities, remove items, subtotal |
| `/checkout` | Shipping (prefilled), payment method, place order, result |
| `/account` | Order history |
| `/account/orders/[id]` | One order: items, payments, reorder |
| `/admin` | Admin sign-in with the admin token |
| `/admin/inventory` | Inventory with a low-stock filter |
| `/admin/reports` | Sales report (date range, grouping) and top products |

## Look and feel

The visual design follows `docs/design-reference/ShopLite` (used for its look only; every product,
price, and stock level comes from the API): dark header with the gold "S" mark, a 3D "stream" of
product photos behind the home page headline (`components/StreamHero.js`, design from
`docs/design-reference/stream-hero.html`; paused for reduced motion, a still collage on narrow
phones), aisle pills, photo cards that fade in and lift on hover or keyboard focus, and a cart
drawer for a quick view. All animation is switched off when the shopper's system asks for reduced
motion. Fonts are the system font stack (nothing downloaded).

- **One photo per item type, never reused.** Each photo in `public/images` belongs to exactly one
  item type, checked against the image itself: Cordless Drill, Claw Hammer, Drywall Screws,
  Interior Paint, Extension Cord, Pruning Shears, Storage Bin, Finishing Nails, Hex Bolts,
  Lag Screws, Washers (`product-4`, an elbow fitting, matches nothing we sell). `lib/images.js`
  finds the item type as whole words in the product name. Products of other item types show a
  placeholder card with their item type and aisle (`components/ProductPhoto.js`), so no card is
  blank. Photos show the item type, not the exact brand or size, so they're decorative (`alt=""`)
  and the product page says "Photo shows a typical …".
- **The Cart control** in the header is a link to `/cart`. A plain click opens the quick-view drawer
  (native `<dialog>`: focus trapped inside, Esc or a backdrop click closes it, focus returns to the
  Cart control). Cmd/Ctrl/Shift/middle-click still opens `/cart`. The drawer links to `/cart` and
  `/checkout`.
- **"+ Add"** on a card adds one unit through the API; the toast shows the API's new item count, or
  its error message (e.g. not enough stock).

## Data rules

- **Money is display-only.** The API sends amounts as exact strings (`"24.99"`). The storefront
  formats them (`lib/format.js` → `$24.99`) and **never does arithmetic on money in JavaScript**:
  line totals, subtotals, shipping, and order totals all come from the API. JavaScript numbers are
  binary floating point, where `0.1 + 0.2` is `0.30000000000000004`.
- **Times are shown in the shopper's local time.** The API sends UTC with a `Z`
  (`2026-11-21T14:03:22.123Z`); `formatDateTime` converts it in the browser. Report dates
  (`2026-11-21`, UTC calendar days) are shown as-is, not shifted.

## Test hooks (`data-testid`)

Stable hooks for the Phase 3 Playwright shoppers. Tests select by these, not by text or CSS, so the
design can change without breaking them. Repeated elements carry an extra `data-*` attribute
(shown in brackets) to pick a specific one.

| Area | `data-testid` |
|---|---|
| Header | `header-search-input`, `header-search-submit`, `nav-cart-link` (plain click opens the cart drawer; its `href` is `/cart`), `cart-count`, `nav-signin-link`, `nav-account-link`, `signout-button`, `nav-admin-link` |
| Catalog | `hero-cta-fasteners`, `hero-cta-power-tools`, `aisle-link` [`data-slug`], `sort-select`, `results-count`, `product-tile` [`data-product-id`], `product-tile-link`, `tile-add-to-cart`, `tile-signin-link`, `empty-state` |
| Cart drawer | `cart-drawer`, `cart-drawer-close`, `cart-drawer-item` [`data-product-id`], `cart-drawer-decrease`, `cart-drawer-increase`, `cart-drawer-remove`, `cart-drawer-subtotal`, `cart-drawer-estimated-total`, `cart-drawer-empty`, `cart-drawer-view-cart`, `cart-drawer-checkout` |
| Toast | `toast` |
| Pagination | `pagination-prev`, `pagination-next`, `pagination-status` |
| Shelf tag | `shelf-tag`, `shelf-tag-price`, `shelf-tag-sku`, `shelf-tag-stock` (on cards it's the badge on the photo; once per product) |
| Product | `product-name`, `quantity-input`, `add-to-cart`, `add-to-cart-message`, `bought-together` |
| Sign-in | `signin-email-input`, `signin-submit`, `signin-demo-button` |
| Cart | `cart-item` [`data-product-id`], `cart-item-quantity`, `cart-item-remove`, `cart-subtotal`, `cart-shipping`, `cart-estimated-total`, `cart-empty`, `checkout-button` |
| Checkout | `shipping-name-input`, `shipping-address-input`, `shipping-city-input`, `shipping-state-input`, `shipping-postal-input`, `payment-method-select`, `checkout-shipping`, `checkout-estimated-total`, `place-order-button`, `order-result` [`data-status`], `order-result-message` |
| Account | `order-row` [`data-order-id`], `order-link`, `orders-empty`, `order-status`, `order-total`, `reorder-button`, `reorder-result` |
| Admin | `admin-token-input`, `admin-signin-submit`, `admin-signout`, `low-stock-toggle`, `inventory-row` [`data-product-id`], `report-start-date`, `report-end-date`, `report-group-by`, `report-run`, `report-row`, `top-n-input`, `top-products-row` |
| Notices | `notice-error`, `notice-info`, `notice-success`, `loading` |

## Layout

```
shoplite/web/
├── .env.example        NEXT_PUBLIC_API_URL template (copy to .env.local)
├── package.json
├── next.config.mjs
├── public/images/      hero and aisle photos (copied from docs/design-reference)
├── lib/
│   ├── api.js          every API call, errors, signed-in customer, admin token
│   ├── format.js       money and time display (string-only money formatting)
│   └── images.js       which aisle photo a product gets (by SKU prefix)
├── components/         Header, CartDrawer, Toast, ShelfTag, ProductTile, Pagination, Notice
└── app/                pages (App Router), globals.css (design tokens), layout.js
```
