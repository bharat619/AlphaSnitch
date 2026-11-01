# Phase 2 handoff — deterministic technical analysis

Prepared 1 November 2026. Status: review draft, not a frozen strategy specification.
Phase 1 fixture functionality can support offline implementation. Live provider
rights, pilot coverage and publication timing remain separate open gates. No
indicator, strategy, LLM integration or new trading recommendation is enabled by
this document. The owner wants direct involvement in Phase 2 decisions.

## What is established

- Market-aware instrument identity, immutable evidence snapshots, versioned
  configuration, independent daily/weekly history requirements and report replay.
- US-only public operation; synthetic second-market isolation tests.
- PostgreSQL 18 domain persistence, Alembic migrations and decimal-string ledger
  accounting. Collection resume is implemented without LangGraph.
- Price routing is selected from mandatory history requirements; a provider
  failure must not silently change sources. Compatible benchmark evidence is
  required. Raw, split-adjusted and total-return prices remain distinct.
- Phase 2 calculates indicators and causal setups deterministically. Agents arrive
  in Phase 3. Indicator values alone do not constitute approved trading signals.

## Contract to agree before implementing calculations

Each registry entry must define feature ID/version, parameters, timeframe, input
basis and units, dependencies, formula/seed, minimum history, extra warm-up,
availability reasons, implementation version and output units. Observations bind
snapshot ID, configuration hash, observed session and available-at timestamp.

Return a value or an explicit unavailable reason. Distinguish insufficient history,
invalid input, missing benchmark, zero denominator and unsupported feed. Optional
features may be unavailable under predefined policy; mandatory missing features
block that strategy. Never fill an unavailable value with zero or a neutral vote.

### Proposed formula conventions for review

These are concrete candidate conventions, not performance-validated parameters.
Confirm the full input window before each computation; do not drop missing sessions
or backfill from future bars. All recurrence seeds and warm-up points are part of
the versioned definition, including the first index with a reportable value.

- SMA: arithmetic mean of the last N completed closes. EMA: alpha 2/(N+1), initial
  seed equal to the first N-close SMA; recurrence starts at the next close. Daily
  periods 20/50/100/200 follow the architecture. Decide additional seed burn-in
  explicitly rather than inheriting a library default.
- Crossovers: compare two available series at t-1 and t; equality conventions must
  be defined. Distance from average: 100*(close/average-1), undefined at zero.
  Slope needs an agreed interval and normalization; do not call an unscaled price
  difference comparable across securities.
- RSI candidate: 14 close changes, Wilder smoothing alpha 1/14 seeded by simple
  means of gains/losses. Positive gains with zero losses yield 100; zero gains
  with positive losses yield 0; an entirely flat window is explicitly undefined.
- MACD candidate: EMA12 minus EMA26, EMA9 signal on the valid MACD sequence,
  histogram equal to MACD minus signal. Signal history includes both EMA seeding
  and signal seeding. ROC candidate: 100*(close[t]/close[t-N]-1), with N reviewed.
- Stochastic candidate: raw %K = 100*(close-low_N)/(high_N-low_N), N=14;
  three-period SMA smoothing for %K and three-period SMA for %D. A zero range is
  unavailable. Decide fast versus slow variant before naming the public feature.
- True range: maximum of high-low, abs(high-previous_close), and
  abs(low-previous_close). ATR candidate: Wilder14; first valid seed requires 14
  true ranges with valid previous closes. ATR% = 100*ATR/close.
- Bollinger candidate: SMA20 plus/minus 2 population standard deviations
  (ddof=0). Width = (upper-lower)/middle; price position =
  (close-lower)/(upper-lower). Define display as ratios or percentages explicitly.
- Historical volatility candidate: sample standard deviation (ddof=1) of 20
  close-to-close log returns, annualized by sqrt(252). The annualization constant
  belongs to the market profile. Zero/negative prices are invalid inputs.
