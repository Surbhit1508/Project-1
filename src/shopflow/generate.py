"""Synthetic "source systems" for an online retailer.

Writes the files an upstream OLTP export / CDC tool would drop into a landing zone:

* ``landing/products/products.csv``                 - product catalogue snapshot
* ``landing/customers/customers_<YYYY-MM-DD>.csv``  - daily customer CDC (signups, tier upgrades,
  address moves, no-op re-syncs)
* ``landing/orders/orders_<YYYY-MM-DD>.jsonl``      - daily order events with nested line items

Customer behaviour is simulated (not random noise) so the downstream analytics and the churn
model have real signal to find: every customer has a latent purchase rate and lifetime, and
engagement fades before they churn. On top of that, realistic data problems are injected:
duplicate deliveries, late-arriving events, status updates (returns), unparseable timestamps,
missing keys, negative quantities, unknown products and inconsistent casing.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

CATEGORIES: dict[str, tuple[float, float, float]] = {
    # category: (min price, max price, share of catalogue)
    "electronics": (25, 900, 0.14),
    "home": (12, 250, 0.18),
    "apparel": (10, 150, 0.20),
    "beauty": (6, 70, 0.14),
    "sports": (15, 300, 0.12),
    "books": (6, 45, 0.12),
    "grocery": (3, 35, 0.10),
}
NOUNS = {
    "electronics": ["Headphones", "Speaker", "Charger", "Smartwatch", "Keyboard", "Monitor", "Drone"],
    "home": ["Lamp", "Cookware Set", "Throw Blanket", "Kettle", "Vase", "Storage Box", "Rug"],
    "apparel": ["Hoodie", "Sneakers", "Denim Jacket", "T-Shirt", "Rain Coat", "Chinos", "Beanie"],
    "beauty": ["Face Serum", "Lip Balm", "Shampoo", "Sunscreen", "Perfume", "Night Cream"],
    "sports": ["Yoga Mat", "Dumbbells", "Running Belt", "Tennis Racket", "Water Bottle", "Bike Helmet"],
    "books": ["Cookbook", "Thriller", "Sci-Fi Novel", "Biography", "Data Science Handbook"],
    "grocery": ["Coffee Beans", "Green Tea", "Granola", "Olive Oil", "Dark Chocolate", "Honey"],
}
ADJECTIVES = ["Classic", "Pro", "Eco", "Smart", "Compact", "Deluxe", "Essential", "Ultra", "Nordic"]
FIRST_NAMES = ["alex", "sam", "jordan", "priya", "li", "maria", "omar", "emma", "noah", "aisha",
               "lucas", "mia", "arjun", "sofia", "ethan", "zara", "leo", "chloe", "ravi", "nina"]
LAST_NAMES = ["smith", "patel", "garcia", "mueller", "chen", "kumar", "brown", "martin", "silva",
              "nguyen", "jones", "rossi", "khan", "dubois", "taylor", "kim", "wilson", "singh"]
EMAIL_DOMAINS = ["example.com", "mail.test", "inbox.test", "post.example"]

COUNTRIES = (["US", "GB", "DE", "IN", "CA", "AU", "FR"], [0.42, 0.15, 0.12, 0.11, 0.09, 0.06, 0.05])
ACQ_CHANNELS = (
    ["organic", "paid_search", "social", "referral", "email"],
    [0.32, 0.25, 0.20, 0.13, 0.10],
)
CHANNEL_RATE = {"organic": 1.0, "paid_search": 0.85, "social": 0.7, "referral": 1.3, "email": 1.15}
CHANNEL_LIFE = {"organic": 1.0, "paid_search": 0.75, "social": 0.6, "referral": 1.5, "email": 1.2}
CHANNEL_LOYAL = {"organic": 0.18, "paid_search": 0.12, "social": 0.08, "referral": 0.30, "email": 0.22}
DEVICES = ["web", "ios", "android"]
PAYMENTS = (["card", "paypal", "apple_pay", "gift_card"], [0.55, 0.25, 0.15, 0.05])
PROMO_CODES = {"SAVE15": 0.15, "VIP20": 0.20, "FLASH25": 0.25}
WELCOME_CODE = ("WELCOME10", 0.10)
TIERS = [("bronze", 0.0), ("silver", 400.0), ("gold", 1500.0)]
SEASONALITY = {1: 0.8, 2: 0.85, 3: 0.95, 4: 0.95, 5: 1.0, 6: 0.95,
               7: 0.9, 8: 0.95, 9: 1.0, 10: 1.05, 11: 1.35, 12: 1.5}
# Churn mechanics: per-order hazard multipliers after a bad experience, and how much a
# customer's purchase rate drops while they are "fading" before they stop buying.
RETURN_CHURN_MULT = 6.0
CANCEL_CHURN_MULT = 3.0
FADE_RATE_MULT = 0.15
FREE_SHIPPING_OVER = 50.0
SHIPPING_FEE = 4.99


@dataclass(frozen=True)
class GeneratorConfig:
    n_customers: int = 4000
    n_products: int = 150
    start: date = date(2024, 1, 1)
    end: date = date(2025, 6, 30)
    seed: int = 7
    dirty: bool = True


@dataclass
class GenerationSummary:
    customers: int = 0
    products: int = 0
    orders: int = 0
    order_events: int = 0
    order_files: int = 0
    customer_files: int = 0
    customer_events: int = 0
    injected: Counter = field(default_factory=Counter)


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def _make_products(rng: np.random.Generator, n: int) -> tuple[list[dict], np.ndarray]:
    cats = list(CATEGORIES)
    shares = np.array([CATEGORIES[c][2] for c in cats])
    rows = []
    for i in range(n):
        cat = str(rng.choice(cats, p=shares / shares.sum()))
        lo, hi, _ = CATEGORIES[cat]
        price = math.floor(float(np.exp(rng.uniform(np.log(lo), np.log(hi))))) + 0.99
        rows.append(
            {
                "product_id": f"P{i + 1:04d}",
                "product_name": f"{rng.choice(ADJECTIVES)} {rng.choice(NOUNS[cat])}",
                "category": cat,
                "unit_price": round(price, 2),
                "unit_cost": round(price * rng.uniform(0.40, 0.75), 2),
            }
        )
    popularity = rng.pareto(1.3, n) + 1.0  # a few best-sellers, a long tail
    return rows, popularity


def _make_customers(rng: np.random.Generator, cfg: GeneratorConfig) -> list[dict]:
    span = (cfg.end - cfg.start).days - 30
    # Signups accelerate over time (growing business).
    offsets = np.sort((rng.uniform(0, 1, cfg.n_customers) ** 0.8 * span).astype(int))
    customers = []
    for i, off in enumerate(offsets):
        chan = str(rng.choice(ACQ_CHANNELS[0], p=ACQ_CHANNELS[1]))
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        loyal = rng.random() < CHANNEL_LOYAL[chan]
        discount_p = float(rng.beta(1.5, 6))
        customers.append(
            {
                "customer_id": f"C{i + 1:06d}",
                "first_name": first.title(),
                "last_name": last.title(),
                "email": f"{first}.{last}{rng.integers(1, 999)}@{rng.choice(EMAIL_DOMAINS)}",
                "country": str(rng.choice(COUNTRIES[0], p=COUNTRIES[1])),
                "acquisition_channel": chan,
                "birth_year": int(rng.integers(1955, 2007)),
                "signup_date": cfg.start + timedelta(days=int(off)),
                # latent behaviour (never exported)
                "_rate": float(np.clip(rng.lognormal(np.log(1.1 * CHANNEL_RATE[chan]), 0.85),
                                       0.15, 6.0)) / 30.0,
                "_return_p": float(rng.beta(1.2, 14)),
                "_discount_p": discount_p,
                # per-order probability of deciding to leave
                "_hazard": (0.012 if loyal else 0.085 / CHANNEL_LIFE[chan]) * (1 + 2.5 * discount_p),
                "_cat_pref": rng.dirichlet(np.full(len(CATEGORIES), 0.6)),
                "_device_pref": rng.dirichlet(np.ones(len(DEVICES))),
            }
        )
    return customers


def _simulate_orders(
    rng: np.random.Generator,
    cfg: GeneratorConfig,
    customers: list[dict],
    products: list[dict],
    popularity: np.ndarray,
) -> list[dict]:
    """Event simulation of each customer's purchase history.

    After every order the customer may decide to leave. The hazard grows with bad experiences
    (a return or cancellation), with deal-seeking, and shrinks for loyal / long-standing
    customers. A customer who decides to leave does not vanish instantly: their purchase rate
    drops for a "fading" period first - the behavioural signal a churn model has to pick up.
    """
    end_dt = datetime.combine(cfg.end, datetime.max.time())
    cats = list(CATEGORIES)
    by_cat: dict[str, list[int]] = defaultdict(list)
    for idx, p in enumerate(products):
        by_cat[p["category"]].append(idx)
    cat_weights = {
        c: popularity[ix] / popularity[ix].sum() for c, ix in ((c, np.array(v)) for c, v in by_cat.items())
    }

    orders = []
    for cust in customers:
        signup = datetime.combine(cust["signup_date"], datetime.min.time())
        rate = cust["_rate"]
        t = signup + timedelta(hours=float(rng.exponential(72)))
        fading_until: datetime | None = None
        n_placed = 0
        while t < end_dt and (fading_until is None or t < fading_until):
            if rng.random() < SEASONALITY[t.month] / 1.5:
                ts = t.replace(hour=int(rng.choice(24, p=_HOUR_P)), minute=int(rng.integers(60)),
                               second=int(rng.integers(60)))
                order = _make_order(rng, cust, ts, n_placed, products, cats, by_cat, cat_weights)
                orders.append(order)
                n_placed += 1
                if fading_until is None:
                    hazard = cust["_hazard"]
                    hazard *= RETURN_CHURN_MULT if order["_will_return"] else 1.0
                    hazard *= CANCEL_CHURN_MULT if order["status"] == "cancelled" else 1.0
                    hazard *= 0.6 if n_placed >= 6 else 1.0
                    if rng.random() < min(hazard, 0.9):
                        fading_until = ts + timedelta(days=float(rng.uniform(20, 120)))
                        rate *= FADE_RATE_MULT
            t += timedelta(days=float(rng.exponential(1 / (rate * 1.5))))
    orders.sort(key=lambda o: o["order_ts"])
    for i, o in enumerate(orders):
        o["order_id"] = f"O{i + 1:08d}"
    return orders


_HOUR_P = np.array([1, 1, 1, 1, 1, 2, 3, 5, 6, 7, 7, 8, 9, 8, 7, 7, 7, 8, 9, 10, 10, 9, 6, 3],
                   dtype=float)
_HOUR_P /= _HOUR_P.sum()


def _make_order(rng, cust, ts, n_placed, products, cats, by_cat, cat_weights) -> dict:
    if n_placed == 0 and rng.random() < 0.5:
        code, disc = WELCOME_CODE
    elif rng.random() < cust["_discount_p"] * 0.6:
        code = str(rng.choice(list(PROMO_CODES)))
        disc = PROMO_CODES[code]
    else:
        code, disc = None, 0.0
    items = []
    for _ in range(min(1 + rng.poisson(0.7), 6)):
        cat = cats[int(rng.choice(len(cats), p=cust["_cat_pref"]))]
        p = products[int(rng.choice(by_cat[cat], p=cat_weights[cat]))] if by_cat[cat] else \
            products[int(rng.integers(len(products)))]
        items.append({"product_id": p["product_id"], "quantity": int(1 + rng.poisson(0.25)),
                      "unit_price": round(p["unit_price"] * (1 - disc), 2)})
    subtotal = sum(i["quantity"] * i["unit_price"] for i in items)
    status = "cancelled" if rng.random() < 0.03 else "completed"
    return {
        "customer_id": cust["customer_id"],
        "order_ts": ts,
        "status": status,
        "channel": DEVICES[int(rng.choice(len(DEVICES), p=cust["_device_pref"]))],
        "payment_method": str(rng.choice(PAYMENTS[0], p=PAYMENTS[1])),
        "discount_code": code,
        "currency": "USD",
        "shipping_fee": 0.0 if subtotal >= FREE_SHIPPING_OVER else SHIPPING_FEE,
        "items": items,
        "_will_return": status == "completed" and rng.random() < cust["_return_p"],
        "_return_after": int(rng.integers(3, 26)),
    }


def _customer_events(rng, cfg, customers, orders) -> list[tuple[datetime, dict]]:
    """CDC stream for the customer table: signup, tier upgrades, moves, no-op re-syncs."""
    exported = ["customer_id", "first_name", "last_name", "email", "country",
                "acquisition_channel", "birth_year", "signup_date"]
    state = {c["customer_id"]: {k: c[k] for k in exported} | {"loyalty_tier": "bronze"}
             for c in customers}
    events: list[tuple[datetime, dict]] = []

    def emit(cid: str, when: datetime, **changes) -> None:
        state[cid].update(changes)
        events.append((when, dict(state[cid])))

    for c in customers:
        emit(c["customer_id"], datetime.combine(c["signup_date"], datetime.min.time()))

    spend: dict[str, float] = defaultdict(float)
    for o in orders:
        if o["status"] != "completed":
            continue
        cid = o["customer_id"]
        spend[cid] += sum(i["quantity"] * i["unit_price"] for i in o["items"])
        new_tier = [t for t, threshold in TIERS if spend[cid] >= threshold][-1]
        if new_tier != state[cid]["loyalty_tier"]:
            emit(cid, o["order_ts"] + timedelta(seconds=1), loyalty_tier=new_tier)

    horizon = (cfg.end - cfg.start).days
    for c in customers:
        start_off = (c["signup_date"] - cfg.start).days
        if start_off >= horizon - 2:
            continue
        if rng.random() < 0.03:  # customer moved country
            when = datetime.combine(cfg.start + timedelta(days=int(rng.integers(start_off + 1, horizon))),
                                    datetime.min.time()) + timedelta(hours=int(rng.integers(24)))
            current = state[c["customer_id"]]["country"]
            new_country = str(rng.choice([x for x in COUNTRIES[0] if x != current]))
            emit(c["customer_id"], when, country=new_country)
        if rng.random() < 0.05:  # CRM re-sync that changes nothing
            when = datetime.combine(cfg.start + timedelta(days=int(rng.integers(start_off + 1, horizon))),
                                    datetime.min.time()) + timedelta(hours=int(rng.integers(24)))
            emit(c["customer_id"], when)
    events.sort(key=lambda e: e[0])
    return events


def generate(landing: Path, cfg: GeneratorConfig | None = None) -> GenerationSummary:
    """Generate the landing zone. Deterministic for a given config."""
    cfg = cfg or GeneratorConfig()
    rng = np.random.default_rng(cfg.seed)
    summary = GenerationSummary()

    products, popularity = _make_products(rng, cfg.n_products)
    customers = _make_customers(rng, cfg)
    orders = _simulate_orders(rng, cfg, customers, products, popularity)
    cust_events = _customer_events(rng, cfg, customers, orders)

    # ---- products snapshot -------------------------------------------------------------
    prod_dir = landing / "products"
    prod_dir.mkdir(parents=True, exist_ok=True)
    prod_rows = list(products)
    if cfg.dirty:  # the export job double-wrote one row
        prod_rows.append(dict(products[3]))
        summary.injected["duplicate_product_row"] += 1
    _write_csv(prod_dir / "products.csv", prod_rows)

    # ---- customer CDC ------------------------------------------------------------------
    cust_by_day: dict[date, list[dict]] = defaultdict(list)
    for when, row in cust_events:
        row = dict(row, updated_at=_ts(when))
        if cfg.dirty and rng.random() < 0.02:
            row["email"] = row["email"].upper()
            summary.injected["uppercase_email"] += 1
        cust_by_day[when.date()].append(row)
    cust_dir = landing / "customers"
    cust_dir.mkdir(parents=True, exist_ok=True)
    for day, rows in sorted(cust_by_day.items()):
        _write_csv(cust_dir / f"customers_{day.isoformat()}.csv", rows)

    # ---- order events ------------------------------------------------------------------
    files: dict[date, list[dict]] = defaultdict(list)

    def deliver(record: dict, day: date) -> None:
        files[min(day, cfg.end)].append(record)

    for o in orders:
        rec = {
            "order_id": o["order_id"],
            "customer_id": o["customer_id"],
            "order_ts": _ts(o["order_ts"]),
            "updated_at": _ts(o["order_ts"]),
            "status": o["status"],
            "channel": o["channel"],
            "payment_method": o["payment_method"],
            "discount_code": o["discount_code"],
            "currency": o["currency"],
            "shipping_fee": o["shipping_fee"],
            "items": [dict(i) for i in o["items"]],
        }
        day = o["order_ts"].date()
        if cfg.dirty:
            r = rng.random()
            if r < 0.0015:
                rec["customer_id"] = None
                summary.injected["missing_customer_id"] += 1
            elif r < 0.0020:
                rec["order_ts"] = "N/A"
                summary.injected["unparseable_order_ts"] += 1
            elif r < 0.0040:
                rec["items"][0]["quantity"] = -1
                summary.injected["negative_quantity"] += 1
            elif r < 0.0050:
                rec["items"][-1]["product_id"] = "P9999"
                summary.injected["unknown_product"] += 1
            elif r < 0.0150:
                rec["status"] = rec["status"].upper() + " "
                summary.injected["messy_status_casing"] += 1
            if rng.random() < 0.02:
                day += timedelta(days=int(rng.integers(1, 4)))
                summary.injected["late_arriving_order"] += 1
        deliver(rec, day)
        if cfg.dirty and rng.random() < 0.01:
            dup_day = day + timedelta(days=int(rng.integers(0, 2)))
            deliver(json.loads(json.dumps(rec)), dup_day)
            summary.injected["duplicate_delivery"] += 1
        if o["_will_return"]:
            returned_at = o["order_ts"] + timedelta(days=o["_return_after"])
            if returned_at.date() <= cfg.end:
                upd = dict(rec, status="returned", updated_at=_ts(returned_at))
                upd["items"] = [dict(i) for i in rec["items"]]
                deliver(upd, returned_at.date())
                summary.injected["status_update_returned"] += 1

    order_dir = landing / "orders"
    order_dir.mkdir(parents=True, exist_ok=True)
    for day, recs in sorted(files.items()):
        with open(order_dir / f"orders_{day.isoformat()}.jsonl", "w", encoding="utf-8") as fh:
            for rec in recs:
                fh.write(json.dumps(rec) + "\n")

    summary.customers = len(customers)
    summary.products = len(products)
    summary.orders = len(orders)
    summary.order_events = sum(len(v) for v in files.values())
    summary.order_files = len(files)
    summary.customer_files = len(cust_by_day)
    summary.customer_events = len(cust_events)
    return summary


def _write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
