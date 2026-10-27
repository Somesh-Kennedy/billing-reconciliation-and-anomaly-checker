"""The reconciliation checks.

Every check reads straight from the billing database with plain SQL and returns
a list of Finding objects. Nothing is modified.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from statistics import median

from sqlalchemy import text

from .rating import expected_invoice, to_cents


@dataclass(frozen=True)
class Finding:
    check: str
    customer_id: int
    period: str
    detail: str

    def key(self) -> tuple[str, int, str]:
        return (self.check, self.customer_id, self.period)


def _as_datetime(value) -> datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _period(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


def load_usage_totals(conn) -> dict[tuple[int, str], dict[str, int]]:
    """{(customer_id, 'YYYY-MM'): {kind: total}} in a single pass over usage_events."""
    totals: dict[tuple[int, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for customer_id, kind, quantity, occurred_at in conn.execute(
        text("SELECT customer_id, kind, quantity, occurred_at FROM usage_events")
    ):
        totals[(customer_id, _period(_as_datetime(occurred_at)))][kind] += quantity
    return totals


def check_invoices(conn, tax_rate: Fraction, totals=None) -> list[Finding]:
    """Recompute every invoice from raw usage; also test its internal arithmetic."""
    totals = totals if totals is not None else load_usage_totals(conn)
    plans = {row["id"]: dict(row) for row in conn.execute(text("SELECT * FROM plans")).mappings()}
    plan_of = dict(conn.execute(text("SELECT id, plan_id FROM customers")).all())
    line_sums: dict[int, int] = defaultdict(int)
    for invoice_id, amount in conn.execute(text("SELECT invoice_id, amount FROM invoice_lines")):
        line_sums[invoice_id] += to_cents(amount)

    findings = []
    for inv in conn.execute(text("SELECT id, customer_id, period, subtotal, tax, total FROM invoices")).mappings():
        subtotal, tax, total = to_cents(inv["subtotal"]), to_cents(inv["tax"]), to_cents(inv["total"])
        where = (inv["customer_id"], inv["period"])

        if subtotal != line_sums[inv["id"]] or total != subtotal + tax:
            findings.append(Finding(
                "invoice_arithmetic", *where,
                f"invoice {inv['id']}: subtotal {subtotal / 100:.2f} vs lines {line_sums[inv['id']] / 100:.2f}, "
                f"total {total / 100:.2f} vs subtotal+tax {(subtotal + tax) / 100:.2f}",
            ))

        plan = plans.get(plan_of.get(inv["customer_id"]))
        if plan is None:
            continue
        want = expected_invoice(plan, dict(totals.get(where, {})), tax_rate)
        if total != want.total_cents:
            findings.append(Finding(
                "invoice_mismatch", *where,
                f"invoice {inv['id']}: billed {total / 100:.2f}, recomputed from usage {want.total_cents / 100:.2f}",
            ))
    return findings


def check_duplicate_usage(conn) -> list[Finding]:
    """Same customer, kind, quantity and timestamp under different event_ids."""
    rows = conn.execute(text(
        "SELECT customer_id, kind, quantity, occurred_at, COUNT(*) AS n, MIN(event_id) AS first_id "
        "FROM usage_events GROUP BY customer_id, kind, quantity, occurred_at HAVING COUNT(*) > 1"
    )).mappings()
    return [
        Finding("duplicate_usage", r["customer_id"], _period(_as_datetime(r["occurred_at"])),
                f"{r['n']} events with identical {r['kind']} {r['quantity']} at {r['occurred_at']} "
                f"(e.g. {r['first_id']})")
        for r in rows
    ]


def check_unbilled_usage(conn, totals=None) -> list[Finding]:
    """Usage exists for a closed period but the customer has no invoice for it.

    A period counts as closed if it is not later than the newest invoiced period.
    """
    totals = totals if totals is not None else load_usage_totals(conn)
    invoiced = {(c, p) for c, p in conn.execute(text("SELECT customer_id, period FROM invoices"))}
    if not invoiced:
        return []
    latest = max(period for _, period in invoiced)
    return [
        Finding("unbilled_usage", customer_id, period, f"usage {dict(kinds)} but no invoice")
        for (customer_id, period), kinds in sorted(totals.items())
        if period <= latest and (customer_id, period) not in invoiced
    ]


def check_usage_anomalies(conn, totals=None, z_threshold: float = 3.5, min_months: int = 4) -> list[Finding]:
    """Flag a month whose usage is far above the customer's own history.

    Robust test: modified z-score (median/MAD) above the threshold AND at least
    double the median, so ordinary month-to-month wobble is not reported.
    """
    totals = totals if totals is not None else load_usage_totals(conn)
    series: dict[tuple[int, str], list[tuple[str, int]]] = defaultdict(list)
    for (customer_id, period), kinds in totals.items():
        for kind, quantity in kinds.items():
            series[(customer_id, kind)].append((period, quantity))

    findings = []
    for (customer_id, kind), points in sorted(series.items()):
        if len(points) < min_months:
            continue
        values = [q for _, q in points]
        mid = median(values)
        mad = median(abs(v - mid) for v in values)
        for period, quantity in sorted(points):
            if quantity < 2 * mid:
                continue
            spike = (0.6745 * (quantity - mid) / mad > z_threshold) if mad else (quantity > 3 * mid)
            if spike:
                findings.append(Finding(
                    "usage_anomaly", customer_id, period,
                    f"{kind} {quantity} vs typical {mid:g}",
                ))
    return findings


def run_all(conn, tax_rate: Fraction) -> list[Finding]:
    totals = load_usage_totals(conn)
    return (
        check_invoices(conn, tax_rate, totals)
        + check_duplicate_usage(conn)
        + check_unbilled_usage(conn, totals)
        + check_usage_anomalies(conn, totals)
    )
