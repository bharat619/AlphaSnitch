# Foundation release verification (US-018)

This foundation collects and preserves data-readiness evidence and records confirmed
offline executions. Indicators, setups, debate and verdict remain `NOT_RUN`.
Passing fixtures establishes reproducibility of the implemented contracts, not
recommendation capability or production reliability. US is the only public market.

The [2026-10-24 clean-checkout acceptance evidence](evidence/foundation-release-2026-10-24.json)
records **472 passed, zero skipped/failures/errors** at source `536b127` with a clean
tracked tree after successful `uv sync --locked` into a new local clone. The
[fresh-process reopen evidence](evidence/foundation-reopen-2026-10-24.json) retains
the three fixture runs and artifact hashes. Ruff lint and formatting checks passed.
Live checks were not invoked for this record; pilot coverage remains failed.

### Bounded live observation (2026-10-24)

After the fixture-only record above, an explicitly opt-in bounded run observed the
AAPL/SPY smoke with two history snapshots per symbol and complete requested history.
Publication timing remains unverified. The run enumerated only two pages from each
sector source, so both enumerations were truncated; 685 reference candidates and 500
discovery candidates are lower bounds, not complete populations. Of the bounded
sample, exact symbol reconciliation found 149 matches and 107 eligible identity
matches. Classification completeness, broader history/liquidity, and retention
rights remain unverified. The pilot gate therefore remains **FAIL** and the release
milestone remains incomplete.

The [live release record](evidence/foundation-live-2026-10-24.json) captures the
fixture result, AAPL/SPY observation, pilot gate, and source hash. Its referenced
[sector findings](evidence/technology-universe-live-2026-10-24.json) retain
allowlisted aggregate metadata and page hashes, without raw provider rows or page
cursors. These observations do not grant full-sector acceptance.
The recorded acceptance also verifies all 472 expected test IDs and successful
mandatory fresh-process evidence, including its SHA-256 hash.

**Release milestone remains blocked by pilot-sector coverage.** The existing
[observed qualification](evidence/technology-universe-2026-10-24.json) fails
classification completeness, cross-source identity mapping, history/liquidity
measurement and retention-rights qualification. The current qualifier intentionally
cannot grant full-sector acceptance. AAPL/SPY smoke success and synthetic top-ten
selection cannot clear these blockers. No paid provider upgrade is part of this release.

## Reproduce from a clean checkout

Use Python 3.12, uv, Docker Compose and PostgreSQL 18. Run from the checkout root:

```sh
uv python install 3.12
uv sync --locked
cp .env.example .env
# Edit .env with a local database password and matching connection URL.
docker compose up -d --wait
uv run --locked stock-analyser --help
uv run --locked stock-analyser doctor
uv run --locked stock-analyser init
```

Export `STOCK_ANALYSER_TEST_DATABASE_URL` with a connection URL to your **disposable
local test server**, using the `.env.example` template. Its role needs `CREATEDB`;
each integration test creates and drops a random database. The ordinary application
database is not modified by fixture acceptance. The test URL must be exported in
the shell; pytest does not infer it from `.env`.

```sh
uv run --locked python scripts/foundation_release.py --output /tmp/foundation-release-001
cat /tmp/foundation-release-001/release.json
cat /tmp/foundation-release-001/reopen.json
```

Choose a new output directory each time. The runner executes the complete test
suite, retains JUnit XML, records counts, lock hash and source commit, and refuses
to pass fixture acceptance with any skipped test, error or failure. Missing database
configuration is a preflight error. Install is the preceding locked sync step, not
an implied result of passing tests in an already installed environment. Retain its
terminal result with release evidence when qualifying a clean installation.

The runner ignores inherited `PYTEST_ADDOPTS` selection flags, clears configured
pytest `addopts`, and explicitly targets `tests/`. It independently collects the
expected test IDs and requires an exact match in the executed JUnit cases. Fixture
acceptance also requires the mandatory fresh-process checklist and its successful
three-report reopen evidence; a passing subset or missing evidence cannot pass.

