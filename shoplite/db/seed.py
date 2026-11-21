"""Generate realistic ShopLite data and bulk-load it into SalesDB.

Run after apply_schema.py, from the repo root, inside the virtual environment:
    python shoplite/db/seed.py                      # local defaults
    python shoplite/db/seed.py --orders 1000000     # production volume (Phase 2)

Realistic means volume and skew, because even, tiny data hides every
performance problem:
  - long-tail product popularity (a few best sellers, many slow movers)
  - heavy and light customers (a few order often, most rarely)
  - seasonality (spring and holiday peaks, busier weekends, daytime hours,
    gentle growth over the history)
  - mostly small orders, age-appropriate statuses, a few cancellations
  - products that are often bought together

Everything comes from one random generator with a fixed seed, so the same
parameters (including --end-date) always produce the same data.
"""

import argparse
import bisect
import itertools
import os
import random
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pyodbc
from dotenv import load_dotenv

DB_DIR = Path(__file__).resolve().parent
REPO_ROOT = DB_DIR.parent.parent
ENV_FILE = REPO_ROOT / ".env"

HOST = "localhost"
SQL_PORT = 1433
ODBC_DRIVER = "ODBC Driver 18 for SQL Server"
LOGIN_TIMEOUT_SECONDS = 5

# Rows sent and committed per round trip. Committing in chunks keeps each
# transaction (and, under SIMPLE recovery, the log) small, and means an
# interruption loses at most one chunk of work.
CHUNK_SIZE = 5000

# ===========================================================================
# Reference data
# ===========================================================================

