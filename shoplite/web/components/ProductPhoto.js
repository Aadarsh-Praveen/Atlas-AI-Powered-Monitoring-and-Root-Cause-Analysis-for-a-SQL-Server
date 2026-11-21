// components/ProductPhoto.js: a product's photo, or a placeholder card when
// its item type has no photo of its own (see lib/images.js). Fills whatever
// box it's placed in (.tile-photo, .product-photo, .line-thumb).
//
// Both are decorative: the product's name is always shown next to them, so
// the photo has alt="" and the placeholder is aria-hidden. The placeholder
// shows the item type and aisle, so a card is never blank.
//
// size: "tile" (cards), "page" (product page), or "thumb" (cart lines,
// where only the item type fits).

import { aisleOf, itemTypeOf, productImage } from "../lib/images";

export default function ProductPhoto({ product, size = "tile", loading = "lazy" }) {
  const src = productImage(product);
  if (src) return <img src={src} alt="" loading={loading} decoding="async" draggable="false" />;

  const type = itemTypeOf(product);
  const aisle = aisleOf(product);
  return (
    <div className={`photo-placeholder photo-placeholder-${size}`} aria-hidden="true">
      <span className="photo-placeholder-type">{type || aisle || "ShopLite"}</span>
      {size !== "thumb" && type && aisle && <span className="photo-placeholder-aisle">{aisle}</span>}
    </div>
  );
}
