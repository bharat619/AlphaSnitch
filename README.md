# Stock market analyser

Local Python CLI foundation for AlphaSnitch, plus architecture and provider-evaluation work. Issue [US-001](https://github.com/bharat619/AlphaSnitch/issues/1) adds installation, configuration, the command menu, and setup diagnostics. Buy, Sell, Portfolio, Record offline trade, and Reports are visibly **unavailable** until their implementation stories land. The Python scripts in `evaluation/` remain isolated, read-only evaluation tools.

## Market-aware contracts (US-002)

Version 1 Pydantic contracts live in `stock_analyser.contracts`. Securities and listings have immutable UUID identities; provider symbols use half-open effective-date intervals. Instrument lookup requires a market and rejects ambiguous listings unless a venue is supplied. Frozen contracts use tuples for nested collections and reject unknown fields and unsupported schema versions.

`MarketRegistry()` enables US only and supplies versioned calendar references, currency, provider routes, feed coverage, capabilities and benchmarks. Calendar references are adapter contracts, not a bundled exchange schedule. US history coverage describes the initial configured policy; it does not verify live entitlements. Provider protocols cover prices, universe, research, corporate actions, calendars and LLMs. Live HTTP adapters and history-selection policy execution belong to later stories.

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

## Install and launch

Use **Python 3.12** and [uv](https://docs.astral.sh/uv/getting-started/installation/). From a clean checkout:

```sh
uv python install 3.12
uv sync --locked
uv run --locked stock-analyser --help
uv run --locked stock-analyser
```

`uv.lock` pins runtime and development dependencies; `--locked` refuses to silently update it. The build backend version is pinned separately in `pyproject.toml`. `.python-version` selects Python 3.12 and the package restricts its supported baseline to 3.12. The first installation downloads packages; launching help, the menu, and setup commands needs no provider credentials or network. Evaluation results and `.env.local` are not required or loaded by the application.

The menu shows the active US market and six actions: Buy, Sell, Portfolio, Record offline trade, Reports, and Exit. In an interactive terminal, use **↑/↓** or numbers **1–6** to highlight an action and **Enter** to select it. Selecting an unavailable action explains its status and returns to the menu with that action highlighted. Select Exit or press Ctrl+C to quit. Piped input and terminals without interactive support use a numbered prompt, where `6`, an empty line, or EOF exits.

For an installation without development tools, use `uv sync --locked --no-dev`. After syncing, the entry point is also available as `.venv/bin/stock-analyser` (Windows: `.venv\Scripts\stock-analyser.exe`), or after activating the environment as `stock-analyser`.

## Local configuration and runtime files

```sh
uv run --locked stock-analyser doctor
uv run --locked stock-analyser --config config.example.toml doctor
uv run --locked stock-analyser --data-dir /absolute/path/to/runtime init
```

`doctor` prints resolved directories, package version, Git commit and dirty state when available, and a SHA-256 fingerprint of the effective runtime configuration. It makes no provider calls or writes. `init` creates directories only; database migrations and business rules belong to later stories. The menu and help do not create runtime files.

Copy [config.example.toml](config.example.toml) to the user configuration location shown by `platformdirs`, or pass `--config /path/to/config.toml`. Defaults are outside the source checkout:

- macOS: configuration at `~/Library/Application Support/stock-analyser/config.toml`; runtime data at `~/Library/Application Support/stock-analyser/`.
- Linux: configuration at `~/.config/stock-analyser/config.toml`; runtime data at `~/.local/share/stock-analyser/` (respects XDG directory settings).
- Windows: configuration and runtime data under `%LOCALAPPDATA%\stock-analyser\`.

The data directory contains `database/`, `snapshots/`, `cache/`, `reports/`, and `logs/`. These are reserved runtime locations; this story logs only to stderr. Each directory can be overridden independently. Relative configured paths resolve beside the configuration file; relative CLI/environment overrides use that same base (the default user configuration directory when no file is supplied). Prefer absolute paths for overrides.

Configuration precedence is CLI options → environment variables → TOML → defaults. Supported variables are `STOCK_ANALYSER_CONFIG`, `STOCK_ANALYSER_MARKET`, `STOCK_ANALYSER_DATA_DIR`, `STOCK_ANALYSER_DATABASE_DIR`, `STOCK_ANALYSER_SNAPSHOTS_DIR`, `STOCK_ANALYSER_CACHE_DIR`, `STOCK_ANALYSER_REPORTS_DIR`, and `STOCK_ANALYSER_LOGS_DIR`. Explicit child-directory overrides take precedence over the data-directory default. A missing explicitly requested configuration file, malformed TOML, unknown settings, or unsupported market fails clearly. Only `US` is enabled.

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

Tests cover offline menu/help, configuration precedence and path creation, unsupported markets, missing/invalid configuration, credential redaction in errors and exception logs, and version/configuration recording. Update dependencies intentionally with `uv lock`, then rerun the focused checks. Provider calls, database rules, reports, and trading decisions are outside US-001.

## Continuous integration

[GitHub Actions](.github/workflows/ci.yml) runs on pull requests targeting `main`, pushes to `main`, and merge-queue checks. The `Tests (Python 3.12)` check installs locked dependencies on Ubuntu, runs the test suite, checks lint and formatting, and launches CLI help. It needs no provider credentials. Actions and uv versions are pinned; superseded runs are cancelled automatically.

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