# Aisles: (name, slug, SKU prefix, share of products, description, items)
#
# Each item is (noun, min price, max price, detail lists, suffixes).
# A product name is:  brand + one choice from each detail list + noun
#                     (+ a suffix such as a pack size, if the item has any)
#   e.g. "Bluestem 3/8in Split Lock Stainless Steel Washers (100-Pack)"
# Details belong to the item, so they always make sense for it (no
# "#10 x 2in Washers"). Every combination for every brand is generated and
# handed out once, so no two products share a name (see generate_products).
# The noun is always present as whole words: lib/images.js in the storefront
# finds a product's photo by it.
CATEGORIES = [
    ("Hand Tools", "hand-tools", "HT", 15, "Hammers, wrenches, screwdrivers, and measuring tools.", [
        ("Claw Hammer", 12, 35, [["13 oz", "16 oz", "20 oz", "22 oz", "28 oz"], ["Fiberglass", "Steel", "Hickory", "Graphite"]], []),
        ("Screwdriver Set", 15, 45, [["6-Piece", "8-Piece", "10-Piece", "14-Piece", "20-Piece"], ["Precision", "Magnetic", "Insulated", "Ratcheting"]], []),
        ("Adjustable Wrench", 10, 30, [["6in", "8in", "10in", "12in", "15in"], ["Chrome", "Black Oxide", "Satin Nickel"]], []),
        ("Tape Measure", 8, 25, [["12ft", "16ft", "25ft", "30ft", "35ft"], ["Magnetic", "Compact", "Wide-Blade", "Auto-Lock"]], []),
        ("Utility Knife", 6, 20, [["Retractable", "Folding", "Fixed-Blade", "Snap-Off"], ["Aluminum", "Rubber-Grip", "Heavy-Duty", "Compact"]], []),
        ("Pliers", 9, 28, [["6in", "8in", "10in"], ["Needle-Nose", "Slip-Joint", "Lineman's", "Locking", "Diagonal-Cutting"]], []),
        ("Socket Set", 25, 120, [["20-Piece", "40-Piece", "65-Piece", "100-Piece", "150-Piece"], ["1/4in Drive", "3/8in Drive", "1/2in Drive", "SAE & Metric"]], []),
        ("Level", 12, 60, [["9in", "24in", "48in", "72in"], ["Torpedo", "Box Beam", "I-Beam", "Magnetic", "Digital"]], []),
        ("Hand Saw", 14, 40, [["12in", "15in", "20in", "26in"], ["Fine-Cut", "Crosscut", "Rip-Cut", "Pull", "Drywall"]], []),
    ]),
    ("Power Tools", "power-tools", "PT", 12, "Drills, saws, sanders, and batteries.", [
        ("Cordless Drill", 59, 229, [["12V", "18V", "20V Max", "24V"], ["Brushless", "Compact", "Hammer"]], ["Kit", "(Tool Only)", "with 2 Batteries"]),
        ("Impact Driver", 79, 249, [["12V", "18V", "20V Max"], ["Brushless", "Compact", "3-Speed"]], ["Kit", "(Tool Only)"]),
        ("Circular Saw", 69, 219, [["6-1/2in", "7-1/4in", "8-1/4in"], ["Cordless", "Corded", "Worm Drive", "Brushless"]], ["Kit", "(Tool Only)"]),
        ("Random Orbit Sander", 45, 139, [["5in", "6in"], ["Corded", "Cordless", "Variable-Speed", "Low-Vibration"]], ["with Dust Bag", "Kit"]),
        ("Jigsaw", 49, 159, [["Corded", "Cordless", "Brushless"], ["Orbital", "Top-Handle", "Barrel-Grip", "Variable-Speed"]], ["Kit", "(Tool Only)"]),
        ("Angle Grinder", 39, 149, [["4-1/2in", "5in", "6in"], ["Corded", "Cordless", "Paddle-Switch", "Slide-Switch"]], ["Kit", "(Tool Only)"]),
        ("Oscillating Multi-Tool", 59, 179, [["Corded", "Cordless", "Brushless"], ["Quick-Change", "Variable-Speed", "Starlock"]], ["Kit", "with 20 Accessories", "(Tool Only)"]),
        ("Battery Pack", 39, 129, [["12V", "18V", "20V Max", "24V"], ["2.0Ah", "4.0Ah", "5.0Ah", "6.0Ah", "8.0Ah"]], ["", "(2-Pack)"]),
    ]),
    ("Fasteners", "fasteners", "FS", 18, "Screws, nails, bolts, anchors, and washers.", [
        ("Wood Screws", 4, 19, [["#6 x 1in", "#6 x 1-1/4in", "#8 x 1-1/4in", "#8 x 1-5/8in", "#8 x 2in", "#10 x 2-1/2in", "#10 x 3in", "#12 x 3-1/2in"],
                                ["Zinc-Plated", "Yellow Zinc", "Stainless Steel", "Brass"]], ["(50-Pack)", "(100-Pack)", "(1 lb Box)", "(5 lb Box)"]),
        ("Drywall Screws", 5, 22, [["#6 x 1-1/4in", "#6 x 1-5/8in", "#8 x 2in", "#8 x 2-1/2in", "#8 x 3in"], ["Coarse-Thread", "Fine-Thread"]],
         ["(100-Pack)", "(1 lb Box)", "(5 lb Box)", "(25 lb Bucket)"]),
        ("Hex Bolts", 3, 15, [["1/4in x 1in", "1/4in x 2in", "5/16in x 1-1/2in", "3/8in x 2in", "3/8in x 3in", "1/2in x 4in"],
                              ["Zinc-Plated", "Stainless Steel", "Galvanized", "Grade 8"]], ["(10-Pack)", "(25-Pack)", "(50-Pack)"]),
        ("Lag Screws", 5, 20, [["1/4in x 2in", "5/16in x 3in", "3/8in x 4in", "1/2in x 6in"], ["Hot-Dip Galvanized", "Zinc-Plated", "Stainless Steel"]],
         ["(10-Pack)", "(25-Pack)", "(50-Pack)"]),
        ("Wall Anchors", 3, 12, [["#6-8", "#8-10", "#10-12", "1/4in"], ["Ribbed Plastic", "Self-Drilling", "Toggle", "Molly"]],
         ["(10-Pack)", "(25-Pack)", "(50-Pack)", "(100-Pack)"]),
        ("Finishing Nails", 3, 11, [["4d 1-1/2in", "6d 2in", "8d 2-1/2in", "10d 3in"], ["Bright Steel", "Galvanized", "Stainless Steel"]],
         ["(1 lb Box)", "(5 lb Box)"]),
        ("Washers", 2, 9, [["#8", "#10", "1/4in", "5/16in", "3/8in", "1/2in"], ["Flat", "Split Lock", "Fender"], ["Zinc-Plated", "Stainless Steel"]],
         ["(25-Pack)", "(100-Pack)"]),
        ("Machine Screws", 3, 13, [["#6-32 x 1/2in", "#8-32 x 3/4in", "#10-24 x 1in", "#10-32 x 1-1/2in", "1/4in-20 x 2in"],
                                   ["Pan-Head", "Flat-Head", "Round-Head"], ["Stainless Steel", "Zinc-Plated"]], ["(25-Pack)", "(100-Pack)"]),
    ]),
    ("Paint", "paint", "PA", 10, "Interior and exterior paint, primer, and painting supplies.", [
        ("Interior Paint", 25, 65, [["Flat", "Matte", "Eggshell", "Satin", "Semi-Gloss"],
                                    ["Ultra White", "Swiss Coffee", "Agreeable Gray", "Naval Blue", "Sage Green", "Charcoal"]],
         ["(1 Quart)", "(1 Gallon)", "(5 Gallon)"]),
        ("Exterior Paint", 30, 75, [["Flat", "Satin", "Semi-Gloss"], ["Bright White", "Barn Red", "Slate Gray", "Forest Green", "Navy"]],
         ["(1 Gallon)", "(5 Gallon)"]),
        ("Primer", 18, 45, [["Stain-Blocking", "Drywall", "Bonding", "Oil-Based", "Exterior"]], ["(1 Quart)", "(1 Gallon)", "(5 Gallon)"]),
        ("Spray Paint", 5, 12, [["Gloss", "Matte", "Satin", "Metallic", "Hammered"], ["Black", "White", "Red", "Silver", "Safety Yellow", "Navy"]],
         ["(12 oz)", "(16 oz)"]),
        ("Paint Roller Kit", 10, 30, [["3-Piece", "5-Piece", "9-Piece"], ["4in", "9in", "18in"], ["Smooth-Surface", "Semi-Rough", "Rough-Surface"]], []),
        ("Angled Brush", 6, 22, [["1in", "1-1/2in", "2in", "2-1/2in", "3in"], ["Nylon", "Polyester", "Natural Bristle", "Nylon/Polyester"]], []),
        ("Painter's Tape", 4, 12, [["0.94in", "1.41in", "1.88in", "2.83in"], ["Multi-Surface", "Delicate-Surface", "Exterior"]], ["(60 yd)", "(3-Pack)"]),
        ("Drop Cloth", 7, 25, [["4ft x 12ft", "6ft x 9ft", "9ft x 12ft", "12ft x 15ft"], ["Canvas", "Plastic", "Butyl-Backed"]], []),
    ]),
    ("Plumbing", "plumbing", "PL", 13, "Faucets, pipe, valves, and repair parts.", [
        ("Kitchen Faucet", 69, 299, [["Pull-Down", "Pull-Out", "Single-Handle", "Two-Handle", "Touchless"],
                                     ["Chrome", "Brushed Nickel", "Matte Black", "Stainless Steel"]], []),
        ("PEX Pipe", 15, 60, [["1/2in", "3/4in", "1in"], ["Red", "Blue", "White"]], ["(10 ft)", "(100 ft Coil)", "(300 ft Coil)"]),
        ("Ball Valve", 8, 35, [["1/2in", "3/4in", "1in"], ["Brass", "Lead-Free Brass", "PVC"], ["Sweat", "Threaded", "Push-Fit", "PEX"]], []),
        ("Toilet Repair Kit", 10, 30, [["Quiet-Fill", "Water-Saving", "Adjustable"], ["Universal", "Fill Valve", "Flapper", "Complete", "Dual-Flush"]], []),
        ("P-Trap", 5, 18, [["1-1/4in", "1-1/2in", "2in"], ["PVC", "ABS", "Chrome-Plated Brass"]], ["", "with Cleanout"]),
        ("Pipe Wrench", 18, 55, [["8in", "10in", "14in", "18in", "24in"], ["Cast Iron", "Aluminum", "Offset"]], []),
        ("Supply Line", 6, 18, [["12in", "16in", "20in", "24in", "30in"], ["Braided Stainless", "Polymer"], ["Faucet", "Toilet", "Ice Maker", "Dishwasher"]], []),
        ("Sump Pump", 89, 299, [["1/4 HP", "1/3 HP", "1/2 HP", "3/4 HP"], ["Cast Iron", "Thermoplastic"], ["Submersible", "Pedestal", "Battery Backup"]], []),
    ]),
    ("Electrical", "electrical", "EL", 13, "Bulbs, cords, outlets, switches, and wire.", [
        ("LED Bulb", 4, 25, [["A19", "BR30", "PAR38", "Candelabra", "Edison"], ["60W Equivalent", "75W Equivalent", "100W Equivalent"], ["Soft White", "Daylight"]],
         ["", "(4-Pack)"]),
        ("Extension Cord", 9, 45, [["6ft", "15ft", "25ft", "50ft", "100ft"], ["16-Gauge", "14-Gauge", "12-Gauge"], ["Indoor", "Outdoor"]], []),
        ("Outlet", 3, 25, [["15A", "20A"], ["Duplex", "GFCI", "Tamper-Resistant", "USB", "Smart"], ["White", "Light Almond", "Black"]], []),
        ("Light Switch", 3, 30, [["Single-Pole", "3-Way", "Dimmer", "Smart", "Motion-Sensing"], ["Toggle", "Rocker"], ["White", "Black"]], []),
        ("Electrical Wire", 15, 120, [["14/2", "12/2", "10/3", "12/3"], ["NM-B", "THHN", "UF-B"]], ["(25 ft)", "(50 ft)", "(250 ft)"]),
        ("Circuit Breaker", 8, 60, [["15A", "20A", "30A", "50A"], ["Single-Pole", "Double-Pole", "GFCI", "AFCI"]], []),
        ("Smart Plug", 12, 35, [["Wi-Fi", "Zigbee", "Matter"], ["Indoor", "Outdoor", "Mini"]], ["", "(2-Pack)", "(4-Pack)"]),
        ("Work Light", 20, 90, [["1,000-Lumen", "3,000-Lumen", "5,000-Lumen", "10,000-Lumen"], ["LED Tripod", "Clamp", "Rechargeable", "Magnetic"]], []),
    ]),
    ("Garden", "garden", "GD", 11, "Hoses, hand tools, soil, and outdoor power equipment.", [
        ("Garden Hose", 19, 79, [["25ft", "50ft", "75ft", "100ft"], ["5/8in", "3/4in"], ["Rubber", "Vinyl", "Hybrid", "Expandable"]], []),
        ("Pruning Shears", 12, 45, [["Bypass", "Anvil", "Ratcheting"], ["Steel", "Titanium-Coated", "Ergonomic", "Compact"]], []),
        ("Shovel", 20, 55, [["Fiberglass-Handle", "Wood-Handle", "Steel-Handle"], ["Round-Point", "Square-Point", "Trenching", "Drain Spade", "Snow"]], []),
        ("Rake", 15, 40, [["Steel", "Poly", "Bamboo"], ["Leaf", "Bow", "Shrub", "Thatch"]], []),
        ("Wheelbarrow", 69, 199, [["4 cu ft", "6 cu ft", "8 cu ft"], ["Steel", "Poly"], ["Single-Wheel", "Dual-Wheel"]], []),
        ("Sprinkler", 8, 40, [["Adjustable", "Timer-Ready", "Heavy-Duty"], ["Metal", "Plastic"], ["Oscillating", "Impact", "Rotary", "Pulsating", "Stationary"]], []),
        ("Potting Mix", 6, 18, [["Organic", "All-Purpose", "Moisture-Control", "Cactus", "Seed-Starting"]], ["(8 qt)", "(16 qt)", "(1 cu ft)", "(2 cu ft)"]),
        ("Leaf Blower", 59, 229, [["400 CFM", "500 CFM", "650 CFM"], ["Cordless", "Gas", "Corded", "Backpack"], ["Handheld", "Brushless", "Variable-Speed"]], []),
    ]),
    ("Storage", "storage", "ST", 8, "Tool boxes, shelving, bins, ladders, and workbenches.", [
        ("Tool Box", 19, 89, [["14in", "19in", "22in", "26in"], ["Plastic", "Steel", "Aluminum", "Rolling"]], []),
        ("Shelving Unit", 49, 199, [["36in W", "48in W", "72in W"], ["4-Shelf", "5-Shelf"], ["Steel", "Plastic", "Wire", "Wood"]], []),
        ("Storage Bin", 8, 30, [["6 qt", "18 gal", "27 gal", "40 gal"], ["Clear", "Black", "Heavy-Duty"], ["Latching", "Stackable"]], ["", "(4-Pack)"]),
        ("Pegboard Kit", 15, 50, [["2ft x 4ft", "4ft x 4ft", "4ft x 8ft"], ["Steel", "Hardboard", "Plastic"]], ["with 30 Hooks", "with 50 Hooks"]),
        ("Rolling Tool Chest", 149, 499, [["26in", "36in", "41in", "52in"], ["5-Drawer", "8-Drawer", "10-Drawer", "12-Drawer"]], []),
        ("Wall Hooks", 5, 20, [["Steel", "Vinyl-Coated"], ["Utility", "Bike", "Ladder", "Hose", "Double"]], ["(2-Pack)", "(4-Pack)", "(6-Pack)"]),
        ("Step Ladder", 59, 249, [["2-Step", "3-Step", "4ft", "6ft", "8ft"], ["Aluminum", "Fiberglass", "Steel"]], []),
        ("Workbench", 99, 349, [["48in", "60in", "72in"], ["Steel", "Hardwood-Top", "Adjustable-Height", "Folding"]], []),
    ]),
]

