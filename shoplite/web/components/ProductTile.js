"use client";
// components/ProductTile.js: one product card (catalog, "often bought
// together"). Takes a ProductSummary from the API as-is.
//
// Layout follows the design reference: aisle photo with a stock badge, the
// aisle and brand, the name, then the gold price tag and "+ Add".
// "+ Add" puts one unit in the cart through the API and confirms with a toast
// that shows the API's new item count.
//
// Only the name is a link (not the whole card), so screen readers announce a
// short, meaningful link, and the card can also hold the "+ Add" button.

import Link from "next/link";
import { useEffect, useState } from "react";

import { addToCart, CUSTOMER_EVENT, getCustomer } from "../lib/api";
import ProductPhoto from "./ProductPhoto";
import ShelfTag, { StockBadge } from "./ShelfTag";
import { showToast } from "./Toast";

// Signed in or not, read after mounting (browser storage) and kept current.
function useSignedIn() {
  const [signedIn, setSignedIn] = useState(false);
  useEffect(() => {
    setSignedIn(Boolean(getCustomer()));
    const onCustomer = (event) => setSignedIn(Boolean(event.detail));
    window.addEventListener(CUSTOMER_EVENT, onCustomer);
    return () => window.removeEventListener(CUSTOMER_EVENT, onCustomer);
  }, []);
  return signedIn;
}

// index: position in the grid. It staggers the fade-in (CSS --i) and lets
// the first row's photos load right away while later ones load lazily.
export default function ProductTile({ product, index = 0 }) {
  const signedIn = useSignedIn();
  const [adding, setAdding] = useState(false);

  async function quickAdd() {
    setAdding(true);
    try {
      const cart = await addToCart(product.product_id, 1);
      const items = `${cart.item_count} ${cart.item_count === 1 ? "item" : "items"}`;
      showToast(`Added ${product.name} · ${items} in your cart`);
    } catch (err) {
      showToast(err.message, "error"); // e.g. "Only 3 of ... in stock"
    } finally {
      setAdding(false);
    }
  }

  return (
    <article
      className="product-tile"
      style={{ "--i": index }}
      data-testid="product-tile"
      data-product-id={product.product_id}
    >
      <div className="tile-photo">
        {/* Its item type's photo, or a placeholder card: decorative either way. */}
        <ProductPhoto product={product} size="tile" loading={index < 4 ? "eager" : "lazy"} />
        <StockBadge
          stockQuantity={product.stock_quantity}
          inStock={product.in_stock}
          lowStock={product.low_stock}
        />
      </div>

      <div className="tile-info">
        <p className="tile-meta">
          {product.category_name}{product.brand ? ` · ${product.brand}` : ""}
        </p>
        <Link href={`/products/${product.product_id}`} className="product-tile-link" data-testid="product-tile-link">
          {product.name}
        </Link>

        <div className="tile-price-row">
          <ShelfTag price={product.price} sku={product.sku} showStock={false} />
          {!signedIn ? (
            <Link className="btn btn-small" href="/signin" data-testid="tile-signin-link">Sign in to buy</Link>
          ) : (
            <button
              type="button"
              className="btn btn-primary btn-small"
              onClick={quickAdd}
              disabled={adding || !product.in_stock}
              data-testid="tile-add-to-cart"
            >
              {!product.in_stock ? "Sold out" : adding ? "Adding…" : "+ Add"}
              {/* Hidden words complete the visible label ("+ Add Ironclad
                  Hammer to cart") instead of replacing it, so voice-control
                  users can still say "click Add". */}
              <span className="visually-hidden"> {product.name}{product.in_stock ? " to cart" : ""}</span>
            </button>
          )}
        </div>
      </div>
    </article>
  );
}
