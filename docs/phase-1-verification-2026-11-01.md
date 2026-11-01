# Phase 1 verification — 1 November 2026

**Outcome: the offline foundation passes after the report-history correction.
Phase 1 live acceptance remains incomplete. Phase 2 can start against synthetic
fixtures; its calculation/strategy specification is not yet frozen.**

All ten requested tasks were assessed. Six are complete, three are blocked by
external/live evidence, and one has a concrete draft but remains partial. No live
market data, news or model calls were made. No provider was contacted or purchase
made. The owner confirmed there is no new account-specific permission evidence.

## Evidence and source identity

- Clean local clone of `6978c8dc12aa45f370e56ad26906cb2918daa814`: successful
  `uv sync --locked`, CLI startup, Ruff lint/format, and **593 passed, zero failed,
  errored or skipped**. Mandatory fresh-process reopen passed.
- Corrected code: **595 passed, zero failed, errored or skipped**, exact suite
  reconciliation and fresh-process reopen passed. The two additional cases cover
  Buy/Sell report-history filtering and preservation of genuinely missing reports.
- The corrected suite ran on the base commit plus an uncommitted patch. It is not
  represented as a clean committed release. The evidence manifest records the
  patch hash and matching SHA-256 hashes of all 73 tracked source/test/script/lock/
  project files in the tested clone and delivery workspace.
- Operator verification executed 38 CLI invocations across eight synthetic scan/
  recovery reports. Restore verification executed 12 further CLI invocations.
- PostgreSQL 18 ran in the isolated `phase1-audit-20261101` Compose project on
  localhost port 55440. Tests created their own disposable databases; operator
  checks used `phase1_operator_verified`; restore used `phase1_restored`.
  The existing application and user verification databases were not accessed.

Retained evidence:

- [Clean baseline release](evidence/phase-1-verification-2026-11-01/baseline-release.json)
  and [fresh-process reopen](evidence/phase-1-verification-2026-11-01/baseline-reopen.json).
- [Corrected release](evidence/phase-1-verification-2026-11-01/corrected-release.json)
  and [fresh-process reopen](evidence/phase-1-verification-2026-11-01/corrected-reopen.json).
- [Operator and restore summary](evidence/phase-1-verification-2026-11-01/operator-summary.json)
  and [database/filesystem restore evidence](evidence/phase-1-verification-2026-11-01/backup-restore.json).
- [Qualification preflight](evidence/phase-1-verification-2026-11-01/qualification-preflight.json),
  [blocked inventory attempt](evidence/phase-1-verification-2026-11-01/inventory-blocked.json),
  and [source/evidence hash manifest](evidence/phase-1-verification-2026-11-01/verification-manifest.json).

## Task outcomes

### 1. Clean verification environment — PASS

Installed Python 3.12 locked dependencies in a new clone and virtual environment.
Verified CLI help, isolated configuration and repeated database initialization.
Lint and formatting passed. Initial sandbox package downloads were denied; the
authorized download succeeded. Local database tests used authorized access to the
isolated server. These environment retries were not application failures.

### 2. Complete automated acceptance — PASS

Both the 593-case baseline and 595-case corrected suite passed without skips.
The runner independently reconciled expected and executed tests and validated
three fresh-process report reopen records. Coverage includes migrations, market
isolation, calendars, routing, fractional volume, cache corrections, ledger
concurrency, partial sales, splits/corrections, report failures and resume.
These tests establish fixture behavior, not live provider quality or profitability.

### 3. Buy selection and reporting — PASS AFTER FIX

Exercised `valid` (10 securities), `shortfall` (3), `zero` (0),
`one-stock-failure` (10, PARTIAL), and `unassessable` (10, PARTIAL).
Reports retained the selected instruments and explicit non-success outcomes.
Indicators, setups, debate and verdict remained NOT_RUN. All five reports
regenerated without hash changes. Frozen universe replay matched across processes.

Confirmed the reported history defect: internal stock and benchmark runs were
listed as incomplete reports. History now excludes runs referenced in Buy/Sell
`child_run_ids` or `shared_context` before applying its limit. Standalone and
combined scans with genuinely missing artifacts remain visible. No stored rows,
snapshots or reports were removed. The operator run produced exactly five history
rows, all SAVED, after five Buy scenarios. Regression cases include both Buy and
Sell references, benchmark children, older parents, limiting and missing reports.

### 4. Ledger and Sell workflow — PASS

Declined a synthetic buy and confirmed no ledger change; confirmed buy 10 at
USD 100 and sale 4 at USD 110 with no fees. Actual result: quantity 6, basis
USD 600, realized P&L USD 40. Replaying the buy key changed nothing. Selling 7
remaining shares was rejected without changing portfolio revision or quantities.
Receipts reopened exactly. Valid Sell succeeded; the one-holding failure produced
a saved FAILED report, correctly reflecting that every holding in that one-stock
portfolio failed. Scans/resume did not change holdings. The full suite additionally
covers empty/larger portfolios, full closes, lots, currencies, split/correction
atomicity and competing sell writes.

