# Billing Reconciler

A command-line checker that audits a billing database. It recomputes every invoice from the
raw usage records with its **own, independent implementation** of the billing rules and reports
anything that doesn't add up: wrong invoices, duplicate usage, unbilled usage and unusual spikes.

It reads the schema of the companion
[usage rating & invoicing service](https://github.com/Somesh-Kennedy/billing-service), and it is
read-only: it never changes the database it audits.

## What it checks
| Check | Finds |
|---|---|
| `invoice_mismatch` | An invoice whose total differs from the total recomputed from usage and the plan |
| `invoice_arithmetic` | An invoice that contradicts itself (subtotal is not the sum of its lines, or total is not subtotal + tax) |
| `duplicate_usage` | Same customer, kind, quantity and timestamp recorded under different event ids |
| `unbilled_usage` | Usage in a closed period with no invoice |
| `usage_anomaly` | A month far above the customer's own history (modified z-score on median/MAD, and at least 2x the median) |

Why an independent re-implementation: it uses exact fractions and integer cents and shares no
code with the service. When the two agree, two different implementations of the rules reached
the same number.

## Run it
```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt        # Windows; .venv/bin/pip elsewhere

# audit any billing database (SQLite or PostgreSQL)
.venv/Scripts/python -m reconciler --database-url postgresql+psycopg2://user:pass@host:5432/billing \
    --json findings.json --csv findings.csv --fail-on-findings
```
`--fail-on-findings` exits with code 1 when anything is found, so it can gate a CI job or a nightly run.

Try it without a real database:
```bash
.venv/Scripts/python -m reconciler.seed --database-url sqlite:///./synthetic.db --customers 200
.venv/Scripts/python -m reconciler --database-url sqlite:///./synthetic.db
```
The generator is deterministic (same `--seed`, same data) and plants known defects, writing the
answer key to `ground_truth.json`.

## Results
- **Synthetic data with planted defects:** 2,000 customers, 214,405 usage events, 11,960 invoices,
  240 planted defects. All 240 were found, with 0 false alarms (precision 1.00, recall 1.00).
  The whole audit ran in about 2 seconds on SQLite. This is synthetic data that I generated alongside
  the checks, so it shows the checks work as designed, not how they would perform on real production data.
- **Against the real service on PostgreSQL 16:** the service was driven through its API with
  1,625 usage events (plus 156 retried duplicates) and 120 invoices. The reconciler agreed with
  every invoice (0 findings). After deliberately corrupting a copy of that database (an invoice
  total off by one cent, an overcharged invoice, a deleted invoice, a duplicated event), it
  reported all four problems.

## Tests
```bash
.venv/Scripts/python -m pytest --cov=reconciler --cov-report=term-missing
```
29 tests: the rating rules against hand calculations (rounding, exact money conversion), each
check against a tiny hand-built database, and an end-to-end test that plants defects and compares
the findings with the answer key, including a clean database that must report nothing.

## Limitations
- Anomaly detection needs at least 4 months of history per customer and kind, and only flags upward spikes.
- It assumes one flat tax rate (`--tax-rate`) and the service's rating rules.
- "Closed period" means any period not later than the newest invoiced period.
- The PostgreSQL check against the live service was run by hand; the automated tests use SQLite.
