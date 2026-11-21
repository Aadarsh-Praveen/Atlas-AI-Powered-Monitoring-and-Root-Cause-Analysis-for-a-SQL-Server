// components/ShelfTag.js: the gold price tag, and the stock badge.
//
// ShelfTag: price (display only: formatted from the API's string, never
// calculated) and SKU on a gold tag. On the product page it also shows the
// stock line under the tag (showStock).
// StockBadge: the stock pill shown on a product card's photo.
//
// Stock wording uses the API's in_stock / low_stock flags, so the rules live
// in one place (the API). Wherever stock appears, it carries
// data-testid="shelf-tag-stock", once per product.

import { formatMoney } from "../lib/format";

export function stockLabel({ stockQuantity, inStock, lowStock }) {
  if (!inStock) return { text: "Out of stock", className: "stock-out" };
  if (lowStock) return { text: `Only ${stockQuantity} left`, className: "stock-low" };
  return { text: `${stockQuantity} in stock`, className: "stock-in" };
}

export function StockBadge({ stockQuantity, inStock, lowStock }) {
  const stock = stockLabel({ stockQuantity, inStock, lowStock });
  return (
    <span className={`stock-badge ${stock.className}`} data-testid="shelf-tag-stock">{stock.text}</span>
  );
}

export default function ShelfTag({ price, sku, stockQuantity, inStock, lowStock, large = false, showStock = true }) {
  const stock = stockLabel({ stockQuantity, inStock, lowStock });
  return (
    <>
      <div className={`shelf-tag${large ? " shelf-tag-large" : ""}`} data-testid="shelf-tag">
        <span className="shelf-tag-price" data-testid="shelf-tag-price">{formatMoney(price)}</span>
        <span className="shelf-tag-sku" data-testid="shelf-tag-sku">
          <span className="visually-hidden">SKU </span>{sku}
        </span>
      </div>
      {showStock && (
        <p className={stock.className} data-testid="shelf-tag-stock" style={{ margin: "var(--space-2) 0 0" }}>
          {stock.text}
        </p>
      )}
    </>
  );
}