# Fictional brands, so no real trademarks appear in the data.
BRANDS = ["Ironclad", "Stanbury", "Keystone", "Ridgeway", "Copperline",
          "Northpeak", "Bluestem", "TrueForge", "Hartwell", "Granite & Co."]

SUPPLIERS = ["Northline Supply Co.", "Ironbridge Distributors", "Keystone Wholesale",
             "Bluewater Plumbing Supply", "Summit Electrical Supply", "Greenfield Garden Wholesale"]

# A few accented and apostrophe names exercise Unicode (NVARCHAR) and quoting.
FIRST_NAMES = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
               "David", "Elizabeth", "William", "Barbara", "Richard", "Susan", "Joseph", "Jessica",
               "Thomas", "Sarah", "Carlos", "Maria", "José", "Ana", "Wei", "Mei", "Aisha", "Omar",
               "Priya", "Arjun", "Zoë", "Chloé", "Liam", "Olivia", "Noah", "Emma", "Mateo", "Sofia",
               "Kenji", "Yuki", "Fatima", "Ali"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
              "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
              "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Nguyen", "Patel", "Kim",
              "Chen", "Singh", "Müller", "O'Brien", "Okafor", "Haddad", "Kowalski", "Rossi",
              "Silva", "Tanaka", "Ivanova", "Cohen"]