`reopen.json` records actual artifact hashes and fresh-process reopen results from
`test_foundation_release_fresh_process_checklist`. That test runs configuration,
idempotent migrations, fixture Buy, cancelled and confirmed trade recording,
duplicate confirmation, fixture Sell, persisted interruption, CLI resume, report
show and regeneration as separate processes. It checks holdings remain unchanged
by scans/resume and that reopen/regeneration preserve report hashes. Its disposable
database is removed afterwards; its report UUIDs identify test evidence, not records
in the operator's application database.

For persistent reports that you can reopen yourself, use a separate scratch
application database and the same runtime directory in each command:

```sh
uv run --locked stock-analyser --data-dir /tmp/foundation-demo init
uv run --locked stock-analyser --data-dir /tmp/foundation-demo scan buy --sector technology --fixture shortfall
uv run --locked stock-analyser --data-dir /tmp/foundation-demo report history
# Copy the printed run UUID into each fresh invocation:
uv run --locked stock-analyser --data-dir /tmp/foundation-demo report show RUN_UUID --json
uv run --locked stock-analyser --data-dir /tmp/foundation-demo scan resume RUN_UUID
uv run --locked stock-analyser --data-dir /tmp/foundation-demo report regenerate RUN_UUID
```

Do not confirm synthetic executions into a real portfolio. The checklist creates
them only inside its disposable database.

## Acceptance coverage

- Install/configuration: locked uv sync above; startup/environment/config tests;
  the fresh-process checklist validates a real TOML and `doctor`.
- Migrations/integrity: `test_migration_idempotency_and_schema`, migration backfill,
  immutable-record, rollback, hash verification and competing-write tests in
  `tests/test_persistence.py`; migration head is `0007`.
- Selection: `tests/test_universe.py` checks deterministic ties, exclusions,
  classification uncertainty, shortfalls and exact replay of frozen inputs.
- Completed sessions: `tests/test_calendars.py` covers holidays, exceptional
  closures, early close, DST, independent daily/weekly history and grace boundaries.
- Routing: `tests/test_price_routing.py` checks one selected source, coverage limits
  and absence of runtime fallback; controlled transports exercise both providers.
- Partial/all failures and recovery: collection/report tests plus persistence
  `test_report_artifacts_cover_success_and_collection_failures`,
  `test_failed_report_write_keeps_analysis_and_regenerates_after_restart`,
  Buy/Sell per-stock failures, cache correction/replay and resume tests.
- Accounting: `tests/test_ledger.py` and persistence ledger tests cover FIFO,
  partial/complete exits, fee allocation, unknown FX, split/correction atomicity,
  oversell rejection, concurrency and duplicate confirmation.
- Market isolation: `tests/test_contracts.py` uses the same `ACME` symbol with
  distinct US/TEST identities, USD/INR currencies, cutoff dates and providers;
  public CLI/registry rejects TEST/IN. These are synthetic calendars and routes,
  not implementation or validation of India.

## Opt-in bounded observations

No live calls occur in the default checklist. To opt in, export the application
database URL as well as the test URL, configure an existing `MASSIVE_API_KEY`, and run:

```sh
uv run --locked python scripts/foundation_release.py --output /tmp/foundation-live-001 --live
```

The live phase refreshes AAPL/SPY daily/weekly evidence using existing routing,
then runs the universe spike with at most two pages per source and two classification
profiles. Each command has a 180-second wall-clock limit; the existing Massive
shared rate allowance, request budget, pagination bounds and cooldown remain in
effect. These deliberately small bounds may truncate enumeration, which is failure
evidence. yfinance SDK HTTP amplification is unknown. A timeout is an inconclusive
failed observation, not absence of data. No news/model calls are requested.

`release.json` separates `fixture_readiness`, `live_observation`, `pilot_sector`
and `milestone`. Runner exit zero means **fixture acceptance passed**; milestone
completion requires fixtures, an observed live smoke, and a passing pilot gate with
no blockers. A successful smoke still retains `publication_timing=unverified`.
Changing that policy requires measured publication evidence. Live report files
and manifests live under the evidence directory's `live-runtime`; their run IDs
remain in the explicitly chosen application database. Reopen them with
`report history` / `report show` using that database and runtime.

## Operating and recovering the foundation

