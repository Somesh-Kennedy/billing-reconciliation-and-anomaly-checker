# Business Requirements: Billing Reconciler

**Version:** 1.0  **Status:** Implemented and tested  **Owner:** Somesh Kennedy

## 1. Purpose
Billing errors cost money and customer trust. Finance and billing operations need an automated
second opinion on invoices: a way to confirm that what was billed matches the usage that was
recorded, and to catch data problems before customers do.

## 2. Scope
**In scope:** read-only audit of a billing database; recomputation of invoices from raw usage;
detection of duplicate usage, unbilled usage and unusual usage; a human-readable and machine-readable report.
**Out of scope:** correcting data, payments, authentication, a web dashboard, scheduling.

## 3. Actors
| Actor | Needs |
|---|---|
| Billing analyst | A clear list of what is wrong, for which customer and period, and why |
| Engineer / CI job | A non-zero exit code when problems exist, and a file to attach to a ticket |

## 4. Functional requirements
| ID | Requirement |
|---|---|
| FR-1 | Recompute each invoice's total from usage and plan terms and report any difference (`invoice_mismatch`). |
| FR-2 | Report invoices that contradict themselves (`invoice_arithmetic`). |
| FR-3 | Report usage events that are identical except for their event id (`duplicate_usage`). |
| FR-4 | Report usage in a closed period that has no invoice (`unbilled_usage`). |
| FR-5 | Report months whose usage is far above the customer's own history (`usage_anomaly`). |
| FR-6 | Print a summary, and optionally write all findings to JSON and CSV. |
| FR-7 | Exit with code 1 when findings exist and `--fail-on-findings` is set. |

## 5. Business rules
| ID | Rule |
|---|---|
| BR-1 | Recomputation uses exact arithmetic (no floating point) with half-up rounding to cents. |
| BR-2 | Usage up to a plan's allowance is free; each unit above it is charged at the overage rate. |
| BR-3 | A billing month is the half-open interval [first of the month, first of the next month) in UTC. |
| BR-4 | A period is closed if it is not later than the newest invoiced period. |
| BR-5 | An anomaly needs at least 4 months of history, a modified z-score above 3.5, and usage at least double the median. |
| BR-6 | The checker never writes to the audited database. |

## 6. Acceptance criteria
1. On a database with no problems, the report is empty and the exit code is 0.
2. An invoice $5 too high, an invoice one cent off, a missing invoice, a duplicated event and a
   sixfold usage spike are each reported under the correct check.
3. On the synthetic benchmark (2,000 customers, 240 planted defects) every planted defect is
   found and nothing else is reported.
4. The same seed always generates identical data.
5. Against the live billing service on PostgreSQL, a correct database reports no findings.