STREETS = ["Main", "Oak", "Maple", "Cedar", "Pine", "Elm", "Washington", "Lake", "Hill", "Park",
           "River", "Sunset", "Highland", "Willow", "Mill"]
STREET_SUFFIXES = ["St", "Ave", "Rd", "Ln", "Dr", "Ct", "Blvd", "Way"]

# (city, state, first three ZIP digits, relative population weight).
# The weights give the sales-by-state report a realistic, uneven shape.
LOCATIONS = [
    ("New York", "NY", "100", 8), ("Los Angeles", "CA", "900", 6), ("Chicago", "IL", "606", 4),
    ("Houston", "TX", "770", 4), ("Phoenix", "AZ", "850", 3), ("Philadelphia", "PA", "191", 3),
    ("San Antonio", "TX", "782", 2), ("San Diego", "CA", "921", 2), ("Dallas", "TX", "752", 2),
    ("Austin", "TX", "787", 2), ("Jacksonville", "FL", "322", 2), ("Columbus", "OH", "432", 2),
    ("Charlotte", "NC", "282", 2), ("Seattle", "WA", "981", 2), ("Denver", "CO", "802", 2),
    ("Boston", "MA", "021", 2), ("Nashville", "TN", "372", 2), ("Atlanta", "GA", "303", 2),
    ("Miami", "FL", "331", 2), ("Portland", "OR", "972", 1), ("Minneapolis", "MN", "554", 1),
    ("Detroit", "MI", "482", 1), ("Salt Lake City", "UT", "841", 1), ("Kansas City", "MO", "641", 1),
    ("Raleigh", "NC", "276", 1), ("Pittsburgh", "PA", "152", 1), ("Albuquerque", "NM", "871", 1),
    ("Omaha", "NE", "681", 1), ("Boise", "ID", "837", 1), ("Burlington", "VT", "054", 1),
]

# ===========================================================================
# Shape of the data (the "realism knobs")
# ===========================================================================

PRODUCT_POPULARITY_EXPONENT = 0.8   # weight = 1 / rank^0.8: a long tail of slow movers
CUSTOMER_PARETO_ALPHA = 2.0         # heavy-tailed order frequency per customer...
CUSTOMER_WEIGHT_CAP = 50.0          # ...capped so the busiest customer isn't absurd

# Seasonality multipliers. Hardware stores peak in spring (projects, gardens)
# and before the holidays; January and February are quiet.
MONTH_FACTOR = {1: 0.80, 2: 0.85, 3: 1.10, 4: 1.35, 5: 1.40, 6: 1.15,
                7: 1.00, 8: 0.95, 9: 0.95, 10: 1.00, 11: 1.25, 12: 1.45}
WEEKDAY_FACTOR = [1.00, 0.95, 0.95, 1.00, 1.10, 1.35, 1.25]  # Monday .. Sunday
GROWTH_OVER_HISTORY = 0.40          # the last day is 40% busier than the first

# Orders by hour of the shopper's local day: quiet overnight, busy daytime,
# peaks at lunch and in the evening. Shoppers are in the US, so local times
# are shifted by UTC_OFFSET_HOURS (roughly US Central) before storing in UTC.
HOUR_WEIGHTS = [0.15, 0.10, 0.08, 0.08, 0.10, 0.20, 0.40, 0.70, 1.00, 1.20, 1.30, 1.40,
                1.50, 1.40, 1.30, 1.30, 1.30, 1.40, 1.50, 1.60, 1.50, 1.20, 0.80, 0.40]
UTC_OFFSET_HOURS = 6

ITEMS_PER_ORDER = [(1, 45), (2, 28), (3, 14), (4, 6), (5, 3), (6, 2), (7, 1), (8, 1)]
QUANTITY_PER_ITEM = [(1, 70), (2, 17), (3, 6), (4, 3), (5, 2), (6, 2)]

# For the second and later items in an order: chance of picking a companion
# of an item already in the order, then of picking from the first item's aisle.
# This creates the "often bought together" pattern in the order history.
COMPANION_CHANCE = 0.35
SAME_AISLE_CHANCE = 0.30

PRICE_INFLATION_PER_YEAR = 0.03     # older orders were placed at slightly lower prices
FREE_SHIPPING_FROM_CENTS = 7500     # free shipping on orders of $75 or more
SHIPPING_CENTS = 799
PAYMENT_METHODS = [("card", 75), ("paypal", 20), ("gift_card", 5)]


# ===========================================================================
# Small helpers
# ===========================================================================

def cumulative(weights):
    """Running totals of weights, for fast weighted picks with bisect."""
    return list(itertools.accumulate(weights))


def weighted_index(rng, cum):
    """Pick an index with probability proportional to its weight."""
    return bisect.bisect_right(cum, rng.random() * cum[-1])


