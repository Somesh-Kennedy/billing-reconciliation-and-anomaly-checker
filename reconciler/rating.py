"""Independent re-rating of a billing period.

This deliberately shares no code with the billing service. It uses exact
fractions and integer cents, so if the service and this module agree, two
different implementations of the rules arrived at the same number.
"""
from dataclasses import dataclass
from fractions import Fraction
from math import floor

KINDS = (
    # (usage kind, plan allowance column, plan overage-rate column)
    ("voice", "included_minutes", "overage_per_minute"),
    ("data", "included_data_mb", "overage_per_mb"),
    ("sms", "included_sms", "overage_per_sms"),
)


def to_fraction(value) -> Fraction:
    """Exact conversion from a DB value (Decimal, float from SQLite, int or str)."""
    return Fraction(str(value))


def cents_half_up(amount: Fraction) -> int:
    """Whole cents, rounding halves up: floor(x * 100 + 1/2)."""
    return floor(amount * 100 + Fraction(1, 2))


@dataclass(frozen=True)
class ExpectedInvoice:
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    line_cents: tuple[int, ...]


def expected_invoice(plan: dict, usage: dict[str, int], tax_rate: Fraction) -> ExpectedInvoice:
    lines = [cents_half_up(to_fraction(plan["monthly_fee"]))]
    for kind, allowance_col, rate_col in KINDS:
        over = max(0, usage.get(kind, 0) - int(plan[allowance_col]))
        if over:
            lines.append(cents_half_up(over * to_fraction(plan[rate_col])))
    subtotal = sum(lines)
    tax = cents_half_up(Fraction(subtotal, 100) * tax_rate)
    return ExpectedInvoice(subtotal, tax, subtotal + tax, tuple(lines))


def to_cents(value) -> int:
    """A stored money value (already in cents precision) as integer cents."""
    return cents_half_up(to_fraction(value))
