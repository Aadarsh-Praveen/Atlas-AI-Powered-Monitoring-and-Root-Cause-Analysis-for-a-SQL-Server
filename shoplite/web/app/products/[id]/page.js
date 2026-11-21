"use client";
// app/products/[id]/page.js: one product. Aisle photo, details, shelf tag
// with stock, quantity, add to cart, and "often bought together".

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";

import { ErrorNotice, Loading, Notice } from "../../../components/Notice";
import ProductTile from "../../../components/ProductTile";
import ShelfTag from "../../../components/ShelfTag";
import { addToCart, getCustomer, getProduct } from "../../../lib/api";
import { formatDateTime } from "../../../lib/format";
import ProductPhoto from "../../../components/ProductPhoto";
import { productPhotoCaption } from "../../../lib/images";

const MAX_PER_ITEM = 99; // the API's limit per product per cart

export default function ProductPage() {
  const { id } = useParams();

  const [product, setProduct] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [signedIn, setSignedIn] = useState(false);
  const [quantity, setQuantity] = useState(1);
  const [adding, setAdding] = useState(false);
  const [addResult, setAddResult] = useState(null); // { ok: true, quantity } or { error }

  useEffect(() => {
    setSignedIn(Boolean(getCustomer())); // browser storage: read after mounting
    let cancelled = false;
    setProduct(null);
    setLoadError(null);
    setAddResult(null);
    getProduct(id)
      .then((data) => !cancelled && setProduct(data))
      .catch((err) => !cancelled && setLoadError(err));
    return () => {
      cancelled = true;
    };
  }, [id]);

  async function handleAdd(event) {
    event.preventDefault();
    setAdding(true);
    setAddResult(null);
    try {
      await addToCart(product.product_id, quantity);
      setAddResult({ ok: true, quantity });
    } catch (err) {
      // The API's own words, e.g. "Only 3 of Ironclad Pro Claw Hammer in stock".
      setAddResult({ error: err });
    } finally {
      setAdding(false);
    }
  }

  if (loadError?.status === 404) {
    return (
      <div className="card">
        <h1>Product not found</h1>
        <p>This product doesn’t exist or is no longer sold. <Link href="/">Browse the aisles</Link>.</p>
      </div>
    );
  }
  if (loadError) return <ErrorNotice error={loadError} title="We couldn’t load this product" />;
  if (!product) return <Loading />;

  // Quantity is a count of units (an integer), not money, so limiting it
  // here is fine. The API checks again and has the final say.
  const maxQuantity = Math.min(MAX_PER_ITEM, product.stock_quantity);
  const photoCaption = productPhotoCaption(product); // null when it shows the placeholder

  return (
    <>
      <p className="small">
        <Link href="/">All aisles</Link> › <Link href={`/?aisle=${product.category_slug}`}>{product.category_name}</Link>
      </p>

      <div className="two-column">
        <div className="stack">
          {/* This item type's photo, or a placeholder card if the type has
              none. Never the exact product: decorative, and when it's a photo
              the caption says so. */}
          <figure className="stack" style={{ margin: 0 }}>
            <div className="product-photo">
              <ProductPhoto product={product} size="page" loading="eager" />
            </div>
            {photoCaption && <figcaption className="muted small">{photoCaption}</figcaption>}
          </figure>
          <h1 data-testid="product-name">{product.name}</h1>
          {product.brand && <p className="muted">By {product.brand}</p>}
          <p>{product.description}</p>
          <p className="muted small">In the catalog since {formatDateTime(product.created_at)}</p>
        </div>

        <aside className="card stack" aria-label="Buy">
          <ShelfTag
            large
            price={product.price}
            sku={product.sku}
            stockQuantity={product.stock_quantity}
            inStock={product.in_stock}
            lowStock={product.low_stock}
          />

          {!signedIn ? (
            <p>
              <Link className="btn btn-primary" href="/signin">Sign in to buy</Link>
            </p>
          ) : product.in_stock ? (
            <form onSubmit={handleAdd} className="stack">
              <div className="field">
                <label htmlFor="quantity">Quantity</label>
                <input
                  id="quantity"
                  className="input input-narrow"
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={maxQuantity}
                  value={quantity}
                  onChange={(e) => {
                    const value = Number.parseInt(e.target.value, 10);
                    setQuantity(Number.isNaN(value) ? 1 : Math.min(Math.max(value, 1), maxQuantity));
                  }}
                  data-testid="quantity-input"
                />
              </div>
              <button type="submit" className="btn btn-primary" disabled={adding} data-testid="add-to-cart">
                {adding ? "Adding…" : "Add to cart"}
              </button>
            </form>
          ) : (
            <p className="stock-out">Out of stock. Check back after the next delivery.</p>
          )}

          <div data-testid="add-to-cart-message" aria-live="polite">
            {addResult?.ok && (
              <Notice kind="success">
                Added {addResult.quantity} to your cart. <Link href="/cart">View cart</Link>
              </Notice>
            )}
            {addResult?.error && <ErrorNotice error={addResult.error} title="Couldn’t add to cart" />}
          </div>
        </aside>
      </div>

      <section data-testid="bought-together" style={{ marginTop: "var(--space-7)" }}>
        <h2>Often bought together</h2>
        {product.bought_together.length === 0 ? (
          <p className="muted">No shopper has bought this with anything else yet.</p>
        ) : (
          <ul className="product-grid">
            {product.bought_together.map((p, index) => (
              <li key={p.product_id}><ProductTile product={p} index={index} /></li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
