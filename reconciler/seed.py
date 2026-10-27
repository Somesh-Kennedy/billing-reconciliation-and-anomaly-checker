"""Synthetic billing database with planted defects and a ground-truth answer key.

Schema names match the usage rating & invoicing service, so the checker runs
unchanged against either this data or that service's database.

The generator is deterministic: the same --seed always produces the same data.
"""
import argparse
import json
import random
from datetime import datetime
from fractions import Fraction

from sqlalchemy import (Column, DateTime, ForeignKey, Integer, MetaData, Numeric, String, Table,
                        create_engine, delete, insert, select, update)

from .rating import cents_half_up, expected_invoice, to_cents

metadata = MetaData()
plans = Table(
    "plans", metadata,
    Column("id", Integer, primary_key=True), Column("name", String(100), unique=True),
    Column("monthly_fee", Numeric(10, 2)), Column("included_minutes", Integer),
    Column("included_data_mb", Integer), Column("included_sms", Integer),
    Column("overage_per_minute", Numeric(10, 4)), Column("overage_per_mb", Numeric(10, 4)),
    Column("overage_per_sms", Numeric(10, 4)),
)
customers = Table(
    "customers", metadata,
    Column("id", Integer, primary_key=True), Column("name", String(200)),
    Column("email", String(320), unique=True), Column("plan_id", ForeignKey("plans.id")),
)
usage_events = Table(
    "usage_events", metadata,
    Column("id", Integer, primary_key=True), Column("event_id", String(100), unique=True),
    Column("customer_id", ForeignKey("customers.id")), Column("kind", String(10)),
    Column("quantity", Integer), Column("occurred_at", DateTime),
)
invoices = Table(
    "invoices", metadata,
    Column("id", Integer, primary_key=True), Column("customer_id", ForeignKey("customers.id")),
    Column("period", String(7)), Column("subtotal", Numeric(12, 2)),
    Column("tax", Numeric(12, 2)), Column("total", Numeric(12, 2)),
)
invoice_lines = Table(
    "invoice_lines", metadata,
    Column("id", Integer, primary_key=True), Column("invoice_id", ForeignKey("invoices.id")),
    Column("description", String(200)), Column("quantity", Integer),
    Column("unit_price", Numeric(10, 4)), Column("amount", Numeric(12, 2)),
)

PERIODS = ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
TAX_RATE = Fraction("0.0825")
PLANS = [
    dict(name="Basic", monthly_fee="25.00", included_minutes=200, included_data_mb=2000, included_sms=50,
         overage_per_minute="0.15", overage_per_mb="0.03", overage_per_sms="0.08"),
    dict(name="Standard", monthly_fee="40.00", included_minutes=300, included_data_mb=5000, included_sms=100,
         overage_per_minute="0.10", overage_per_mb="0.02", overage_per_sms="0.05"),
    dict(name="Unlimited", monthly_fee="70.00", included_minutes=1000, included_data_mb=20000, included_sms=500,
         overage_per_minute="0.05", overage_per_mb="0.01", overage_per_sms="0.02"),
]
# typical monthly usage (voice minutes, data MB, sms) relative to each plan's allowance
USAGE_SCALE = {"voice": (0.9, "included_minutes"), "data": (0.9, "included_data_mb"), "sms": (0.8, "included_sms")}


def _cents(amount_cents: int):
    from decimal import Decimal
    return Decimal(amount_cents) / 100


