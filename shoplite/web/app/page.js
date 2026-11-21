"use client";
// app/page.js: the catalog (home page). Hero banner, aisles, search results,
// sorting, pagination.
//
// All state lives in the URL:  /?q=drill&aisle=power-tools&sort=price_asc&page=2
// The page reads the URL, asks the API, and draws the result. Clicking an
// aisle, changing the sort, or paging just changes the URL. So every view
// can be bookmarked, shared, and opened directly by a Playwright shopper.

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";

import { ErrorNotice, Loading, Notice } from "../components/Notice";
import Pagination from "../components/Pagination";
import ProductTile from "../components/ProductTile";
import StreamHero from "../components/StreamHero";
import { getCategories, searchProducts } from "../lib/api";

const SORT_OPTIONS = [
  { value: "name", label: "Name A–Z" },
  { value: "price_asc", label: "Price: low to high" },
  { value: "price_desc", label: "Price: high to low" },
  { value: "newest", label: "Newest" },
];
const PAGE_SIZE = 24;

// Build a catalog URL, leaving out empty values and page 1.
function catalogHref({ q, aisle, sort, page }) {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (aisle) params.set("aisle", aisle);
  if (sort && sort !== "name") params.set("sort", sort);
  if (page && page > 1) params.set("page", String(page));
  const query = params.toString();
  return query ? `/?${query}` : "/";
}

function Catalog() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const q = searchParams.get("q") || "";
  const aisle = searchParams.get("aisle") || "";
  const sort = SORT_OPTIONS.some((o) => o.value === searchParams.get("sort")) ? searchParams.get("sort") : "name";
  const page = Math.max(1, Number.parseInt(searchParams.get("page") || "1", 10) || 1);

  const [categories, setCategories] = useState(null);
  const [results, setResults] = useState(null);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0); // bumped by "Try again"

  // Aisles: loaded once.
  useEffect(() => {
    getCategories().then(setCategories).catch(setError);
  }, [attempt]);

  const currentAisle = categories?.find((c) => c.slug === aisle) || null;
  const unknownAisle = Boolean(aisle && categories && !currentAisle);

  // Products: reloaded whenever the URL (or the aisle list) changes.
  useEffect(() => {
    if (!categories) return;
    let cancelled = false; // ignore answers to requests the shopper has moved on from
    setResults(null);
    setError(null);
    searchProducts({
      q,
      category_id: currentAisle?.category_id,
      sort,
      page,
      page_size: PAGE_SIZE,
    })
      .then((data) => !cancelled && setResults(data))
      .catch((err) => !cancelled && setError(err));
    return () => {
      cancelled = true;
    };
  }, [categories, currentAisle?.category_id, q, sort, page, attempt]);

  const hrefFor = useCallback((n) => catalogHref({ q, aisle, sort, page: n }), [q, aisle, sort]);

  // The stream hero only greets the plain home view; searches and aisles go
  // straight to products. With the hero showing, its headline is the page's
  // one <h1> and the catalog title is an <h2>.
  const showHero = !q && !aisle && page === 1;
  const Title = showHero ? "h2" : "h1";
  const heading = q ? `Results for “${q}”` : currentAisle ? currentAisle.name : "Shop all aisles";

  return (
    <>
      {showHero && <StreamHero />}

      <div className="catalog-head">
        <div>
          <Title>{heading}</Title>
          {/* Updates politely for screen readers when results change. */}
          <p className="muted" data-testid="results-count" aria-live="polite" style={{ margin: "var(--space-2) 0 0" }}>
            {results
              ? `${results.total_items.toLocaleString()} ${results.total_items === 1 ? "product" : "products"}`
              : " "}
          </p>
        </div>
        <label className="sort">
          Sort by
          <select
            className="select"
            value={sort}
            onChange={(e) => router.push(catalogHref({ q, aisle, sort: e.target.value }))}
            data-testid="sort-select"
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </label>
      </div>

      {categories && (
        <nav aria-label="Aisles">
          <ul className="aisle-list">
            <li>
              <Link href={catalogHref({ q, sort })} aria-current={!aisle ? "true" : undefined}
                    data-testid="aisle-link" data-slug="">
                All aisles
              </Link>
            </li>
            {categories.map((c) => (
              <li key={c.category_id}>
                <Link href={catalogHref({ q, aisle: c.slug, sort })}
                      aria-current={c.slug === aisle ? "true" : undefined}
                      data-testid="aisle-link" data-slug={c.slug}>
                  {c.name}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
      )}

      {unknownAisle && (
        <Notice kind="info">There’s no aisle called “{aisle}”, so you’re seeing every aisle.</Notice>
      )}

      {error && (
        <ErrorNotice error={error} title="We couldn’t load the products" onRetry={() => setAttempt((n) => n + 1)} />
      )}

      {!error && !results && <Loading label="Stocking the shelves" />}

      {results && (
        <>
          {results.items.length === 0 ? (
            <div className="empty-state" data-testid="empty-state">
              <h2>Nothing on this shelf</h2>
              <p>
                {q
                  ? `No products match “${q}”${currentAisle ? ` in ${currentAisle.name}` : ""}. Try a shorter word or check the spelling.`
                  : "This aisle is empty right now."}
              </p>
              <Link className="btn btn-primary" href="/">Clear filters</Link>
            </div>
          ) : (
            <ul className="product-grid">
              {results.items.map((product, index) => (
                <li key={product.product_id}><ProductTile product={product} index={index} /></li>
              ))}
            </ul>
          )}

          <Pagination page={results.page} totalPages={results.total_pages} hrefFor={hrefFor} />
        </>
      )}
    </>
  );
}

// useSearchParams() must sit inside a Suspense boundary in the App Router:
// the URL's query isn't known while the page is pre-rendered on the server,
// so Next.js shows the fallback until the browser has it.
export default function CatalogPage() {
  return (
    <Suspense fallback={<Loading />}>
      <Catalog />
    </Suspense>
  );
}
