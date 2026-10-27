"""Each check against a tiny hand-built database where the right answer is obvious."""
from datetime import datetime
from decimal import Decimal
from fractions import Fraction

import pytest
from sqlalchemy import create_engine, insert

from reconciler.checks import (check_duplicate_usage, check_invoices, check_unbilled_usage,
                               check_usage_anomalies, run_all)
from reconciler.seed import customers, invoice_lines, invoices, metadata, plans, usage_events

TAX = Fraction("0.0825")


@pytest.fixture()
def conn():
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(insert(plans).values(
            id=1, name="Standard", monthly_fee=Decimal("40.00"), included_minutes=300, included_data_mb=5000,
            included_sms=100, overage_per_minute=Decimal("0.10"), overage_per_mb=Decimal("0.02"),
            overage_per_sms=Decimal("0.05")))
        c.execute(insert(customers).values(id=1, name="Ana", email="a@example.com", plan_id=1))
    with engine.connect() as c:
        yield c
    engine.dispose()


def add_usage(conn, event_id, qty, when, kind="voice", customer_id=1):
    conn.execute(insert(usage_events).values(
        event_id=event_id, customer_id=customer_id, kind=kind, quantity=qty, occurred_at=when))


def add_invoice(conn, period, subtotal, tax, total, line_amounts=None):
    invoice_id = len(conn.execute(invoices.select()).fetchall()) + 1
    conn.execute(insert(invoices).values(
        id=invoice_id, customer_id=1, period=period,
        subtotal=Decimal(subtotal), tax=Decimal(tax), total=Decimal(total)))
    for amount in line_amounts or [subtotal]:
        conn.execute(insert(invoice_lines).values(
            invoice_id=invoice_id, description="line", quantity=1, unit_price=Decimal(amount), amount=Decimal(amount)))


# ---- invoice recomputation -----------------------------------------------------

def test_correct_invoice_has_no_findings(conn):
    add_usage(conn, "e1", 320, datetime(2026, 10, 5))
    add_invoice(conn, "2026-10", "42.00", "3.47", "45.47", ["40.00", "2.00"])
    assert check_invoices(conn, TAX) == []


def test_overcharged_invoice_is_a_mismatch(conn):
    add_usage(conn, "e1", 320, datetime(2026, 10, 5))
    add_invoice(conn, "2026-10", "47.00", "3.88", "50.88", ["45.00", "2.00"])  # consistent, but $5 too high
    findings = check_invoices(conn, TAX)
    assert [f.check for f in findings] == ["invoice_mismatch"]
    assert "50.88" in findings[0].detail and "45.47" in findings[0].detail


def test_total_that_disagrees_with_subtotal_plus_tax_is_an_arithmetic_error(conn):
    add_usage(conn, "e1", 320, datetime(2026, 10, 5))
    add_invoice(conn, "2026-10", "42.00", "3.47", "45.48", ["40.00", "2.00"])
    assert {f.check for f in check_invoices(conn, TAX)} == {"invoice_arithmetic", "invoice_mismatch"}


def test_subtotal_that_disagrees_with_its_lines_is_an_arithmetic_error(conn):
    add_usage(conn, "e1", 320, datetime(2026, 10, 5))
    add_invoice(conn, "2026-10", "42.00", "3.47", "45.47", ["40.00", "1.00"])
    assert [f.check for f in check_invoices(conn, TAX)] == ["invoice_arithmetic"]


def test_month_boundary_matches_the_service_rules(conn):
    # 23:59:59 on Oct 31 is October; 00:00:00 on Nov 1 is November
    add_usage(conn, "last", 400, datetime(2026, 10, 31, 23, 59, 59))
    add_usage(conn, "first", 400, datetime(2026, 11, 1, 0, 0, 0))
    add_invoice(conn, "2026-10", "50.00", "4.13", "54.13", ["40.00", "10.00"])
    add_invoice(conn, "2026-11", "50.00", "4.13", "54.13", ["40.00", "10.00"])
    assert check_invoices(conn, TAX) == []


# ---- duplicates ----------------------------------------------------------------

def test_identical_events_under_different_ids_are_flagged(conn):
    add_usage(conn, "e1", 50, datetime(2026, 10, 5, 12, 0, 0))
    add_usage(conn, "e2", 50, datetime(2026, 10, 5, 12, 0, 0))
    add_usage(conn, "e3", 50, datetime(2026, 10, 5, 12, 0, 1))  # one second later: not a duplicate
    findings = check_duplicate_usage(conn)
    assert [(f.check, f.period) for f in findings] == [("duplicate_usage", "2026-10")]


def test_different_kinds_at_the_same_time_are_not_duplicates(conn):
    add_usage(conn, "e1", 50, datetime(2026, 10, 5), kind="voice")
    add_usage(conn, "e2", 50, datetime(2026, 10, 5), kind="data")
    assert check_duplicate_usage(conn) == []


# ---- unbilled ------------------------------------------------------------------

def test_usage_in_a_closed_period_without_an_invoice_is_flagged(conn):
    add_usage(conn, "e1", 10, datetime(2026, 9, 5))
    add_usage(conn, "e2", 10, datetime(2026, 10, 5))
    add_invoice(conn, "2026-10", "40.00", "3.30", "43.30")
    findings = check_unbilled_usage(conn)
    assert [(f.check, f.period) for f in findings] == [("unbilled_usage", "2026-09")]


def test_the_still_open_period_is_not_flagged(conn):
    add_usage(conn, "e1", 10, datetime(2026, 10, 5))
    add_usage(conn, "e2", 10, datetime(2026, 11, 5))  # newer than the latest invoice: still open
    add_invoice(conn, "2026-10", "40.00", "3.30", "43.30")
    assert check_unbilled_usage(conn) == []


def test_no_invoices_at_all_means_nothing_is_closed(conn):
    add_usage(conn, "e1", 10, datetime(2026, 10, 5))
    assert check_unbilled_usage(conn) == []


# ---- anomalies -----------------------------------------------------------------

def add_months(conn, quantities):
    for month, qty in enumerate(quantities, start=1):
        add_usage(conn, f"m{month}", qty, datetime(2026, month, 10))


def test_a_sixfold_spike_is_flagged(conn):
    add_months(conn, [100, 110, 95, 105, 600, 100])
    findings = check_usage_anomalies(conn)
    assert [(f.check, f.period) for f in findings] == [("usage_anomaly", "2026-05")]


def test_normal_wobble_is_not_flagged(conn):
    add_months(conn, [100, 120, 90, 110, 130, 95])
    assert check_usage_anomalies(conn) == []


def test_too_little_history_is_not_judged(conn):
    add_months(conn, [100, 100, 900])
    assert check_usage_anomalies(conn) == []


def test_flat_history_then_big_jump_is_flagged_even_with_zero_spread(conn):
    add_months(conn, [100, 100, 100, 100, 400])  # MAD is 0, so the ratio rule applies
    assert [f.period for f in check_usage_anomalies(conn)] == ["2026-05"]


def test_run_all_combines_every_check(conn):
    add_usage(conn, "e1", 50, datetime(2026, 10, 5, 1))
    add_usage(conn, "e2", 50, datetime(2026, 10, 5, 1))
    add_invoice(conn, "2026-10", "40.00", "3.30", "43.30")
    assert {f.check for f in run_all(conn, TAX)} == {"duplicate_usage"}
