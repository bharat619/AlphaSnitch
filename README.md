# Stock market analyser

Local Python CLI foundation for AlphaSnitch, plus architecture and provider-evaluation work. Issues [US-001](https://github.com/bharat619/AlphaSnitch/issues/1) and [US-002](https://github.com/bharat619/AlphaSnitch/issues/2) add installation, configuration, the command menu, diagnostics, and market-aware contracts. [US-003](https://github.com/bharat619/AlphaSnitch/issues/3) adds PostgreSQL persistence and initialization. [US-004](https://github.com/bharat619/AlphaSnitch/issues/4) adds completed-session and history resolution. [US-005](https://github.com/bharat619/AlphaSnitch/issues/5) freezes and validates fixture evidence; [US-006](https://github.com/bharat619/AlphaSnitch/issues/6) saves, reopens and regenerates data-readiness reports. Reports is available from the menu. Buy, Sell, Portfolio and Record offline trade remain visibly **unavailable** until their implementation stories land. The Python scripts in `evaluation/` remain isolated, read-only evaluation tools.

## Market-aware contracts (US-002)

Version 1 Pydantic contracts live in `stock_analyser.contracts`. Securities and listings have immutable UUID identities; provider symbols use half-open effective-date intervals. Instrument lookup requires a market and rejects ambiguous listings unless a venue is supplied. Frozen contracts use tuples for nested collections and reject unknown fields and unsupported schema versions.

`MarketRegistry()` enables US only and supplies versioned calendar references, currency, provider routes, feed coverage, capabilities and benchmarks. The calendar adapter resolves these references using maintained, locked calendar data as described below. US history coverage describes the initial configured policy; it does not verify live entitlements. Provider protocols cover prices, universe, research, corporate actions, calendars and LLMs. The Massive live adapter is described below; price-provider selection belongs to a later story.

Analysis requests require an aware as-of timestamp and explicit history requirements. Evidence retains the resolved market, instrument, currency, cutoff, provider symbol, feed and source hash. Every price source declares inclusive local session dates within its effective symbol mapping, allowing a snapshot to retain separate sources before and after a ticker rename. Any returned bars require provenance, including partial results, and price-history validation checks that sources cover every bar's session.

An `AVAILABLE` report must cover each requested instrument and mandatory timeframe with the requested adjustment and minimum-plus-warmup bar count in a single available snapshot. Sector reports must provide `resolved_instrument_ids` before claiming availability. Optional history may be omitted. Data-readiness reports separate operational status, data availability and strategy verdict. Indicators, setups, debate and verdict remain `NOT_RUN`, including when data collection succeeds.

The locked `tzdata` dependency supplies IANA timezone data when the operating system has none. Startup regression tests disable system timezone lookup with `PYTHONTZPATH=''`.

Run this offline demonstration after installation (`TEST` is synthetic and cannot be selected through CLI flags, environment or TOML):

```sh
uv run --locked --no-sync python - <<'PY'
from datetime import UTC, datetime
from stock_analyser.contracts import HistoryRequirement
from stock_analyser.fixtures import synthetic_markets

markets, instruments, adapters = synthetic_markets()
as_of = datetime(2026, 10, 16, 15, tzinfo=UTC)
for market_id, adapter in zip(("US", "TEST"), adapters, strict=True):
    market = markets.resolve(market_id)
    instrument = instruments.resolve(market_id, "ACME")
    evidence = adapter.history(
        instrument, market,
        HistoryRequirement(timeframe="daily", minimum_bars=1), as_of,
    ).metadata
    print(market_id, instrument.instrument_id, evidence.currency,
          evidence.sources[0].provider, evidence.session_cutoff.isoformat())
PY
```

The two `ACME` listings have distinct security and instrument IDs, USD/INR currencies and provider routes. At the same as-of instant, the fixture US calendar resolves October 15 while the second market resolves October 16. The adapters use explicit synthetic closes and fractional-volume bars, require no credentials and make no network calls. Research and corporate-action fixtures intentionally contain empty datasets; the LLM fixture returns a fixed synthetic response.

## Completed sessions and history (US-004)

`stock_analyser.calendars.ExchangeCalendarAdapter` supplies regular US equity sessions from the locked `pandas-market-calendars` dependency. XNYS uses NYSE, XNAS uses NASDAQ, and ARCX uses the NYSE regular equity schedule. Holidays, exceptional closures, early closes and DST offsets come from that maintained data, which is bundled with the installed dependency and requires no runtime network access. Unknown calendar references fail explicitly. Extended-hours trading is excluded. Calendar behavior and schedule APIs are documented by [pandas-market-calendars](https://pandas-market-calendars.readthedocs.io/en/latest/usage.html).

`HistoryResolver.resolve(market, venue, requirement, requested_as_of)` returns a frozen, JSON-serializable `ResolvedHistory`. It stores the requested instant normalized to UTC separately from local session dates, actual close instants and grace-adjusted eligibility instants. It also freezes the market/configuration, calendar reference, installed calendar-data version, complete cutoff policy and original history requirement. `history_start`, `session_date`, and `session_cutoff` expose the resulting provider request window; no completed history produces `None` cutoffs.

Policy `completed-sessions-1` uses exchange-local Monday–Sunday weeks. A weekly bar is eligible only after its final **scheduled** session closes and its own grace period expires; a Friday holiday can make Thursday the final session. Daily and weekly requirements each request `minimum_bars + warmup_bars` independently. Weekly provider labels must normalize to the week's final local session date. The resolver queries through the end of the current week to avoid treating a midweek prefix as a completed week. It expands the history search as needed, bounded by `earliest_session` (default 1990-01-01), and reports insufficient history instead of shortening the requirement. A partial week at that lower bound is excluded conservatively.

Daily and weekly publication grace defaults to 900 seconds each and can be configured separately through `CutoffPolicy`. These defaults are provisional policy, **not measured publication guarantees**. Live-provider timing remains `unverified`, producing `PUBLICATION_UNVERIFIED` until validated in US-007. Synthetic fixtures explicitly use `synthetic`; `observed` is reserved for a policy supported by provider observations.

Resolution reasons distinguish `MARKET_CLOSED` (outside a scheduled session), `PUBLICATION_DELAYED` (a completed period is still inside its grace window), and `INSUFFICIENT_HISTORY`. `assess_history(plan, returned_sessions)` additionally detects `STALE_DATA` when the latest eligible bar or an interior expected bar is missing. Short history from a recent listing has an insufficient-history reason without being marked stale when its latest bars are current. Holiday/weekend closure alone does not make data stale. Reasons can coexist; closed-market and grace states describe context and do not establish provider failure. Older bars do not count toward the resolved requirement. Unexpected labels within or beyond the resolved window, including incomplete weeks, are rejected.

`RunRecord.history_resolutions` persists these plans in the existing PostgreSQL run JSON payload, independently of the original request timestamp. Plans must match the frozen run request and market; attached snapshots must match the plan's venue/timeframe cutoff, adjustment and requested bar count. Existing runs without plans remain readable. The persistence demonstration below records a synthetic daily plan and verifies it across processes.

Run this offline demonstration of the Good Friday boundary and Thanksgiving early close:

```sh
uv run --locked --no-sync python - <<'PY'
from datetime import datetime
from stock_analyser.calendars import ExchangeCalendarAdapter, HistoryResolver
from stock_analyser.contracts import HistoryRequirement
from stock_analyser.markets import US_MARKET

resolver = HistoryResolver(ExchangeCalendarAdapter())
for instant in (
    "2026-04-02T20:14:59+00:00", "2026-04-02T20:15:00+00:00",
    "2026-11-27T18:14:59+00:00", "2026-11-27T18:15:00+00:00",
):
    for timeframe in ("daily", "weekly"):
        plan = resolver.resolve(
            US_MARKET, "XNYS",
            HistoryRequirement(timeframe=timeframe, minimum_bars=1),
            datetime.fromisoformat(instant),
        )
        print(instant, timeframe, plan.session_date,
              plan.session_cutoff.isoformat(), ",".join(plan.reasons))
PY
```

At April 2's grace boundary, the daily cutoff advances from April 1 to April 2 and the weekly cutoff from March 27 to April 2 (Good Friday is closed). At November 27's early-close grace boundary, daily advances from November 25 to November 27 and weekly from November 20 to November 27. The actual November 27 close is 18:00 UTC; eligibility begins at 18:15 UTC.

`fixtures.synthetic_session_calendars()` supplies two enumerated synthetic weeks for US and TEST markets. At `2026-10-16T15:00:00Z`, US resolves daily October 15 and weekly October 9; TEST resolves daily and weekly October 16. Tests exercise both markets without enabling TEST in production configuration or claiming live India calendar support.

## Install and launch

Use **Python 3.12** and [uv](https://docs.astral.sh/uv/getting-started/installation/). From a clean checkout:

```sh
uv python install 3.12
uv sync --locked
uv run --locked stock-analyser --help
uv run --locked stock-analyser
```

`uv.lock` pins runtime and development dependencies; `--locked` refuses to silently update it. The build backend version is pinned separately in `pyproject.toml`. `.python-version` selects Python 3.12 and the package restricts its supported baseline to 3.12. The first installation downloads packages; launching help, the menu, and `doctor` needs no provider credentials or network. `init` requires access to your PostgreSQL server. The CLI automatically loads `.env` from the current working directory. Evaluation results are not required. Live Massive collection can reuse the key in `.env.local`.

The menu shows the active US market and six actions: Buy, Sell, Portfolio, Record offline trade, Reports, and Exit. In an interactive terminal, use **↑/↓** or numbers **1–6** to highlight an action and **Enter** to select it. Reports lists recent runs and shows the commands to reopen or regenerate a report. Selecting an unavailable action explains its status and returns to the menu with that action highlighted. Select Exit or press Ctrl+C to quit. Piped input and terminals without interactive support use a numbered prompt, where `6`, an empty line, or EOF exits.

For an installation without development tools, use `uv sync --locked --no-dev`. After syncing, the entry point is also available as `.venv/bin/stock-analyser` (Windows: `.venv\Scripts\stock-analyser.exe`), or after activating the environment as `stock-analyser`.

## PostgreSQL setup (US-003)

Application state uses **PostgreSQL 18**, SQLAlchemy, Alembic, and psycopg. This implements the PostgreSQL choice in place of the issue's original SQLite/WAL requirement. Trade ledger tables and LangGraph checkpoints remain for their implementing stories.

Install and start Docker with Compose support, then run these commands from the repository root. The example password uses URL-safe characters; when choosing another password, percent-encode reserved characters in the connection URL.

```sh
cp .env.example .env
# Edit .env once to match your database password and port.
docker compose up -d --wait
uv run --locked stock-analyser init
uv run stock-analyser collect-fixture --scenario valid
```

Compose and the CLI both read the repository-root `.env` when run from that directory, so subsequent commands need no exports. `.env` is gitignored; `.env.example` is a credential-free local-development template. Use the password already configured in your database if it exists. Shell environment variables override `.env` values. General configuration loads only the current-directory `.env`, without searching parent directories. Live Massive collection additionally supports a `.env.local` key fallback.

Compose publishes PostgreSQL on localhost only. If port 5432 is occupied, put `STOCK_ANALYSER_POSTGRES_PORT=55432` in `.env` and use port 55432 in the URL. The password variable is required by Compose. The application reads `STOCK_ANALYSER_DATABASE_URL` from `.env` or the shell; credentials do not belong in TOML. `doctor` reports whether a URL is configured without exposing it or connecting to the database. Both `postgresql://` and `postgresql+psycopg://` URLs use psycopg.

`init` creates local runtime directories, applies packaged Alembic migrations to the selected database, and seeds the versioned US market configuration. The database itself must already exist; Compose creates it on first startup. Repeating `init` preserves existing records and does not duplicate market configuration. Migrations and seed data share one transaction. Initialization errors roll back database changes; directories may already have been created.

```sh
docker compose ps
docker compose logs --tail 50 postgres
docker compose stop
# Resume the same database and named volume:
docker compose up -d --wait
# Remove the container and network, retaining database files:
docker compose down
```

The named `postgres_data` volume is mounted at `/var/lib/postgresql`, as required by the [PostgreSQL 18 image layout](https://hub.docker.com/_/postgres). It survives container restarts and `docker compose down`. `docker compose down -v` deletes the database volume and all its records. Changing `POSTGRES_PASSWORD` does not change an existing volume's database password; use the original credentials or change the database role password explicitly. Keep the same Compose project name/directory when reopening an existing volume.

Back up the database before upgrading an existing installation, then run `uv sync --locked` and `stock-analyser init` to apply migrations. For example, with the Compose server running:

```sh
docker compose exec -T postgres pg_dump -U stock_analyser -d stock_analyser -Fc > /absolute/path/to/stock-analyser.dump
```

The `database/` runtime directory is retained for compatibility with existing path settings; PostgreSQL stores its data in the Docker volume, independently of `--data-dir`. Snapshots and report files remain under their configured local directories.

### Persistence and restart demonstration

Run these as separate commands/processes after initialization:

```sh
uv run --locked python -m stock_analyser.persistence.demo create
# Copy the printed UUID into the next command, including after restarting PostgreSQL:
uv run --locked python -m stock_analyser.persistence.demo show <run-uuid>
```

The synthetic run retains its requested cutoff, complete versioned market configuration, effective configuration, source version, component versions, collection stage, snapshot reference and source hashes. Collection succeeds while indicators, setups, debate and verdict remain `NOT_RUN`. All three report artifact records remain `PENDING`; the demonstration creates no report or evidence files and makes no provider calls. Synthetic data uses the separate `synthetic-1` market configuration and never changes the production US profile.

`stock_analyser.persistence.Database` exposes short `write()` and `read()` repository scopes. Write transactions take a database-scoped PostgreSQL advisory lock shared across application processes. Provider calls and filesystem operations belong outside write scopes. Repositories preserve immutable security/instrument identities, effective-dated provider mappings, market configuration versions and snapshot references. Instrument security, market, venue, and series define stable listing identity. Symbol and provider-mapping changes append content-addressed instrument versions and update current lookups under the same UUID. Currency changes under the same instrument UUID are rejected until runs bind their own instrument revisions, preserving historical stage updates and idempotent run insertion. `get_instrument(id, revision)` retrieves a historical version, and snapshots retain their original registered version even after a rename. Reusing other immutable keys with different content fails; changing market configuration requires a new version. Runs retain frozen requests/provenance while operational status and per-instrument stage state can be updated. Artifact failures and retries update artifact records independently of run status. Repository writes revalidate serialized contracts, including objects changed with Pydantic `model_copy`. Fixture snapshot identities include all evidence metadata, including retrieval time; collecting unchanged bars later creates a distinct immutable snapshot.

### PostgreSQL integration tests

The test account needs `CREATEDB`. The Compose account has this permission; ordinary application accounts only need schema migration and application table access. Each integration test creates a randomly named temporary database and drops only that database afterward. Tests never initialize or modify the database named in the test URL.

```sh
# Add STOCK_ANALYSER_TEST_DATABASE_URL to .env, using the same local database URL.
uv run --locked --env-file .env pytest -q
```

Without `STOCK_ANALYSER_TEST_DATABASE_URL`, PostgreSQL integration tests are explicitly skipped and offline tests still run. CI always supplies a PostgreSQL service and runs the integration tests. Checks cover empty/repeated initialization, schema parity, downgrade/upgrade and instrument-version backfill, transaction rollback, symbol renames with historical snapshot reuse, invalid artifact update rejection, repeated fixture collection, immutable records, market/cutoff validation, independent artifact failure/retry, cross-connection write coordination, and process restart recovery. No private evaluation results or provider credentials are used.

## Local configuration and runtime files

```sh
uv run --locked stock-analyser doctor
uv run --locked stock-analyser --config config.example.toml doctor
uv run --locked stock-analyser --data-dir /absolute/path/to/runtime init
```

`doctor` prints resolved directories, package version, Git commit and dirty state when available, and a SHA-256 fingerprint of the effective runtime configuration. It makes no provider calls or writes. `init` also migrates and seeds the PostgreSQL database; configure and start it as described above. The menu and help do not create runtime files.

Copy [config.example.toml](config.example.toml) to the user configuration location shown by `platformdirs`, or pass `--config /path/to/config.toml`. Defaults are outside the source checkout:

- macOS: configuration at `~/Library/Application Support/stock-analyser/config.toml`; runtime data at `~/Library/Application Support/stock-analyser/`.
- Linux: configuration at `~/.config/stock-analyser/config.toml`; runtime data at `~/.local/share/stock-analyser/` (respects XDG directory settings).
- Windows: configuration and runtime data under `%LOCALAPPDATA%\stock-analyser\`.

The data directory contains `database/`, `snapshots/`, `cache/`, `reports/`, and `logs/`. These are reserved runtime locations; this story logs only to stderr. Each directory can be overridden independently; `database/` does not control the PostgreSQL data location. Relative configured paths resolve beside the configuration file; relative CLI/environment overrides use that same base (the default user configuration directory when no file is supplied). Prefer absolute paths for overrides.

Configuration precedence is CLI options → shell environment variables → current-directory `.env` → TOML → defaults. Supported variables are `STOCK_ANALYSER_CONFIG`, `STOCK_ANALYSER_MARKET`, `STOCK_ANALYSER_DATA_DIR`, `STOCK_ANALYSER_DATABASE_DIR`, `STOCK_ANALYSER_SNAPSHOTS_DIR`, `STOCK_ANALYSER_CACHE_DIR`, `STOCK_ANALYSER_REPORTS_DIR`, `STOCK_ANALYSER_LOGS_DIR`, and `STOCK_ANALYSER_DATABASE_URL` (from `.env` or the shell). Explicit child-directory overrides take precedence over the data-directory default. A missing explicitly requested configuration file, malformed TOML, unknown settings, or unsupported market fails clearly. Only `US` is enabled.

Keep provider credentials in environment variables or a local secrets mechanism; AWS integration will use the SDK credential chain. Credentials do not belong in TOML. The example contains no secrets. Terminal errors and the application's logging formatter redact known credential environment values, API-key/token/password assignments, and bearer tokens. Configuration validation never echoes invalid input values. Optional credentials are not validated or required for these offline commands.

## Source version and focused verification

This checkout already has Git initialized. For a source folder without `.git`, initialize local version control with `git init` and make a reviewed initial commit; no remote is required. Check `git status --short` before committing, and keep credentials, environments, and runtime data outside tracked files. `doctor` records the current commit and whether the checkout has uncommitted changes. A wheel installation or a source folder without Git reports `null` for commit/dirty state and retains the package version; record the source commit separately when distributing a wheel. Future analysis runs can consume this provenance helper.

```sh
uv run --locked pytest -q
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked stock-analyser --help
printf '1\n6\n' | uv run --locked stock-analyser
uv run --locked stock-analyser --config config.example.toml doctor
```

Tests cover offline menu/help, configuration precedence and path creation, unsupported markets, missing/invalid configuration, credential redaction in errors and exception logs, and version/configuration recording. Update dependencies intentionally with `uv lock`, then rerun the focused checks. Live provider calls and trading decisions remain outside these foundation stories; PostgreSQL repository and report checks are described above and below.

## Continuous integration

[GitHub Actions](.github/workflows/ci.yml) runs on pull requests targeting `main`, pushes to `main`, and merge-queue checks. The `Tests (Python 3.12)` check installs locked dependencies on Ubuntu, runs the test suite, checks lint and formatting, and launches CLI help. It starts PostgreSQL 18 and runs disposable-database integration tests as well as offline checks. It needs no provider credentials. Actions and uv versions are pinned; superseded runs are cancelled automatically.

To enforce this before merging, `main` branch protection must require the `Tests (Python 3.12)` status check with the branch up to date. Keep that required check name aligned with the workflow job name when editing CI.

## Documentation

- [Architecture](ARCHITECTURE.md): CLI workflows, agents, deterministic technical analysis, market/provider contracts, database, portfolio recording, report artifacts, validation and implementation phases.
- [Next-phase implementation plan](IMPLEMENTATION_PLAN.md): proposed data and CLI foundation, ordered work packages, dependencies, decision gates, and acceptance scenarios.
- [Development user stories](planning/github-issues/README.md): deliverable-sized GitHub issue drafts, acceptance criteria, execution order, and publication tracking.
- [Provider evaluation findings](evaluation/REPORT.md): live Massive and yfinance results, cross-provider comparison, limitations, supporting sources and remaining checks.
- [Evaluation setup and reproduction](evaluation/README.md): pinned environment, commands, credential handling and evaluation scope.
- [Agreed history-based provider selection](ARCHITECTURE.md#agreed-history-based-price-provider-selection): Massive by default; yfinance when mandatory history exceeds Massive's configured coverage, with a complete price snapshot from one provider per stock analysis.

## Confirmed decisions

- CLI with Buy → sector → ten eligible stocks, and Sell → recorded portfolio holdings.
- Swing-trading horizon of days to weeks; US first, India through separately validated market/provider configuration later.
- Deterministic code calculates indicators and rule-defined setups. Challenger, defender and independent judge interpret one frozen evidence snapshot.
- AWS Bedrock for LLM inference. Proposed orchestration and persistence details are in the architecture.
- Massive for primary US prices and initial news discovery; yfinance for the approved longer-history route and supplementary classification/discovery. Start with free tiers and pay only for demonstrated essential gaps.
- For a mandatory five-year weekly requirement beyond Massive coverage, validate yfinance history per instrument and use it for both daily and weekly prices. Do not silently mix price feeds or shorten required lookbacks.
- Actual trades happen offline. Record purchases/sales in the database only after the user confirms actual execution details; selecting a recommendation alone does not change holdings.
- Save human-readable reports, structured results and evidence manifests. No automatic broker execution.

## Saved evaluation evidence

The original responses/history frames and results remain locally under `evaluation/results/`:

- [yfinance main run](evaluation/results/20261010T070940Z/results.json): 51 probes; US/India histories, profiles, screening, events, news and adjustment checks.
- [yfinance targeted follow-up](evaluation/results/20261010T071310Z-followup/results.json): five probes for search news and BSE history.
- [Massive live run](evaluation/results/20261010T072024Z-massive/results.json): 25 probes, 27 HTTP requests; prices, ETFs, reference data, corporate actions, news and pagination.
- [Cross-provider comparison](evaluation/results/20261010T072024Z-massive/comparison.json): common-date OHLCV differences and diagnostic feature/flag comparisons.
- [Final verification](evaluation/results/20261010T072024Z-massive/verification.json): split factors, dividends, pagination, news metadata and request pacing.
- Hash manifests: [yfinance](evaluation/results/20261010T070940Z/sha256.json), [follow-up](evaluation/results/20261010T071310Z-followup/sha256.json), [Massive](evaluation/results/20261010T072024Z-massive/sha256.json).

Sandbox connectivity failures are retained separately and excluded from provider findings. The report distinguishes observed access and data quality from documented capabilities and untested requirements.

## Remaining decisions and validation

Complete sector-universe eligibility/classification and liquidity selection; verify a forward-earnings source; select Bedrock region, models and per-scan cost limits; finalize indicator conventions and strategy gates. Before operational reliance, validate publication timing, corrections, failure recovery, longer-history quality, broader coverage and applicable data-use terms. India requires its own integration evaluation.

## Local files and credentials

The evaluation scripts and their dependency versions are in `evaluation/`. The API key stays in gitignored `.env.local`; no key is embedded in documentation or saved results. Virtual environments, provider cookie caches and raw results are also gitignored. Raw evidence is saved locally and is not required by the installable CLI or its tests.

### Freeze and validate synthetic evidence (US-005)

With PostgreSQL running and `STOCK_ANALYSER_DATABASE_URL` configured as above:

```sh
uv run stock-analyser collect-fixture --scenario valid
uv run stock-analyser collect-fixture --scenario missing-bar
```

Both commands create a new one-stock run with three requested daily sessions.
The valid fixture preserves fractional volume and exits successfully. The
missing-bar fixture exits with code 1, persists `FAILED` collection/run status,
and records `PARTIAL` evidence with `MISSING_SESSION`, `STALE_DATA`, and
`INSUFFICIENT_HISTORY` findings. Each command prints the run ID, evidence metadata,
and the absolute paths of its saved report and `manifest.json`. These are entirely synthetic
fixtures shipped in the package; no credentials for a market-data provider,
network calls to such a provider, or `evaluation/` directory are required.

Evidence lives under the configured snapshots directory:

- `<snapshot-id>/source.json`: exact raw response bytes, including invalid rows.
- `<snapshot-id>/manifest.json`: normalized valid bars, all quality findings,
  source hash and retrieval time, instrument and frozen market configuration,
  adjustment and feed metadata, requested calendar periods and returned row count,
  calendar/cutoff policy versions, source version, and collection version.
- `runs/<run-id>/manifest.json`: frozen run inputs, overall collection outcome,
  and evidence packets. Reports retain this collection manifest's contents.
  PostgreSQL stores each snapshot manifest's path/hash and the collection stage outcome.

Files are published atomically without replacing existing files and are read-only.
A correction or repeated CLI invocation creates a new run and new snapshot IDs.
Hashes cover the exact stored bytes. The source record's session interval is the
queried window; it does not assert that every session was returned. Returned
sessions and row-level failures remain visible in the evidence packet and source.

The versioned `price-quality-1` policy requires unique ascending session labels,
exact timezone-aware completed-period close timestamps, finite positive OHLC
values with consistent high/low bounds, and finite nonnegative volume. Weekly
labels, when requested through the service, refer to the final session of each
resolved week. Missing or unexpected periods, invalid values, adjustment mismatch,
retrieval before the requested as-of time, and unverified publication policy are
errors. The full history resolution retains market-closed/publication-delay
context; a closed market alone does not imply stale data. Missing expected bars
are stale even when an older response has enough total rows.

Provider JSON numeric fractions are decoded directly as decimals to preserve their exact precision. Volume is stored as decimal strings without rounding to integers. Explicit zero
volume is retained and flagged `ZERO_VOLUME` as a warning; missing/null volume is
an error and is never replaced with zero. A current open at or below 60%, or at or
above 160%, of the previous close triggers a blocking
`CORPORATE_ACTION_DISCONTINUITY` finding. This conservative heuristic catches
large split-like gaps but cannot identify all corporate actions; it neither
repairs prices nor certifies adjustment correctness. The response's declared
adjustment is preserved separately from the requested policy.

An error in any mandatory history blocks `SUCCEEDED`. Valid normalized rows in a
failed packet are retained for inspection and must not be treated as usable
assessment input. The service does not run indicators, setup detection, or agent
conclusions. Filesystem/provider exceptions propagate without claiming success;
previously attached snapshots remain available for inspection.

If a response declares the wrong provider, feed, or symbol, its exact raw bytes
are frozen before provenance validation. A failed diagnostic records the actual
response labels, retrieval time, raw hash, requested resolution, and mismatch
findings (`PROVIDER_MISMATCH`, `FEED_MISMATCH`, or `SYMBOL_MISMATCH`). The saved
run and report manifests link to this diagnostic by path and hash in `rejected_responses`.
Rejected responses are kept outside trusted snapshot metadata and cannot satisfy
mandatory history requirements.

### Save, reopen and regenerate reports (US-006)

After updating the application, run `stock-analyser init` to apply migration
`0003`, which adds immutable report inputs in PostgreSQL. Every `collect-fixture`
invocation that reaches collection saves a data-readiness report, including
missing-bar and total provider failures:

```sh
uv run --locked stock-analyser collect-fixture --scenario valid
uv run --locked stock-analyser collect-fixture --scenario missing-bar
uv run --locked stock-analyser collect-fixture --scenario provider-failure
uv run --locked stock-analyser report history --limit 20
# Copy a run_id from collection output or history; these commands also work after restart:
uv run --locked stock-analyser report show <run-id>
uv run --locked stock-analyser report show <run-id> --json
uv run --locked stock-analyser report regenerate <run-id>
```

The missing-bar and provider-failure commands exit with code 1 while still saving
failure reports. Operational collection status, data availability and report-save
status are separate. No strategy recommendation is generated: indicators,
setups, debate and verdict are explicitly `NOT_RUN`.

Reports live under the configured reports directory at `<date>/<run-id>/`:

- `report.md`: readable market, currency, venues, history/cutoff policy, source
  and adjustment basis, quality findings, collection outcomes and coverage limits.
- `report.json`: schema-versioned request, frozen run/configuration, snapshot IDs,
  evidence packets, per-instrument collection stages and data-readiness results.
- `manifest.json`: matching run/snapshot/schema IDs, source/configuration versions,
  snapshot references/hashes, rejected-response diagnostics and hashes of both reports.
  PostgreSQL holds the manifest's own hash, avoiding a circular self-hash.

Inputs are committed before writing the report destination. Each artifact is
flushed to a sibling temporary file and published by atomic rename. The files
are verified together before all three artifact records become `SAVED` and the
CLI announces success. The three files are not one atomic filesystem transaction;
an interrupted publication remains incomplete until regeneration finishes.

If the destination is unwritable or a write fails, collection state and evidence
remain intact. The error identifies the run and its `report regenerate` command;
history lists the report as incomplete. Fix the destination permissions or free
disk space, then regenerate. Retries reuse the persisted artifact paths and IDs,
do not invoke providers or recalculate calendar cutoffs, and reproduce identical
artifact bytes from the immutable inputs. Existing matching files are preserved;
missing or damaged artifacts are repaired. `report show` checks all three saved
hashes before displaying a report. Older runs can also be rendered from retained
state; unavailable evidence packets are disclosed and prevent an `AVAILABLE` claim.

PostgreSQL integration tests inject a failure after the first artifact, verify that
analysis survives, regenerate in a separate process with provider fetching disabled,
reopen the structured report after restart, and check repeated regeneration and
tamper recovery. They also cover total provider failure and interruption after a
completed daily history but before weekly history returns.

### Massive live evidence (US-007)

Run `stock-analyser collect-massive` to opt in to a small live AAPL and SPY
benchmark collection. Set `STOCK_ANALYSER_DATABASE_URL` and your existing
`MASSIVE_API_KEY` in the shell or `.env`. The key also falls back to the current
directory's `.env.local`, matching the earlier evaluation; unrelated settings
from that file are not loaded. No key is accepted as a CLI argument.

The command collects three daily and two weekly bars for each listing, plus
splits and dividends, then saves the usual Markdown/JSON reports. The two
one-instrument reports share a smoke group and request instant. These are
explicit smoke listings, not reference-data discovery or strategy execution.
Use `report show <run-id>` and `report regenerate <run-id>` offline afterward.

Configure `[massive]` in TOML using `config.example.toml`. The default is five
requests per minute with a shared 100-attempt budget and two retries per request.
The twelve unpaginated requests take about 2.5 minutes; pagination and retries
can take longer. No live stress testing or provider fallback occurs. Every
attempt is paced, including pages and retries. Retry-After seconds and HTTP dates
are respected. Excessive delays are persisted before stopping all remaining
collection requests; restarting cannot bypass the saved deadline. The CLI holds a fixed
per-user `massive.lock` for the complete run, independently of `--data-dir`.
Competing runs fail immediately. Cooldowns persist across process restarts.
This controls cooperating local processes; it cannot coordinate another machine
or unrelated applications using the same credential.

Fresh readiness remains blocked with `PUBLICATION_UNVERIFIED`, even when every
requested bar is present. A valid saved smoke report can therefore show
collection `FAILED` and data `PARTIAL`. Exit zero requires both reports and
complete, valid histories per listing. Only `PUBLICATION_UNVERIFIED` is ignored
for the command exit status; empty/short histories, other quality errors and
provider failures exit one.
The adapter records transport observations, not subscription or billing claims.
See [Massive evidence policy](docs/massive-evidence.md) for retained fields,
normalization, and the publication-timing evidence still needed.