def generate(database_url: str, n_customers: int = 200, seed: int = 42, defects_per_type: int = 6) -> dict:
    """Create tables, fill them, plant defects. Returns the ground truth."""
    rng = random.Random(seed)
    engine = create_engine(database_url)
    metadata.drop_all(engine)
    metadata.create_all(engine)

    with engine.begin() as conn:
        for index, plan in enumerate(PLANS, start=1):
            conn.execute(insert(plans).values(id=index, **plan))
        plan_rows = {index: plan for index, plan in enumerate(PLANS, start=1)}
        customer_plan = {}
        for cid in range(1, n_customers + 1):
            customer_plan[cid] = rng.randint(1, len(PLANS))
            conn.execute(insert(customers).values(
                id=cid, name=f"Customer {cid}", email=f"c{cid}@example.com", plan_id=customer_plan[cid]))

        # choose disjoint (customer, period) targets for each defect type
        slots = [(c, p) for c in range(1, n_customers + 1) for p in PERIODS[1:-1]]  # interior months only
        rng.shuffle(slots)
        take = lambda: [slots.pop() for _ in range(defects_per_type)]  # noqa: E731
        spike_slots, dup_slots, overcharge_slots, arith_slots, unbilled_slots = (take() for _ in range(5))

        # ---- usage ----------------------------------------------------------------
        events, event_no = [], 0

        def add_event(cid, kind, qty, when):
            nonlocal event_no
            event_no += 1
            events.append(dict(event_id=f"evt-{event_no}", customer_id=cid, kind=kind, quantity=qty, occurred_at=when))

        baseline = {}
        for cid in range(1, n_customers + 1):
            plan = plan_rows[customer_plan[cid]]
            for kind, (scale, allowance_col) in USAGE_SCALE.items():
                baseline[(cid, kind)] = max(10, int(plan[allowance_col] * scale * rng.uniform(0.6, 1.5)))
        spike_keys = {(c, p) for c, p in spike_slots}
        for cid in range(1, n_customers + 1):
            for period in PERIODS:
                year, month = map(int, period.split("-"))
                for kind in USAGE_SCALE:
                    month_total = int(baseline[(cid, kind)] * rng.uniform(0.85, 1.15))
                    if (cid, period) in spike_keys and kind == "voice":
                        month_total *= 6  # planted spike
                    pieces = rng.randint(4, 8)
                    cuts = sorted(rng.randint(1, max(1, month_total - 1)) for _ in range(pieces - 1))
                    parts = [b - a for a, b in zip([0] + cuts, cuts + [month_total]) if b - a > 0]
                    for qty in parts:
                        when = datetime(year, month, rng.randint(1, 28), rng.randint(0, 23), rng.randint(0, 59),
                                        rng.randint(0, 59))
                        add_event(cid, kind, qty, when)
        # planted duplicates: same customer/kind/quantity/timestamp, new event_id
        for cid, period in dup_slots:
            original = next(e for e in events
                            if e["customer_id"] == cid and e["occurred_at"].strftime("%Y-%m") == period)
            add_event(cid, original["kind"], original["quantity"], original["occurred_at"])
        conn.execute(insert(usage_events), events)

        # ---- invoices (generated honestly from the data above) ----------------------
        totals = {}
        for e in events:
            key = (e["customer_id"], e["occurred_at"].strftime("%Y-%m"))
            totals.setdefault(key, {}).setdefault(e["kind"], 0)
            totals[key][e["kind"]] += e["quantity"]
        invoice_ids = {}
        for cid in range(1, n_customers + 1):
            for period in PERIODS:
                want = expected_invoice(plan_rows[customer_plan[cid]], totals.get((cid, period), {}), TAX_RATE)
                invoice_id = len(invoice_ids) + 1
                invoice_ids[(cid, period)] = invoice_id
                conn.execute(insert(invoices).values(
                    id=invoice_id, customer_id=cid, period=period, subtotal=_cents(want.subtotal_cents),
                    tax=_cents(want.tax_cents), total=_cents(want.total_cents)))
                conn.execute(insert(invoice_lines), [
                    dict(invoice_id=invoice_id, description=f"line {i}", quantity=1,
                         unit_price=_cents(c), amount=_cents(c)) for i, c in enumerate(want.line_cents)])

        # ---- defects planted AFTER invoicing ----------------------------------------
        for cid, period in overcharge_slots:  # pricing error: self-consistent invoice, $5.00 too high
            invoice_id = invoice_ids[(cid, period)]
            subtotal_cents = to_cents(conn.execute(
                select(invoices.c.subtotal).where(invoices.c.id == invoice_id)).scalar_one()) + 500
            tax_cents = cents_half_up(Fraction(subtotal_cents, 100) * TAX_RATE)
            conn.execute(update(invoices).where(invoices.c.id == invoice_id).values(
                subtotal=_cents(subtotal_cents), tax=_cents(tax_cents), total=_cents(subtotal_cents + tax_cents)))
            first_line = conn.execute(
                select(invoice_lines.c.id).where(invoice_lines.c.invoice_id == invoice_id)
                .order_by(invoice_lines.c.id).limit(1)).scalar_one()
            conn.execute(update(invoice_lines).where(invoice_lines.c.id == first_line).values(
                amount=invoice_lines.c.amount + _cents(500)))
        for cid, period in arith_slots:  # invoice total is off by a cent from its own parts
            invoice_id = invoice_ids[(cid, period)]
            conn.execute(update(invoices).where(invoices.c.id == invoice_id).values(total=invoices.c.total + _cents(1)))
        for cid, period in unbilled_slots:  # invoice never issued
            invoice_id = invoice_ids[(cid, period)]
            conn.execute(delete(invoice_lines).where(invoice_lines.c.invoice_id == invoice_id))
            conn.execute(delete(invoices).where(invoices.c.id == invoice_id))

    findings = (
        [("usage_anomaly", c, p) for c, p in spike_slots]
        + [("duplicate_usage", c, p) for c, p in dup_slots]
        + [("invoice_mismatch", c, p) for c, p in overcharge_slots]
        # a total off by one cent breaks the invoice's arithmetic AND differs from the recomputed total
        + [("invoice_arithmetic", c, p) for c, p in arith_slots]
        + [("invoice_mismatch", c, p) for c, p in arith_slots]
        + [("unbilled_usage", c, p) for c, p in unbilled_slots]
    )
    engine.dispose()
    return {
        "customers": n_customers, "periods": PERIODS, "usage_events": len(events),
        "invoices": len(invoice_ids) - len(unbilled_slots), "seed": seed,
        "planted": sorted([list(f) for f in findings]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a synthetic billing database with planted defects")
    parser.add_argument("--database-url", default="sqlite:///./synthetic.db")
    parser.add_argument("--customers", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--defects-per-type", type=int, default=6)
    parser.add_argument("--truth", default="ground_truth.json", help="where to write the answer key")
    args = parser.parse_args()
    truth = generate(args.database_url, args.customers, args.seed, args.defects_per_type)
    with open(args.truth, "w", encoding="utf8") as handle:
        json.dump(truth, handle, indent=2)
    print(f"{truth['usage_events']} usage events, {truth['invoices']} invoices, "
          f"{len(truth['planted'])} planted defects -> {args.truth}")


if __name__ == "__main__":
    main()
