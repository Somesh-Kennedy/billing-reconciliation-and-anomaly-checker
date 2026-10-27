import argparse
import csv
import json
import sys
from collections import Counter
from fractions import Fraction

from sqlalchemy import create_engine

from .checks import run_all


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="reconciler",
        description="Reconcile a billing database: recompute invoices from raw usage and flag problems.",
    )
    parser.add_argument("--database-url", required=True, help="e.g. sqlite:///billing.db or postgresql+psycopg2://...")
    parser.add_argument("--tax-rate", default="0.0825", help="tax rate the invoices were issued with")
    parser.add_argument("--json", dest="json_path", help="write findings to this JSON file")
    parser.add_argument("--csv", dest="csv_path", help="write findings to this CSV file")
    parser.add_argument("--fail-on-findings", action="store_true", help="exit with code 1 if anything is found (for CI)")
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url)
    try:
        with engine.connect() as conn:
            findings = run_all(conn, Fraction(args.tax_rate))
    finally:
        engine.dispose()
    findings.sort(key=lambda f: (f.check, f.customer_id, f.period))

    counts = Counter(f.check for f in findings)
    print(f"{len(findings)} finding(s)")
    for check, n in sorted(counts.items()):
        print(f"  {check}: {n}")
    for f in findings[:20]:
        print(f"  - [{f.check}] customer {f.customer_id}, {f.period}: {f.detail}")
    if len(findings) > 20:
        print(f"  ... {len(findings) - 20} more (use --json or --csv for the full list)")

    rows = [vars(f) for f in findings]
    if args.json_path:
        with open(args.json_path, "w", encoding="utf8") as handle:
            json.dump(rows, handle, indent=2)
    if args.csv_path:
        with open(args.csv_path, "w", newline="", encoding="utf8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["check", "customer_id", "period", "detail"])
            writer.writeheader()
            writer.writerows(rows)
    return 1 if (findings and args.fail_on_findings) else 0


if __name__ == "__main__":
    sys.exit(main())
