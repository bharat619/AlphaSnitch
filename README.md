# Stock market analyser

Architecture and provider-evaluation workspace. Documentation consolidated on **10 October 2026**. Application implementation has not started; the Python scripts here are isolated, read-only evaluation tools.

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

The scripts and dependency versions are in `evaluation/`. The API key stays in gitignored `.env.local`; no key is embedded in documentation or saved results. Virtual environments, provider cookie caches and raw results are also gitignored. The raw evidence is saved in this folder but would not be included in a future Git commit unless explicitly selected after checking retention/sharing requirements. No remote backup, publishing or repository push has been performed.
# AlphaSnitch