def weighted_choice(rng, pairs):
    """Pick a value from (value, weight) pairs."""
    values = [v for v, _ in pairs]
    return rng.choices(values, weights=[w for _, w in pairs], k=1)[0]


def money(cents):
    """Whole cents to an exact Decimal (2499 -> Decimal('24.99')). Never float."""
    return Decimal(cents).scaleb(-2)


def ascii_slug(text):
    """'Zoë O'Brien' -> 'zoeobrien': strip accents and anything not a-z."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", plain.lower())


def to_ms(moment):
    """Truncate a datetime to whole milliseconds.

    The columns are DATETIME2(3). With fast_executemany, pyodbc binds values
    at exactly that precision, and a value with extra microseconds fails with
    "Datetime field overflow". Every timestamp written goes through here.
    """
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def random_time_between(rng, start, end):
    """A random datetime in [start, end), to the millisecond."""
    span_ms = int((end - start).total_seconds() * 1000)
    return to_ms(start + timedelta(milliseconds=rng.randrange(max(span_ms, 1))))


# ===========================================================================
# Generators (pure Python, no database access)
# ===========================================================================

def generate_categories():
    rows = []
    for index, (name, slug, _prefix, _share, description, _items) in enumerate(CATEGORIES, start=1):
        rows.append((index, name, slug, description, index * 10))
    return rows


def name_pool(rng, noun, detail_lists, suffixes):
    """Every possible (name, brand, details) for one item, shuffled.

    brand x each detail list x suffix. Products take names from the end of
    this list, so each name is used at most once.
    """
    pool = []
    for brand in BRANDS:
        for details in itertools.product(*detail_lists):
            for suffix in suffixes or [""]:
                words = [brand, *details, noun] + ([suffix] if suffix else [])
                pool.append((" ".join(words), brand, " ".join([*details] + ([suffix] if suffix else []))))
    rng.shuffle(pool)
    return pool


def generate_products(rng, count, history_start, history_end):
    """Return product rows plus the lookups order generation needs.

    Names are unique: each item has a shuffled pool of all its possible names
    (name_pool), and every product takes the next unused one. If an item's
    pool runs out, another item in the same aisle is chosen.
    """
    pools = {(cat_index, item_index): name_pool(rng, noun, details, suffixes)
             for cat_index, category in enumerate(CATEGORIES, start=1)
             for item_index, (noun, _low, _high, details, suffixes) in enumerate(category[5])}

    # Split the product count across aisles by share, then shuffle so
    # product IDs aren't grouped by aisle.
    shares = [c[3] for c in CATEGORIES]
    per_category = [count * s // sum(shares) for s in shares]
    per_category[-1] += count - sum(per_category)
    category_of = [i for i, n in enumerate(per_category, start=1) for _ in range(n)]
    rng.shuffle(category_of)

    rows = []
    price_cents = [0] * (count + 1)        # index = product_id (index 0 unused)
    created_at = [None] * (count + 1)
    category = [0] * (count + 1)

    for product_id in range(1, count + 1):
        cat_id = category_of[product_id - 1]
        cat_name, _slug, prefix, _share, _desc, items = CATEGORIES[cat_id - 1]
        # Pick an item that still has unused names, then take its next name.
        available = [i for i in range(len(items)) if pools[(cat_id, i)]]
        if not available:
            sys.exit(f"[FAIL] Not enough unique names for {count:,} products in {cat_name}; "
                     "lower --products or add details to CATEGORIES.")
        item_index = rng.choice(available)
        noun, low, high, _details, _suffixes = items[item_index]
        name, brand, details = pools[(cat_id, item_index)].pop()

        # Skew toward the cheaper end of the range, then end in .99.
        dollars = low + (high - low) * rng.random() ** 1.5
        cents = max(int(dollars), 2) * 100 - 1

        # 85% of products existed before the history starts; the rest were
        # added over time, which makes the "newest" sort meaningful.
        if rng.random() < 0.85:
            created = to_ms(history_start - timedelta(days=rng.uniform(1, 730)))
        else:
            created = random_time_between(rng, history_start, history_end)

        price_cents[product_id] = cents
        created_at[product_id] = created
        category[product_id] = cat_id
        rows.append((
            product_id,
            f"{prefix}-{product_id:05d}",
            name,
            f"{noun} from {brand}: {details}. Stocked in the {cat_name} aisle.",
            cat_id,
            brand,
            money(cents),
            rng.random() > 0.01,           # about 1% discontinued (is_active = 0)
            created,
        ))
    return rows, price_cents, created_at, category


def generate_inventory(rng, product_count, end_dt):
    rows = []
    for product_id in range(1, product_count + 1):
        reorder_level = rng.randint(5, 25)
        roll = rng.random()
        if roll < 0.03:
            quantity = 0                                   # out of stock: checkout returns 409
        elif roll < 0.11:
            quantity = rng.randint(1, reorder_level)       # low stock: shows in the admin filter
        else:
            quantity = rng.randint(reorder_level + 1, 400)
        rows.append((product_id, quantity, reorder_level,
                     to_ms(end_dt - timedelta(hours=rng.uniform(1, 72)))))
    return rows


def generate_supplier_prices(rng, price_cents, end_dt):
    rows = []
    for product_id in range(1, len(price_cents)):
        for n, supplier in enumerate(rng.sample(SUPPLIERS, rng.randint(1, 3)), start=1):
            code = "".join(word[0] for word in supplier.split()[:2]).upper()
            rows.append((
                supplier,
                f"{code}-{product_id:05d}-{n}",
                product_id,
                money(int(price_cents[product_id] * rng.uniform(0.45, 0.75))),  # cost below selling price
                rng.randint(0, 2000),
                to_ms(end_dt - timedelta(hours=rng.uniform(1, 24))),
            ))
    return rows


def generate_customers(rng, count, history_start, history_end):
    rows = []
    ship_to = [None] * (count + 1)         # snapshot copied into each order
    created_at = [None] * (count + 1)
    location_cum = cumulative([loc[3] for loc in LOCATIONS])

    for customer_id in range(1, count + 1):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        city, state, zip3, _w = LOCATIONS[weighted_index(rng, location_cum)]
        address = f"{rng.randint(1, 9999)} {rng.choice(STREETS)} {rng.choice(STREET_SUFFIXES)}"
        postal = f"{zip3}{rng.randint(0, 99):02d}"

        # 60% were customers before the history starts; the rest sign up over
        # time, which is part of the gentle growth.
        if rng.random() < 0.60:
            created = to_ms(history_start - timedelta(days=rng.uniform(1, 1095)))
        else:
            created = random_time_between(rng, history_start, history_end)

        # customer_id in the email keeps it unique; example.com is reserved for examples.
        email = f"{ascii_slug(first)}.{ascii_slug(last)}{customer_id}@example.com"
        phone = f"555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}"

        ship_to[customer_id] = (f"{first} {last}", address, city, state, postal)
        created_at[customer_id] = created
        rows.append((customer_id, email, first, last, phone, address, city, state, postal, created))
    return rows, ship_to, created_at


class AvailablePicker:
    """Weighted random picks that respect "created before this order".

    A customer can't order before signing up, and a product can't be sold
    before it's added. Picks are retried a few times; if they keep landing on
    something too new, a pick from the always-available group is used.
    """

    def __init__(self, ids, weights, created_at, history_start):
        self.ids = ids
        self.cum = cumulative(weights)
        self.created_at = created_at
        founders = [(i, w) for i, w in zip(ids, weights) if created_at[i] < history_start]
        self.founder_ids = [i for i, _ in founders]
        self.founder_cum = cumulative([w for _, w in founders])

    def pick(self, rng, at):
        for _ in range(10):
            item = self.ids[weighted_index(rng, self.cum)]
            if self.created_at[item] <= at:
                return item
        return self.founder_ids[weighted_index(rng, self.founder_cum)]


def order_status(rng, age_days):
    """Status that fits the order's age: old orders delivered, new ones paid."""
    roll = rng.random()
    if roll < 0.03:
        return "cancelled"
    if roll < 0.045:
        return "payment_failed"
    if age_days < 2:
        return "paid"
    if age_days < 7:
        return "shipped" if rng.random() < 0.9 else "paid"
    if age_days < 14:
        return "delivered" if rng.random() < 0.6 else "shipped"
    return "delivered"


