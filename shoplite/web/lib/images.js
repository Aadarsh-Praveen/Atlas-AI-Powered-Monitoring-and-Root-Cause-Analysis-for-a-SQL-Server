// lib/images.js: which photo (if any) a product gets, and what to show
// when it has none.
//
// ONE PHOTO PER ITEM TYPE. Each photo in public/images belongs to exactly one
// item type, checked by looking at the photo (every "... Claw Hammer ..."
// gets the hammer, and no other item type uses it). Products of the other
// item types get no photo; components/ProductPhoto.js shows a placeholder
// card with their item type and aisle instead. No photo is ever reused for
// a different kind of item.
//
// The item type is found as whole words anywhere in the name, because names
// can end with a pack size: "Bluestem 3/8in Flat Zinc-Plated Washers
// (100-Pack)". The aisle comes from the SKU prefix (PL-00042 is Plumbing).
// So this works anywhere a product's name and SKU are shown, with no extra
// API fields.
//
// A photo shows the item type, not the exact brand or size, so images stay
// decorative (alt=""); productPhotoCaption() says so on the product page.

export const HERO_IMAGE = "/images/hero.webp";

// Item type -> its photo, verified against each image:
//   product-0  yellow/black cordless drill with battery
//   product-1  claw hammer
//   product-2  black bugle-head drywall screws
//   product-3  paint can with brush (the can is the subject)
//   product-5  coiled yellow extension cord
//   product-6  bypass pruning shears
//   product-7  black latching plastic storage tote (no carry handle)
//   product-8  steel nails (our only nail type)
//   product-9  hex bolts
//   product-10 hex-head lag screws
//   product-11 flat washers
// product-4 (chrome elbow pipe fitting) matches no item ShopLite sells.
const ITEM_PHOTOS = {
  "Cordless Drill": "/images/product-0.webp",
  "Claw Hammer": "/images/product-1.webp",
  "Drywall Screws": "/images/product-2.webp",
  "Interior Paint": "/images/product-3.webp",
  "Extension Cord": "/images/product-5.webp",
  "Pruning Shears": "/images/product-6.webp",
  "Storage Bin": "/images/product-7.webp",
  "Finishing Nails": "/images/product-8.webp",
  "Hex Bolts": "/images/product-9.webp",
  "Lag Screws": "/images/product-10.webp",
  "Washers": "/images/product-11.webp",
  // Added one at a time from the owner's own photos (public/images/items/).
  "Utility Knife": "/images/items/utility-knife.png",
  "Pliers": "/images/items/pliers.png",
  "Tape Measure": "/images/items/tape-measure.png",
  "Adjustable Wrench": "/images/items/adjustable-wrench.png",
  "Screwdriver Set": "/images/items/screwdriver-set.png",
  "Socket Set": "/images/items/socket-set.png",
  "Level": "/images/items/level.png",
  "Hand Saw": "/images/items/hand-saw.png",
  "Impact Driver": "/images/items/impact-driver.png",
  "Circular Saw": "/images/items/circular-saw.png",
  "Random Orbit Sander": "/images/items/random-orbit-sander.png",
  "Jigsaw": "/images/items/jigsaw.png",
  "Angle Grinder": "/images/items/angle-grinder.png",
  // Found by searching (mostly Wikimedia Commons), each checked by eye.
  "Oscillating Multi-Tool": "/images/items/oscillating-multi-tool.jpg",
  "Battery Pack": "/images/items/battery-pack.jpg",
  "Wood Screws": "/images/items/wood-screws.jpg",
  "Wall Anchors": "/images/items/wall-anchors.jpg",
  "Machine Screws": "/images/items/machine-screws.jpg",
  "Exterior Paint": "/images/items/exterior-paint.jpg",
  "Spray Paint": "/images/items/spray-paint.jpg",
  "Kitchen Faucet": "/images/items/kitchen-faucet.jpg",
  "PEX Pipe": "/images/items/pex-pipe.jpg",
  "Pipe Wrench": "/images/items/pipe-wrench.jpg",
  "Sump Pump": "/images/items/sump-pump.jpg",
  "LED Bulb": "/images/items/led-bulb.jpg",
  "Outlet": "/images/items/outlet.jpg",
  "Light Switch": "/images/items/light-switch.jpg",
  "Circuit Breaker": "/images/items/circuit-breaker.jpg",
  "Smart Plug": "/images/items/smart-plug.jpg",
  "Garden Hose": "/images/items/garden-hose.jpg",
  "Shovel": "/images/items/shovel.jpg",
  "Rake": "/images/items/rake.jpg",
  "Wheelbarrow": "/images/items/wheelbarrow.jpg",
  "Sprinkler": "/images/items/sprinkler.jpg",
  "Potting Mix": "/images/items/potting-mix.jpg",
  "Leaf Blower": "/images/items/leaf-blower.jpg",
  "Tool Box": "/images/items/tool-box.jpg",
  "Pegboard Kit": "/images/items/pegboard-kit.jpg",
  "Wall Hooks": "/images/items/wall-hooks.jpg",
  "Step Ladder": "/images/items/step-ladder.jpg",
  "Workbench": "/images/items/workbench.jpg",
  // The owner's set from docs/design-reference/ShopLite-hardware-images.
  "Primer": "/images/items/primer.png",
  "Paint Roller Kit": "/images/items/paint-roller-kit.png",
  "Angled Brush": "/images/items/angled-brush.png",
  "Painter's Tape": "/images/items/painters-tape.png",
  "Drop Cloth": "/images/items/drop-cloth.png",
  "Ball Valve": "/images/items/ball-valve.png",
  "Toilet Repair Kit": "/images/items/toilet-repair-kit.png",
  "P-Trap": "/images/items/p-trap.png",
  "Supply Line": "/images/items/supply-line.png",
  "Electrical Wire": "/images/items/electrical-wire.png",
  "Work Light": "/images/items/work-light.png",
  "Shelving Unit": "/images/items/shelving-unit.png",
  "Rolling Tool Chest": "/images/items/rolling-tool-chest.png",
};

