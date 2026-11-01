# Next phase: data and CLI foundation

Planning date: 10 October 2026. Status updated 1 November 2026: Phase 1 foundation code is implemented; live pilot qualification remains incomplete. This document preserves the original acceptance scope. Current results are in [Phase 1 verification](docs/phase-1-verification-2026-11-01.md).

This plan turns Phase 1 of [ARCHITECTURE.md](ARCHITECTURE.md#validation-and-delivery-phases) into deliverable work. The architecture remains the source of truth for product scope. [Provider evaluation findings](evaluation/REPORT.md) establish the starting evidence, not production acceptance.

The [development backlog](planning/github-issues/README.md), prepared on 11 October 2026, splits F1–F6 into deliverable-sized user stories for GitHub. It tracks dependencies and publication status separately from this phase-level plan.

## Outcome

Build a local US CLI that can select a sector, reproducibly select up to ten eligible stocks, collect validated evidence, and save an inspectable report. It must also record explicitly confirmed offline executions and collect evidence for every open holding in a sell scan.

This milestone produces **data-readiness reports**, not trading recommendations. Indicator calculations, strategy decisions, and agent debate enter subsequent phases. Reports must identify unimplemented stages as `NOT_RUN`; successful data collection must never imply BUY, SELL, HOLD, or REJECT.

The milestone is complete when both buy and sell collection paths work with persisted state, failures remain visible, and the acceptance scenarios below pass. A hard-coded ticker demo alone does not complete it.

## Starting point and preserved decisions

- At planning time application implementation had not started. The foundation now implements F1–F6 machinery and fixture workflows; live acceptance still requires the evidence gates below. Original provider probes remain separate from application adapters.
- Sampled Massive daily prices, benchmarks, corporate actions, news, and pagination have passed evaluation. Full sector enumeration, classification, earnings corroboration, publication timing, and correction handling remain open.
- Massive stays the default price source. Route to yfinance only when mandatory history exceeds configured Massive coverage, as already agreed. Fetch the stock's daily/weekly evidence and compatible benchmarks from the selected price source; an outage does not authorize a source switch.
- US is the only enabled market. Instrument identity, calendars, currencies, and provider routes remain market-aware from the first schema.
- Buy scans select ten distinct eligible securities by the documented liquidity policy. Sell scans include every open holding, regardless of buy eligibility.
- Executed trades are authoritative. Scans and selection of candidates cannot change holdings.
- Bedrock remains the selected LLM provider. Model selection does not block this foundation.
- The workspace currently has no Git repository. Local version-control setup belongs in the first implementation package; a remote or publishing is a separate action. Until then, this plan is a local file.

## Delivery order

Implement six small work packages. Each should end with a runnable demonstration and its relevant checks. Estimates and calendar commitments should follow the universe spike, because full-sector request volume is not yet measured.

### F1 — Runnable project and contracts

**Deliver:** the Python package and CLI entry point, a reproducible dependency lock, configuration loading, test commands, and the first migration. Proposed tooling baseline: Python 3.12 and uv, matching the existing evaluation environment; retain the architecture's Typer/Rich, Pydantic, and SQLAlchemy/Alembic choices.

- Introduce the market registry and US configuration, instrument/security identity, provider capabilities, history requirements, analysis requests, evidence manifests, run/stage statuses, and report artifact records.
- Define provider interfaces and fixture adapters. Only create modules and tables used by this phase; add agent assessments and intents with their implementing phases.
- Separate operational status, data quality, and strategy verdict. Give schemas explicit versions and preserve missing-data reasons.
- Resolve application data directories outside source for databases, snapshots, caches, and reports. Include a credential-free configuration example and redact secrets from errors/logs.
- Establish source/configuration version recording. Initialize local Git during implementation so changes are reviewable; no remote is needed.

**Acceptance:** a clean installation exposes CLI help and the Buy/Sell/Portfolio/Record trade/Reports menu; configuration rejects unsupported markets; migrations work on an empty database. Synthetic fixtures prove that identical ticker text in two markets does not collide and currencies are retained.

**Dependency:** none. **Demonstration:** initialize local state and resolve an example US instrument without network access.

### F2 — One-instrument evidence-to-report path

**Deliver:** a fixture-backed application service that takes one instrument through cutoff resolution, evidence validation, persistence, and all three report artifacts.

- Use an exchange-calendar adapter to resolve completed daily sessions and weeks, including early closes and daylight-saving transitions. Freeze the requested and resolved cutoffs.
- Store snapshot IDs, source hashes, retrieval times, requested/returned history, adjustment/feed metadata, and quality findings. Preserve fractional volume.
- Implement immutable evidence storage and readable `report.md`, structured `report.json`, and `manifest.json` with matching run/snapshot IDs.
- Persist run and artifact states separately. Write artifacts atomically and support idempotent regeneration from persisted data.
- Keep the collection coordinator simple. LangGraph's agent checkpoints arrive with debate; do not build two competing workflow engines.

**Acceptance:** a fixture run produces all three artifacts and can be reopened through `report show`. A missing bar, stale response, and unwritable report destination are visible failures; regeneration succeeds without recollecting evidence. No strategy verdict is emitted.

**Dependency:** F1. **Demonstration:** the first complete evidence report, entirely offline.

### F3 — Live data adapters and history routing

**Deliver:** Massive and yfinance price adapters, benchmark collection, corporate-action metadata, a shared cache, and the agreed capability-based router.

- Implement history requirements independently of the future indicator engine; Phase 2's registry will supply them. Test short daily and longer mandatory weekly requirements explicitly.
- Apply one provider-wide limiter across requests, pagination, retries, and stock workers. For simultaneous CLI processes, initially enforce a single active collection process using a local lock; do not multiply the configured quota per process.
- Bound timeouts and retries, respect retry delays, validate pagination destinations, and checkpoint completed collection units. Use simulated 429/5xx responses rather than live quota stress.
- Cache with provider, instrument, market, feed, timeframe, adjustment policy, and version metadata. Refresh a documented overlap window to detect corrections; corrections create new snapshots while old runs retain their evidence.
- Explicitly configure yfinance adjustment/repair behavior and validate weekly completeness and per-instrument history. Split-adjusted technical prices and total-return prices must remain distinguishable.
- Record Massive news discovery with source IDs, timestamps, relevance/coverage status, and permitted retained content. Model earnings status as known, unknown, or conflicting; collection does not resolve uncertainty by itself.

**Acceptance:** live sample collection produces a valid report; warm-cache replay makes no network calls when the requested immutable snapshot is already available. Fixtures prove long-history routing, insufficient-history abstention, consistent stock/benchmark sources, and no switch on provider failure. Interrupted collection retains completed evidence and resumes with the original cutoff; a changed cutoff creates a new run.

**Dependency:** F2. **Demonstration:** the same instrument through fixture replay and the live adapter, with provenance visible in each report.

### F4 — Sector universe and reproducible buy collection

**Deliver:** full enumeration for one supported pilot sector, classification/eligibility enrichment, exclusions, deterministic ranking, and a ten-stock buy collection report. Proposed pilot: Technology, because relevant price samples already exist; this does not establish full sector coverage.

- Start by testing complete pagination and coverage. Evaluate Massive reference data plus yfinance classification/discovery against the documented common-equity scope; preserve taxonomy source, mapping version, and retrieval time.
- Deduplicate securities, exclude unsupported listings/security types, and surface missing classifications. Show the eligible, excluded, and unresolved populations rather than asserting completeness from a sample.
- Calculate trailing 60-session average traded value under a documented convention, then rank by value with instrument ID as tie-breaker. Apply configured price, liquidity, and mandatory-history filters first.
- Freeze the universe snapshot, ranking inputs, selected instruments, and exclusion reasons. Persist the liquidity observations needed to reproduce selection; the full indicator library is not required yet.
- Measure full-sector cold-start and warm-refresh requests, elapsed time, and unresolved coverage. Cache metadata separately from price histories and reuse price data already fetched for ranking.
- Enable additional sectors only after the same coverage checks pass. A curated list may support a clearly labeled development fixture, but cannot satisfy the dynamic-sector acceptance gate.

**Acceptance:** `scan buy --sector technology` selects the same instruments from the same snapshot and configuration; all selected stocks appear in the report. Fewer than ten eligible securities produces an explicit shortfall. Incomplete enumeration is reported as incomplete and cannot masquerade as the sector's top ten.

**Dependency:** F3 for completion; the coverage spike can begin after F1. **Demonstration:** a dynamic pilot-sector scan with reproducible ranking and exclusions.

### F5 — Confirmed trade ledger and sell collection

**Deliver:** execution recording, derived holdings/lots, execution receipts, and collection for all open holdings.

- Require an actual execution confirmation containing instrument, quantity, price, currency, fees, and execution timestamp; recommendation provenance stays optional. Show a confirmation summary before writing.
- Use decimal arithmetic and lossless storage, transactional share-availability checks, idempotency keys, FIFO lots, and explicit currency handling. Unknown fee conversion leaves affected accounting incomplete rather than inventing an FX rate.
- Support purchases, partial/full sales, separate fills, and auditable corrections. Apply split events idempotently; surface ambiguous corporate actions for reconciliation.
- Sell collection reads a portfolio revision and includes every open position, including more than ten and holdings ineligible for new purchases. Disclose portfolio changes during a scan.
- Keep bulk existing-holding import and recommendation-linked intents for the end-to-end phase. Independently executed trades can already be entered individually.

**Acceptance:** a synthetic purchase of 10 shares at USD 100 followed by a sale of 4 at USD 110, with zero fees, leaves 6 shares, USD 600 basis, and USD 40 realized P&L. Replaying the same confirmation changes nothing; attempting to sell 7 remaining shares fails. Verify full close, split replay, correction, competing sell writes, and currency isolation.

**Dependency:** F1/F2 for ledger work, F3 for live sell collection. **Demonstration:** record confirmed fixture trades and run a sell collection over 12 open fixture holdings without truncation.

### F6 — Integrated foundation acceptance

**Deliver:** connected menus/commands, setup instructions, failure recovery, and a documented release checklist for this milestone.

- Connect buy collection, sell collection, portfolio, trade recording, report history/show, and collection resume. Mark analysis/debate as unavailable in this milestone rather than displaying simulated recommendations.
- Use synthetic fixtures for repeatable offline checks. Saved provider responses can support local contract checks where permitted; the gitignored evaluation results cannot be a required dependency of a clean installation.
- Run opt-in live smoke checks with existing configured provider access. Record observed capabilities and remaining limitations without claiming an account tier from successful HTTP responses.
- Document runtime paths, cache refresh, interrupted-run recovery, report regeneration, and database migration/backup procedures.

**Acceptance:** all release scenarios below pass, and the pilot-sector coverage gate is resolved. The milestone may be demonstrated with fixtures before live readiness, but must retain that distinction in its status.

**Dependency:** F4 and F5.

## Decisions and when they block progress

**Before F4 can pass:** resolve the pilot sector's classification source/mapping, enumeration completeness, listing eligibility, exact traded-value convention, and initial configurable price/liquidity thresholds. The implementation spike should propose concrete values and measured request costs. Thresholds are evaluation hypotheses, not evidence of strategy profitability. If free coverage cannot meet the scope, present the specific gap and smallest verified alternative before any paid purchase.

**Before accepting live data as fresh:** specify calendar/version, publication grace period, refresh overlap, and cache-validity policy. Reuse the passed daily-price samples, then add observations around actual closes and corrections. Until timing is validated, report the limitation and fail closed where freshness is unknown. This does not prevent fixture development.

**Before Phase 2 strategy acceptance:** pin indicator implementation and formula conventions; finalize daily/weekly requirements, detector windows, entry/exit gates, evidence-group caps, and earnings-event policy. Draft this configuration while finishing the foundation. Do not let arbitrary library defaults silently make these decisions.

**Before enabling a candidate whose policy requires earnings clearance:** resolve forward-earnings corroboration and event-timezone conflicts. Foundation reports can carry unknown/conflicting events. Unknown must block the applicable candidate gate later; it does not block building the CLI or ledger.

**Before Phase 3 live inference:** choose Bedrock region, role-specific models/inference profiles, verified access/quotas, and token/cost ceilings. Budget a ten-stock baseline of 30 role calls plus bounded repair/retry allowance. Fixture-only interface work can proceed before these choices; live paid inference needs an established budget.

**Before retaining or sending provider content:** resolve the applicable retention and model-use permissions identified in the architecture. Keep this targeted to the selected sources and actual stored/sent fields; it does not require another broad provider comparison.

India provider selection, multi-market user flows, additional strategy families, a terminal dashboard, and remote hosting do not block this phase.

## Foundation release scenarios

1. **Reproducibility:** an identical frozen universe, data snapshot, and configuration reproduces selections, hashes, and report content, apart from explicitly variable artifact-generation metadata.
2. **Coverage:** ten eligible pilot-sector securities are selected where available; shortfalls and incomplete enumeration are distinct, visible outcomes.
3. **Data integrity:** holiday/early-close cutoffs, incomplete weeks, missing/duplicate bars, fractional volume, split adjustments, and correction refresh behave as specified.
4. **Routing:** mandatory long history selects yfinance consistently for stock and compatible benchmarks; neither unsupported history nor provider failure silently changes the policy.
5. **Persistence/recovery:** partial and total collection failures still produce reports; cancellation retains completed work; resume preserves the original snapshot policy; failed report writes can be retried.
6. **Ledger correctness:** duplicate confirmations, partial/full sells, over-selling, splits, corrections, concurrent writes, and fee/currency handling pass focused accounting tests.
7. **Workflow separation:** buy/sell collection never changes holdings; recording actual independent trades does not require a recommendation; missing analysis stages never become verdicts.
8. **Portability:** synthetic second-market instruments verify symbol isolation, separate currencies, provider routes, and independent cutoffs while the public CLI continues to reject unsupported markets.

## Handoff to subsequent phases

- **Phase 2 — deterministic analysis:** implement the full configured indicator library with availability reasons, causal detectors, and separate breakout/pullback buy and exit policies. Produce evidence-rich technical assessments; validate known fixtures and absence of look-ahead before adding debate.
- **Phase 3 — agent debate:** integrate Bedrock and LangGraph, structured challenger/defender/judge outputs, evidence validation, mandatory final gates, budgets, and per-stock checkpoint recovery.
- **Phase 4 — complete user workflow:** add candidate-linked intents, execution reconciliation, existing-holding import, and the remaining report/history interactions.
- **Phase 5 — evaluation before reliance:** compare deterministic strategy versus strategy plus debate using predefined held-out/paper-observation criteria, costs, drawdowns, evidence errors, consistency, latency, and model expense. Point-in-time research limitations remain explicit.
- **Phase 6 — India:** qualify its data sources, calendars, venues, currency handling, and strategy profiles before enabling the market.

**First implementation task:** F1 followed immediately by F2. The first reviewable result should be an installable CLI that saves and reopens a one-instrument fixture report. That establishes the contracts and audit trail before live provider complexity or model expense is added.