def order_timestamps(rng, count, history_start, history_end):
    """Sorted order timestamps following growth, seasons, weekdays, and hours."""
    days = (history_end - history_start).days
    day_weights = []
    for offset in range(days):
        day = history_start + timedelta(days=offset)
        growth = 1.0 + GROWTH_OVER_HISTORY * offset / days
        day_weights.append(growth * MONTH_FACTOR[day.month] * WEEKDAY_FACTOR[day.weekday()])
    day_cum = cumulative(day_weights)
    hour_cum = cumulative(HOUR_WEIGHTS)

    stamps = []
    for _ in range(count):
        day = history_start + timedelta(days=weighted_index(rng, day_cum))
        local = day + timedelta(hours=weighted_index(rng, hour_cum),
                                milliseconds=rng.randrange(3_600_000))
        stamp = local + timedelta(hours=UTC_OFFSET_HOURS)
        # The UTC shift can push the last evening's orders past the end of
        # the history; move those back one day so nothing is in the future.
        if stamp >= history_end:
            stamp -= timedelta(days=1)
        stamps.append(stamp)
    # Sorted, so order IDs increase with time, as they would in a real system.
    stamps.sort()
    return stamps


def generate_order_chunks(rng, args, history_start, history_end, products, customers):
    """Yield (orders, order_items, payments) rows, CHUNK_SIZE orders at a time."""
    price_cents, product_created, product_category, product_weights = products
    ship_to, customer_created, customer_weights = customers
    product_count = len(price_cents) - 1
    customer_count = len(ship_to) - 1

    product_ids = list(range(1, product_count + 1))
    all_products = AvailablePicker(product_ids, product_weights, product_created, history_start)

    by_aisle = {}
    for pid in product_ids:
        by_aisle.setdefault(product_category[pid], []).append(pid)
    aisle_pickers = {cat: AvailablePicker(pids, [product_weights[p - 1] for p in pids],
                                          product_created, history_start)
                     for cat, pids in sorted(by_aisle.items())}

    # Each product gets two companions from its aisle (a drill and its
    # battery, a faucet and its supply line). Picked once, up front.
    companions = [[]] + [rng.sample(by_aisle[product_category[pid]], 2) for pid in product_ids]

    customer_picker = AvailablePicker(list(range(1, customer_count + 1)), customer_weights,
                                      customer_created, history_start)

    stamps = order_timestamps(rng, args.orders, history_start, history_end)

    orders, items, payments = [], [], []
    for order_id, ordered_at in enumerate(stamps, start=1):
        customer_id = customer_picker.pick(rng, ordered_at)
        years_ago = (history_end - ordered_at).days / 365

        # Choose distinct products for this order.
        chosen = []
        for position in range(weighted_choice(rng, ITEMS_PER_ORDER)):
            for _ in range(10):
                roll = rng.random()
                if position > 0 and roll < COMPANION_CHANCE:
                    candidate = rng.choice(companions[rng.choice(chosen)])
                elif position > 0 and roll < COMPANION_CHANCE + SAME_AISLE_CHANCE:
                    candidate = aisle_pickers[product_category[chosen[0]]].pick(rng, ordered_at)
                else:
                    candidate = all_products.pick(rng, ordered_at)
                if candidate not in chosen and product_created[candidate] <= ordered_at:
                    chosen.append(candidate)
                    break

        # All money is summed in whole cents (integers), so the subtotal is
        # exactly the sum of quantity * unit_price stored on the items, and
        # total is exactly subtotal + shipping. verify.sql checks this.
        subtotal = 0
        for product_id in chosen:
            quantity = weighted_choice(rng, QUANTITY_PER_ITEM)
            unit_cents = round(price_cents[product_id] / (1 + PRICE_INFLATION_PER_YEAR * years_ago))
            subtotal += quantity * unit_cents
            items.append((order_id, product_id, quantity, money(unit_cents)))

        shipping = 0 if subtotal >= FREE_SHIPPING_FROM_CENTS else SHIPPING_CENTS
        status = order_status(rng, (history_end - ordered_at).days)
        # Delivered and shipped orders were last updated days after ordering.
        updated_at = to_ms(min(ordered_at + timedelta(days=rng.uniform(0, 7) if status in ("shipped", "delivered") else 0,
                                                      seconds=rng.randint(5, 60)),
                               history_end))
        name, address, city, state, postal = ship_to[customer_id]
        orders.append((order_id, customer_id, status, money(subtotal), money(shipping),
                       money(subtotal + shipping), name, address, city, state, postal,
                       ordered_at, updated_at))

        # Exactly one payment per historical order, for the order's total.
        # Failed and cancelled orders have a declined payment; every other
        # status was paid. In history, "cancelled" means the order was
        # abandoned at payment, so no money was ever taken.
        payments.append((
            order_id,
            money(subtotal + shipping),
            weighted_choice(rng, PAYMENT_METHODS),
            "declined" if status in ("payment_failed", "cancelled") else "approved",
            f"SIM-{rng.getrandbits(48):012x}",
            to_ms(ordered_at + timedelta(seconds=rng.uniform(1, 30))),
        ))

        if len(orders) == CHUNK_SIZE:
            yield orders, items, payments
            orders, items, payments = [], [], []

    if orders:
        yield orders, items, payments