// Every item type ShopLite sells (the nouns in shoplite/db/seed.py), used to
// label the placeholder. Keep in step with seed.py's CATEGORIES.
const ITEM_TYPES = [
  "Claw Hammer", "Screwdriver Set", "Adjustable Wrench", "Tape Measure", "Utility Knife", "Pliers",
  "Socket Set", "Level", "Hand Saw",
  "Cordless Drill", "Impact Driver", "Circular Saw", "Random Orbit Sander", "Jigsaw", "Angle Grinder",
  "Oscillating Multi-Tool", "Battery Pack",
  "Wood Screws", "Drywall Screws", "Hex Bolts", "Lag Screws", "Wall Anchors", "Finishing Nails",
  "Washers", "Machine Screws",
  "Interior Paint", "Exterior Paint", "Primer", "Spray Paint", "Paint Roller Kit", "Angled Brush",
  "Painter's Tape", "Drop Cloth",
  "Kitchen Faucet", "PEX Pipe", "Ball Valve", "Toilet Repair Kit", "P-Trap", "Pipe Wrench",
  "Supply Line", "Sump Pump",
  "LED Bulb", "Extension Cord", "Outlet", "Light Switch", "Electrical Wire", "Circuit Breaker",
  "Smart Plug", "Work Light",
  "Garden Hose", "Pruning Shears", "Shovel", "Rake", "Wheelbarrow", "Sprinkler", "Potting Mix",
  "Leaf Blower",
  "Tool Box", "Shelving Unit", "Storage Bin", "Pegboard Kit", "Rolling Tool Chest", "Wall Hooks",
  "Step Ladder", "Workbench",
];

const AISLES = {
  PT: "Power Tools", HT: "Hand Tools", FS: "Fasteners", PA: "Paint",
  PL: "Plumbing", EL: "Electrical", GD: "Garden", ST: "Storage",
};

// The product's item type: the longest listed type found as whole words in
// its name ("... Rolling Tool Chest" is a Rolling Tool Chest, not a Tool Box).
export function itemTypeOf({ name }) {
  const padded = ` ${name || ""} `;
  let best = null;
  for (const type of ITEM_TYPES) {
    if (padded.includes(` ${type} `) && (!best || type.length > best.length)) best = type;
  }
  return best;
}

export function aisleOf({ sku }) {
  return AISLES[(sku || "").split("-")[0]] || null;
}

// The photo for this product's item type, or null if that type has none.
export function productImage(product) {
  const type = itemTypeOf(product);
  return (type && ITEM_PHOTOS[type]) || null;
}

// Product page caption for a photo (only shown when there is one).
export function productPhotoCaption(product) {
  const type = itemTypeOf(product);
  return type && ITEM_PHOTOS[type]
    ? `Photo shows a typical ${type.toLowerCase()}, not this exact brand or size.`
    : null;
}
