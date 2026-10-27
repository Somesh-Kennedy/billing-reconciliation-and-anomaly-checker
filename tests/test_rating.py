from fractions import Fraction

import pytest

from reconciler.rating import cents_half_up, expected_invoice, to_cents, to_fraction

PLAN = dict(
    monthly_fee="40.00", included_minutes=300, included_data_mb=5000, included_sms=100,
    overage_per_minute="0.10", overage_per_mb="0.02", overage_per_sms="0.05",
)
TAX = Fraction("0.0825")


def test_no_usage_is_fee_plus_tax():
    want = expected_invoice(PLAN, {}, TAX)
    assert (want.subtotal_cents, want.tax_cents, want.total_cents) == (4000, 330, 4330)


def test_usage_at_allowance_is_free():
    want = expected_invoice(PLAN, {"voice": 300, "data": 5000, "sms": 100}, TAX)
    assert want.line_cents == (4000,)


def test_overage_on_all_kinds_matches_hand_calculation():
    want = expected_invoice(PLAN, {"voice": 320, "data": 5100, "sms": 130}, TAX)
    assert want.line_cents == (4000, 200, 200, 150)
    assert (want.subtotal_cents, want.tax_cents, want.total_cents) == (4550, 375, 4925)


@pytest.mark.parametrize("value, cents", [("0.005", 1), ("0.004", 0), ("1.005", 101), ("2.675", 268), ("0.025", 3)])
def test_cents_round_half_up(value, cents):
    assert cents_half_up(Fraction(value)) == cents


def test_conversion_from_sqlite_floats_is_exact_for_money():
    # SQLite hands back floats; 0.1 must not turn into 0.1000000000000000055...
    assert to_fraction(0.1) == Fraction(1, 10)
    assert to_cents(43.3) == 4330