# ===========================================================================
# Database
# ===========================================================================

def load_admin_login():
    """The SQL admin login from the root .env: (user, password).

    The user is SQL_ADMIN_USER, or "sa" if it's not set (local Docker). On
    prod-sql-01 it's the login created by the SQL IaaS extension, since Azure
    doesn't allow "sa" there. The password is SA_PASSWORD in both places.
    """
    if not ENV_FILE.exists():
        sys.exit(f"[FAIL] No .env file at {ENV_FILE}. Run: cp .env.example .env")
    load_dotenv(ENV_FILE)
    password = os.getenv("SA_PASSWORD")
    if not password:
        sys.exit("[FAIL] SA_PASSWORD is missing from .env")
    user = (os.getenv("SQL_ADMIN_USER") or "sa").strip()
    return user, password


def connect(admin_user, admin_password):
    """Connect to SalesDB as the SQL admin login. Transactions are committed per chunk."""
    # Braces make ODBC take the values literally; a "}" inside is doubled.
    user = admin_user.replace("}", "}}")
    password = admin_password.replace("}", "}}")
    conn_str = (
        f"DRIVER={{{ODBC_DRIVER}}};"
        f"SERVER={HOST},{SQL_PORT};"
        "DATABASE=SalesDB;"
        f"UID={{{user}}};"
        f"PWD={{{password}}};"
        "Encrypt=yes;"
        "TrustServerCertificate=yes;"
        "APP=ShopLite-seed;"
    )
    try:
        return pyodbc.connect(conn_str, timeout=LOGIN_TIMEOUT_SECONDS, autocommit=False)
    except pyodbc.Error as error:
        message = error.args[-1] if error.args else str(error)
        print(f"[FAIL] Could not connect to SalesDB: {message}")
        if "Cannot open database" in message:
            print("       Likely cause: SalesDB doesn't exist yet. Run: python shoplite/db/apply_schema.py")
        else:
            print("       Run 'python tools/check_local.py' for a diagnosis.")
        sys.exit(1)


def insert_sql(table, columns):
    # Table and column names are constants in this file, never user input,
    # so building the statement text is safe. Values always go through "?".
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"


def bulk_insert(conn, cursor, table, columns, rows, explicit_ids=False):
    """Insert rows in chunks, committing after each one."""
    if explicit_ids:
        # Lets us supply IDENTITY values ourselves, so child rows can refer
        # to known IDs. Only one table per session can have this ON at a time.
        cursor.execute(f"SET IDENTITY_INSERT {table} ON")
    sql = insert_sql(table, columns)
    for start in range(0, len(rows), CHUNK_SIZE):
        cursor.executemany(sql, rows[start:start + CHUNK_SIZE])
        conn.commit()
    if explicit_ids:
        cursor.execute(f"SET IDENTITY_INSERT {table} OFF")
    print(f"   {table}: {len(rows):,} rows")


ORDER_COLUMNS = ["order_id", "customer_id", "status", "subtotal", "shipping_cost", "total",
                 "ship_name", "ship_address_line1", "ship_city", "ship_state", "ship_postal_code",
                 "created_at", "updated_at"]
ORDER_ITEM_COLUMNS = ["order_id", "product_id", "quantity", "unit_price"]
PAYMENT_COLUMNS = ["order_id", "amount", "method", "status", "provider_reference", "created_at"]

# Tables whose IDs the seed supplies explicitly (IDENTITY_INSERT).
IDENTITY_TABLES = [("dbo.categories", "category_id"), ("dbo.products", "product_id"),
                   ("dbo.customers", "customer_id"), ("dbo.orders", "order_id")]

ALL_TABLES = ["dbo.categories", "dbo.products", "dbo.inventory", "dbo.customers", "dbo.carts",
              "dbo.cart_items", "dbo.orders", "dbo.order_items", "dbo.payments",
              "vendor.supplier_prices"]


# ===========================================================================
# Main
# ===========================================================================

def parse_args():
    parser = argparse.ArgumentParser(description="Generate and load ShopLite data into SalesDB.")
    parser.add_argument("--products", type=int, default=5000, help="number of products (default 5000)")
    parser.add_argument("--customers", type=int, default=50000, help="number of customers (default 50000)")
    parser.add_argument("--orders", type=int, default=200000, help="number of orders (default 200000)")
    parser.add_argument("--years", type=int, default=2, help="length of order history in years (default 2)")
    parser.add_argument("--seed", type=int, default=42, help="random seed (default 42)")
    parser.add_argument("--end-date", type=date.fromisoformat,
                        default=datetime.now(timezone.utc).date(),
                        help="last day of history, YYYY-MM-DD (default: today, UTC)")
    args = parser.parse_args()

    # 100 products guarantees every aisle gets several, which the
    # "bought together" companions need.
    if args.products < 100 or args.customers < 1 or args.orders < 1:
        parser.error("need at least 100 products, 1 customer, and 1 order")
    if not 1 <= args.years <= 10:
        parser.error("--years must be between 1 and 10")
    return args