- Relative volume candidate: current completed-session volume divided by the
  preceding 20 completed sessions' mean volume, excluding the current session.
  Preserve fractional volume. Zero mean denominator is unavailable.
- Relative strength: align security and benchmark observations to identical
  completed-session windows and return conventions; define the lookback before
  implementation. Do not mix total-return benchmarks with price-only stock returns.

Weekly context needs its own explicit feature subset and warm-up. Do not assume
260 daily bars can produce a 200-week average. The current pilot qualification has
no additional mandatory weekly feature; revise/version the applicable criteria
when new requirements are introduced.

### Causal detector contracts

- Recent extrema/breakout levels use prior completed bars, excluding the current
  bar being assessed. Persist the exact contributing bar IDs and lookback.
- Support/resistance needs an agreed trailing window or pivot definition,
  touch count, merging tolerance and confirmation time.
- A pivot using R right-hand bars is available only after those R bars close.
  Its signal time must never be backdated to the pivot date.
- Breakout/breakdown needs an agreed close buffer, persistence and volume rule.
  Pullback needs a qualifying trend, reference zone, tolerance and confirmation.
- Reversal detection may expose descriptive observations while reversal trading
  remains disabled. A candidate reversal and a confirmed reversal differ.

### Strategy decisions requiring owner participation

1. Which daily/weekly features are mandatory for breakout and pullback profiles?
   What history and warm-up does the longest mandatory dependency require?
2. Exact regime, setup, entry, stop/invalidation and target rules; reward/risk
   threshold; earnings-event source and blackout window. Unknown mandatory
   earnings evidence must block candidates. No threshold is evidence of profit.
3. Separate exit policies: thesis invalidation, confirmed trend/support failure,
   relative-strength deterioration and unavailable entry-thesis handling. Failure
   to qualify for a new buy does not imply a sale.
4. Evidence groups and cross-group caps: price trend/EMA/MACD cannot count as
   independent votes without a justified policy. Preserve all conflicting features.
5. How simultaneous breakout and pullback assessments resolve disagreement.
   Keep separate assessments and a fixed resolution policy.
6. Numeric calculation implementation and tolerances: compare maintained candidates
   against hand-computed fixtures, pin the selected implementation and dependencies,
   and record seed/rounding differences. No package selection or new dependency
   installation was made during Phase 1 verification.

Do not freeze these decisions implicitly through code. Review this draft with the
owner, amend concrete values, then assign the first accepted registry/policy versions.

## Implementation order and acceptance evidence

1. Registry and availability contracts, with independent dependency/history
   expansion for daily and weekly features. No providers called by calculations.
2. Small golden fixtures and a reference calculation notebook/script. Examples:
   SMA3([1,2,3])=2; EMA3 seed=2, next input 4 gives 3; prior close 100,
   high 105/low 99 gives TR=6; current volume 200 against prior mean 100 gives RVOL=2.
   These are proposed expected values, not executed Phase 2 tests.
3. Full indicator library in small reviewed increments. Add flat-price, monotonic,
   gap, zero-volume, insufficient-history, split and benchmark-alignment cases.
4. Causal price-action detectors. Append future bars and assert earlier published
   observations do not change; pivot availability must remain delayed correctly.
5. Versioned breakout/pullback strategy policies, separate Buy/Sell gates and
   evidence grouping. Test mandatory blockers, optional absence and contradiction.
6. Persist observations/setups and render every enabled feature, including unused
   and unavailable values. Offline replay must reproduce calculations and reports.

At each step, the owner should review a small worked example and its tests before
moving to the next component. Finish with one complete synthetic stock trace from
bars to features, setup, gates and report, plus deliberate failure cases. Historical
evaluation and prospective paper observation remain Phase 5; Phase 2 tests do not
validate profitability.

## Completion criterion

This handoff becomes frozen only after the open parameter/implementation decisions
are recorded, golden fixtures run against the selected pinned implementation, and
the resulting history contract is checked against provider capabilities. Until then
Task 9 of the Phase 1 verification checklist is PARTIAL (draft prepared).
