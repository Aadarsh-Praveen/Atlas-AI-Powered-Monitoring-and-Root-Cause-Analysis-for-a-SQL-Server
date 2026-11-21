// components/Notice.js: loading, empty, success, and error states.
//
// Messages are plain language: what happened, and what to do next. Errors
// from the API keep its own wording (e.g. "Only 3 of ... in stock"), and
// server-side failures show the request ID as a reference, the same ID the
// API wrote in its log.

// kind: "info" | "success" | "error"
export function Notice({ kind = "info", title, children }) {
  return (
    <div
      className={`notice notice-${kind}`}
      data-testid={`notice-${kind}`}
      // Errors interrupt the screen reader (alert); others wait politely (status).
      role={kind === "error" ? "alert" : "status"}
    >
      {title && <strong className="notice-title">{title}</strong>}
      {children}
    </div>
  );
}

// Shows an ApiError (from lib/api.js) the same way everywhere.
export function ErrorNotice({ error, title = "Something went wrong", onRetry }) {
  if (!error) return null;
  // A reference only helps for failures on our side (5xx) or when the store
  // couldn't be reached; "out of stock" doesn't need one.
  const showReference = error.requestId && (error.status >= 500 || error.status === 0);
  return (
    <Notice kind="error" title={title}>
      <span>{error.message}</span>
      {showReference && <span className="notice-reference">Reference: {error.requestId}</span>}
      {onRetry && (
        <div style={{ marginTop: "var(--space-2)" }}>
          <button type="button" className="btn-link" onClick={onRetry}>Try again</button>
        </div>
      )}
    </Notice>
  );
}

export function Loading({ label = "Loading" }) {
  return (
    <p className="loading" data-testid="loading" role="status" aria-live="polite">
      {label}…
    </p>
  );
}