### 5. Recovery and backup restoration — PASS

The full-suite fresh-process checklist injected and persisted an interruption,
then resumed against the original inputs. Completed-run regeneration/resume
preserved hashes. A failed-stage retry produced a new report generation while its
previous files remained byte-identical; it did not change the ledger.

The operator drill obstructed a report destination with a regular file. Collection
persisted successfully, publication failed visibly, and repairing the original
destination allowed regeneration and reopening without collecting data again.
Changing the configured reports directory alone does not move already recorded
artifact paths. The audit harness was corrected to respect this recovery contract.

Backed up PostgreSQL plus runtime files/configuration with no operator writers.
Restored into a new database; moved the original runtime aside and copied backup
files to their recorded absolute paths. **539 files matched their hashes**. All
eight reports reopened, portfolio state/revision matched exactly, and both
execution receipts matched exactly. Original runtime and backup were retained.

### 6. Provider permissions — BLOCKED

The owner confirmed no new evidence. Nine required collect/retain/derive
dispositions for Massive reference/prices and Yahoo classification remain UNKNOWN.
The existing [provider request drafts](provider-permissions.md) remain available.
No new rights grants were inferred from credentials or successful past requests.

### 7. Complete live Technology universe — BLOCKED

Ran a fresh offline plan and attempted the bounded inventory stage using the
exact generated manifest hash. Inventory stopped with RIGHTS_NOT_PERMITTED:
**zero Massive requests, zero Yahoo requests, zero SDK calls**. Its origin label
is `live` because that was the requested mode; no live observation occurred.
No complete inventory, classification, verified identity population or measured
cold/warm workload was obtained. The default two-page plan is not a full-sector
budget. Enrichment and broad collection were not dispatched through this blocker.

### 8. History/publication and live acceptance — BLOCKED

The fresh release evaluation still reports LIVE_SMOKE_NOT_OBSERVED and
PILOT_SECTOR_COVERAGE_NOT_QUALIFIED. Calendar/feed, actual account provenance,
candidate/benchmark coverage and workload remain unqualified. The native Massive
adapter still deliberately records PUBLICATION_UNVERIFIED. Repeated downloads
cannot establish observed publication timing without a measured, versioned policy.
No live smoke/history measurement was invoked while permissions remain unknown.
Synthetic calendar, routing, history, failure and qualification contracts passed
in the full suite; they are not substitutes for these live observations.

### 9. Phase 2 specification — PARTIAL; REVIEW DRAFT READY

Prepared the [Phase 2 handoff](phase-2-handoff.md): registry/input contracts,
candidate formula conventions, causal detector rules, independent daily/weekly
requirements, missing-data behavior, Buy/Sell separation, proposed golden values,
and an ordered implementation/review sequence. No new indicator package was
selected or pinned, and golden fixtures were not executed against a Phase 2 engine.
Parameters, mandatory features, detector thresholds, evidence caps, earnings policy
and package selection require explicit design work with the owner before freezing.
No accepted strategy or completed Phase 2 implementation is claimed.

### 10. Acceptance record and handoff — COMPLETE; MILESTONE OPEN

Recorded these outcomes and immutable evidence. Updated local architecture and
implementation-plan status to distinguish implemented PostgreSQL foundation from
future technical-analysis and agent work. Documented report-history/recovery
behavior in the release runbook. Changes belong on the existing feature branch
`chore/phase-1-acceptance`, submitted as a draft PR for review without merging.
The release milestone remains open because Tasks 6–8 are blocked; Task 9 remains
an owner review draft rather than a frozen implementation contract.

## Reproduction and remaining work

From a clean checkout with Python 3.12 and a disposable PostgreSQL 18 server,
export `STOCK_ANALYSER_TEST_DATABASE_URL` and follow
[the foundation release runbook](foundation-release.md#reproduce-from-a-clean-checkout).
Use the current qualification preflight as explicit `--qualification-evidence`;
the fixture runner's zero exit does not mean the live milestone passed.

Local raw logs and JUnit files are under `/private/tmp/phase1-audit-baseline`,
`/private/tmp/phase1-audit-final`, and
`/private/tmp/phase1-audit-operator-verified`. The database dump and file backup are
under `/private/tmp/phase1-audit-backup`. These temporary paths are not permanent
archives; committed summaries and hashes above preserve the observed outcomes.
The one-off operator/backup scripts are local verification harnesses, not shipped
application commands. Early harness assertions were corrected for the existing
schema-version field, CLI exit codes, report generations and persisted destinations;
the final 38-call run and subsequent 12-call restore run passed in fresh scratch DBs.

Next actions: obtain applicable provider evidence, then complete the reviewed live
qualification stages; separately review the Phase 2 handoff with the owner and
freeze one small calculation contract at a time. Begin deterministic work with
synthetic fixtures while maintaining the visible live-data acceptance blockers.