Run `doctor` to inspect resolved runtime paths and configuration hash without
connecting or creating directories. Precedence is CLI, environment, TOML, then
platform defaults. Relative TOML paths resolve against its file location.
`--data-dir` contains `snapshots/`, `cache/`, `reports/`, `logs/` and a compatibility
`database/` directory; PostgreSQL data is actually in its server/Compose volume.
Separate directory environment overrides can place these outside `--data-dir`.
The Massive allowance/lease is per user and deliberately independent of data-dir.

Credentials belong in environment or gitignored `.env`, never TOML or evidence.
Shell variables override current-directory `.env`. Massive also supports the
existing `.env.local` fallback. Fixture workflows need only PostgreSQL; Bedrock
credentials are not used. `doctor` checks presence, not account entitlements.

Use `--cache-mode reuse` normally, `refresh` for the current same-source overlap,
`replay` for exact retained offline evidence, and `off` for a fresh full window.
Metadata TTL is 24 hours. A new completed session changes the history window;
intraday reuse does not certify future publication availability. Refresh detects
corrections inside its overlap only, retaining both generations and lineage.
For older restatements or split-adjusted history, request a fresh full window.
Never edit old snapshot/report bytes to apply corrections. See
[cache retention and correction policy](evidence-cache.md).

Stop writers before backup or upgrades. Back up PostgreSQL **and** the resolved
snapshots, cache sources, reports and config together, preserving absolute paths
and permissions; a database dump alone cannot restore evidence files. Keep a copy
of the matching source revision and lock file, and store credentials separately.

```sh
docker compose exec -T postgres pg_dump -U stock_analyser -d stock_analyser -Fc > /safe/backup/foundation.dump
# With writers stopped, copy the resolved runtime directories to the same backup.
uv sync --locked
uv run --locked stock-analyser init
```

`init` applies migrations transactionally and is idempotent. If migration fails,
fix connectivity/permissions and retry; preserve the original backup. To recover,
create an empty scratch database, restore with `pg_restore --exit-on-error` against
that database, and restore evidence files to their recorded absolute paths before
selecting the restored database URL. For the Compose role, for example:

```sh
docker compose exec -T postgres createdb -U stock_analyser foundation_restore
docker compose exec -T postgres pg_restore -U stock_analyser -d foundation_restore --exit-on-error < /safe/backup/foundation.dump
```

Verify `init`, `portfolio`, receipts and `report show` in the restored copy before
switching normal operation. If rolling back code, use the pre-upgrade dump and
matching files/code, rather than guessing a production downgrade. Never use
`docker compose down -v` on a database you need to retain.

Report-write failure does not undo collected evidence or ledger records. Correct
the directory/permissions, then `report regenerate RUN_UUID`; generation uses
persisted inputs without a provider. A hash mismatch is corruption: preserve the
files for investigation, restore trusted originals from backup, or remove only
the identified damaged generated artifacts before regeneration. Missing raw
evidence needs backup recovery; regeneration cannot reconstruct a vanished source.
For interrupted supported scans use `scan resume RUN_UUID`, which preserves frozen
selection, holdings revision, provider and cutoff and skips completed work.
Legacy runs without resume plans require a new scan. Never retry a recorded trade
with a new key: use its original idempotency key and inspect `execution-receipt`.
Use confirmed `correct-execution`/corporate-action events for ledger changes;
historical receipts and reports remain immutable.

## Decisions deferred to later phases

- Earnings: a corroborated forward-event source, entitlement/retention rights,
  timezone/session interpretation, conflict resolution and mandatory blackout
  policy. Missing news is not proof of no news; unknown earnings cannot clear gates.
- Indicators/strategy: formula and smoothing seeds, warm-up, missing/zero handling,
  price adjustment, detector thresholds, evidence-group caps and independently
  evaluated Buy/Sell gates. No indicator or debate implementation is included.
- Bedrock: region, model/inference-profile IDs per role, account access, quotas,
  scan budget and permission to send provider content. No model readiness is claimed.
- Pilot coverage: complete classification, immutable cross-provider identity,
  measured history/liquidity workload and retention rights. The limited live
  spike is evidence of gaps, not approval of a production universe.

India and additional paid providers remain outside this release.
