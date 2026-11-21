"use client";
// components/Toast.js: a short message at the bottom of the screen, such as
// "Added to cart · 3 items in your cart". Mounted once in app/layout.js;
// shown from anywhere with showToast(message, kind).
//
// The element is ALWAYS in the page (empty when idle) with role="status".
// Screen readers announce changes to a live region only if the region
// already existed, so it must not be created at the moment of the message.

import { useEffect, useRef, useState } from "react";

const TOAST_EVENT = "shoplite:toast";
const VISIBLE_MS = 2300;

// kind: "info" (default) or "error"
export function showToast(message, kind = "info") {
  window.dispatchEvent(new CustomEvent(TOAST_EVENT, { detail: { message, kind } }));
}

export default function Toast() {
  const [toast, setToast] = useState({ message: "", kind: "info", visible: false });
  const timer = useRef(null);

  useEffect(() => {
    function onToast(event) {
      clearTimeout(timer.current);
      setToast({ ...event.detail, visible: true });
      // Hide after a moment; the text stays until the next message so the
      // fade-out doesn't flash an empty box.
      timer.current = setTimeout(() => setToast((t) => ({ ...t, visible: false })), VISIBLE_MS);
    }
    window.addEventListener(TOAST_EVENT, onToast);
    return () => {
      window.removeEventListener(TOAST_EVENT, onToast);
      clearTimeout(timer.current);
    };
  }, []);

  return (
    <p
      className={`toast${toast.visible ? " visible" : ""}${toast.kind === "error" ? " toast-error" : ""}`}
      role="status"
      aria-live="polite"
      data-testid="toast"
    >
      {toast.message}
    </p>
  );
}
