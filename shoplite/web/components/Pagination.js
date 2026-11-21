// components/Pagination.js: Previous / "Page X of Y" / Next.
//
// Pages are LINKS that change the URL (?page=3), not buttons that change
// hidden state. So the back button works, a page can be bookmarked or shared,
// and a Playwright shopper can open page 3 directly.
//
// hrefFor(pageNumber) is supplied by the page, which knows its other URL
// parameters (search, aisle, sort) and keeps them.

import Link from "next/link";

export default function Pagination({ page, totalPages, hrefFor }) {
  if (!totalPages || totalPages <= 1) return null;

  const hasPrevious = page > 1;
  const hasNext = page < totalPages;

  return (
    <nav className="pagination" aria-label="Pages">
      {hasPrevious ? (
        <Link className="btn" href={hrefFor(page - 1)} data-testid="pagination-prev" rel="prev">
          ← Previous
        </Link>
      ) : (
        // At the first page there's nothing to go back to: plain text, not a
        // dead link, and announced as unavailable.
        <span className="btn" aria-disabled="true" data-testid="pagination-prev">← Previous</span>
      )}

      <span data-testid="pagination-status" aria-current="page">
        Page {page} of {totalPages}
      </span>

      {hasNext ? (
        <Link className="btn" href={hrefFor(page + 1)} data-testid="pagination-next" rel="next">
          Next →
        </Link>
      ) : (
        <span className="btn" aria-disabled="true" data-testid="pagination-next">Next →</span>
      )}
    </nav>
  );
}