def main():
    args = parse_args()
    rng = random.Random(args.seed)

    # Timestamps are naive datetimes that represent UTC (DATETIME2 has no time zone).
    history_end = datetime.combine(args.end_date, datetime.min.time())
    history_start = history_end - timedelta(days=365 * args.years)

    conn = connect(*load_admin_login())
    cursor = conn.cursor()
    # Sends each chunk to SQL Server as one array of parameters instead of
    # one round trip per row: the difference between seconds and hours.
    cursor.fast_executemany = True

    # Refuse to mix a new dataset into an existing one.
    existing = cursor.execute(
        "SELECT (SELECT COUNT(*) FROM dbo.categories) + (SELECT COUNT(*) FROM dbo.customers)"
        " + (SELECT COUNT(*) FROM dbo.orders);"
    ).fetchone()[0]
    if existing:
        print("[FAIL] SalesDB already has data. Run 'python shoplite/db/apply_schema.py' to start fresh.")
        return 1

    print(f"Seeding SalesDB: {args.products:,} products, {args.customers:,} customers, "
          f"{args.orders:,} orders over {args.years} years ending {args.end_date} (seed {args.seed}).")
    started = time.monotonic()

    print("-> Catalog")
    bulk_insert(conn, cursor, "dbo.categories",
                ["category_id", "name", "slug", "description", "sort_order"],
                generate_categories(), explicit_ids=True)
    product_rows, price_cents, product_created, product_category = generate_products(
        rng, args.products, history_start, history_end)
    bulk_insert(conn, cursor, "dbo.products",
                ["product_id", "sku", "name", "description", "category_id", "brand", "price",
                 "is_active", "created_at"],
                product_rows, explicit_ids=True)
    bulk_insert(conn, cursor, "dbo.inventory",
                ["product_id", "quantity_on_hand", "reorder_level", "updated_at"],
                generate_inventory(rng, args.products, history_end))
    bulk_insert(conn, cursor, "vendor.supplier_prices",
                ["supplier_name", "supplier_sku", "product_id", "unit_cost", "supplier_stock", "updated_at"],
                generate_supplier_prices(rng, price_cents, history_end))

    print("-> Customers")
    customer_rows, ship_to, customer_created = generate_customers(
        rng, args.customers, history_start, history_end)
    bulk_insert(conn, cursor, "dbo.customers",
                ["customer_id", "email", "first_name", "last_name", "phone", "address_line1",
                 "city", "state", "postal_code", "created_at"],
                customer_rows, explicit_ids=True)

    # Popularity weights: shuffle ranks so best sellers are spread across
    # product IDs, then weight = 1 / rank^exponent.
    ranks = list(range(1, args.products + 1))
    rng.shuffle(ranks)
    product_weights = [1 / r ** PRODUCT_POPULARITY_EXPONENT for r in ranks]
    customer_weights = [min(rng.paretovariate(CUSTOMER_PARETO_ALPHA), CUSTOMER_WEIGHT_CAP)
                        for _ in range(args.customers)]

    print("-> Orders, order items, payments")
    order_sql = insert_sql("dbo.orders", ORDER_COLUMNS)
    item_sql = insert_sql("dbo.order_items", ORDER_ITEM_COLUMNS)
    payment_sql = insert_sql("dbo.payments", PAYMENT_COLUMNS)
    cursor.execute("SET IDENTITY_INSERT dbo.orders ON")   # order_items and payments use their own IDENTITY

    loaded = 0
    chunks = generate_order_chunks(
        rng, args, history_start, history_end,
        (price_cents, product_created, product_category, product_weights),
        (ship_to, customer_created, customer_weights))
    for orders, items, payments in chunks:
        # Parents before children, so the foreign keys are satisfied.
        cursor.executemany(order_sql, orders)
        cursor.executemany(item_sql, items)
        cursor.executemany(payment_sql, payments)
        conn.commit()
        loaded += len(orders)
        print(f"   {loaded:,} / {args.orders:,} orders ({loaded * 100 // args.orders}%)"
              f"  {time.monotonic() - started:,.0f}s", flush=True)

    cursor.execute("SET IDENTITY_INSERT dbo.orders OFF")
    conn.commit()

    # New rows from the API must continue numbering after the seeded IDs.
    # Inserting explicit values under IDENTITY_INSERT already moves each
    # table's identity counter up to the highest value inserted; RESEED with
    # no value makes sure of it: if the counter is below MAX(id) it is raised
    # to MAX(id), otherwise it is left alone. Next API insert gets MAX(id) + 1.
    print("-> Checking identity counters")
    for table, id_column in IDENTITY_TABLES:
        cursor.execute(f"DBCC CHECKIDENT ('{table}', RESEED) WITH NO_INFOMSGS;")
        current, highest = cursor.execute(
            f"SELECT IDENT_CURRENT('{table}'), MAX({id_column}) FROM {table};").fetchone()
        print(f"   {table:<24} next id {int(current) + 1:>10,}  (max seeded {highest or 0:,})")
    conn.commit()

    # Fresh statistics give the optimizer accurate row counts and value
    # distributions for the skewed data just loaded. FULLSCAN reads every row
    # rather than a sample: slower, but exact, and this runs once.
    print("-> Updating statistics")
    for table in ALL_TABLES:
        cursor.execute(f"UPDATE STATISTICS {table} WITH FULLSCAN;")
    conn.commit()

    print("\nRow counts:")
    for table in ALL_TABLES:
        count = cursor.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0]
        print(f"   {table:<24} {count:>10,}")

    conn.close()
    print(f"\nDone in {time.monotonic() - started:,.0f}s. Next: run queries/verify.sql in VS Code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
