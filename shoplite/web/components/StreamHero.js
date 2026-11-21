// components/StreamHero.js: the catalog home's banner. Two streams of
// product photos flow out of a vanishing point and past the headline, in 3D.
//
// Visual design from docs/design-reference/stream-hero.html (look only).
// Everything moving is plain CSS: the paths are precomputed @keyframes
// (stream-right / stream-left in app/globals.css), so no JavaScript runs per
// render or per frame, and the server and browser render identical markup.
//
// Accessibility:
//   - The photo stream is decoration: aria-hidden, alt="", not clickable.
//   - A dark scrim sits between the photos and the text, for readable copy.
//   - With "reduce motion" on, the cards stop where they are (paused), so the
//     banner shows a still 3D spread instead of moving.
//   - On narrow phones a static photo collage replaces the 3D stream.
// The buttons are ordinary links, so they work as navigation (and with
// JavaScript off).

import Link from "next/link";

const CARDS_PER_RAIL = 9;
const LOOP_SECONDS = 26;

// Our 12 photos (public/images). Each rail shows 9; the left rail starts six
// photos later so the two streams don't mirror each other.
const PHOTOS = Array.from({ length: 12 }, (_, i) => `/images/product-${i}.webp`);
const RIGHT_RAIL = Array.from({ length: CARDS_PER_RAIL }, (_, i) => PHOTOS[i % PHOTOS.length]);
const LEFT_RAIL = Array.from({ length: CARDS_PER_RAIL }, (_, i) => PHOTOS[(i + 6) % PHOTOS.length]);

// Negative delays spread the nine cards evenly along the path from the very
// first frame (card i starts i/9 of the way through the loop). Computed once
// when this module loads, never on render.
const DELAYS = Array.from({ length: CARDS_PER_RAIL }, (_, i) => `${(-(i * LOOP_SECONDS) / CARDS_PER_RAIL).toFixed(2)}s`);

// Photos for the static collage on narrow phones.
const STATIC_PHOTOS = [PHOTOS[0], PHOTOS[2], PHOTOS[3], PHOTOS[1]];

function Rail({ photos, side }) {
  return photos.map((src, i) => (
    <div key={`${side}-${i}`} className={`stream-card stream-card-${side}`} style={{ "--delay": DELAYS[i] }}>
      <img src={src} alt="" draggable="false" decoding="async" />
    </div>
  ));
}

export default function StreamHero() {
  return (
    <section className="stream-hero" aria-labelledby="stream-hero-title">
      {/* 3D photo stream: decorative, hidden from screen readers. */}
      <div className="stream-stage" aria-hidden="true">
        <div className="stream-rails">
          <Rail photos={RIGHT_RAIL} side="right" />
          <Rail photos={LEFT_RAIL} side="left" />
        </div>
      </div>

      {/* Narrow phones: a still collage instead of the cramped 3D stream. */}
      <div className="stream-static" aria-hidden="true">
        {STATIC_PHOTOS.map((src) => <img key={src} src={src} alt="" decoding="async" />)}
      </div>

      {/* Dark scrim between photos and text, so the copy stays readable. */}
      <div className="stream-scrim" aria-hidden="true" />

      <div className="stream-copy">
        <span className="stream-badge">Hardware · Aisles 1–8</span>
        <h1 id="stream-hero-title">Tools &amp; hardware for every build.</h1>
        <p>
          From cordless drivers and hand tools to steel fasteners: browse eight aisles with live
          stock levels.
        </p>
        <div className="stream-actions">
          <Link className="btn btn-primary" href="/?aisle=fasteners" data-testid="hero-cta-fasteners">
            Shop fasteners
          </Link>
          <Link className="btn btn-on-dark" href="/?aisle=power-tools" data-testid="hero-cta-power-tools">
            Explore power tools
          </Link>
        </div>
      </div>
    </section>
  );
}
