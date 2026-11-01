# Phase 2 handoff — deterministic technical analysis

Updated 1 November 2026 after the breakout re-evaluation and creation of Issue #41.
Status: first calculation issue specified; implementation has not started. The
broader strategy specification remains a review draft.
Phase 1 fixture functionality can support offline implementation. Live provider
rights, pilot coverage and publication timing remain separate open gates. No
indicator, strategy, LLM integration or new trading recommendation is enabled by
this document. The owner wants direct involvement in Phase 2 decisions.

## Resume here in a new session

1. Read this handoff, the relevant technical-analysis sections of
   [ARCHITECTURE.md](../ARCHITECTURE.md), and
   [Issue #41: Define and verify prior-high and relative-volume observations](https://github.com/bharat619/AlphaSnitch/issues/41).
   The issue contains the full scope, acceptance criteria and verification steps.
2. Verify Git/PR state before editing. Phase 1 [PR #40](https://github.com/bharat619/AlphaSnitch/pull/40)
   is merged into main at `f9bb159`; its Linux and Windows CI passed. The verified
   corrected suite had 595 passing tests with zero failures/errors/skips. See the
   [Phase 1 verification record](phase-1-verification-2026-11-01.md).
3. The next implementation task is **Issue #41 only**, on a feature branch from
   current main. Check whether this documentation handoff has been merged before
   starting; do not lose newer handoff content when switching branches. Preserve
   unrelated untracked `.agents/`, `.cursor/rules/code-review.mdc`, `evaluation/`
   and `planning/` content. No Phase 2 application code or tests have been written.
4. Implement in three small steps: typed input/output and availability contracts;
   pure calculations; then an executable synthetic walkthrough, focused tests and
   regression verification. Reuse Phase 1 evidence/calendar contracts without
   broad refactoring or changing historical snapshots/reports.
5. Present the worked example and passing/failing/unavailable results to the owner
   with the PR. Do not automatically continue through the full indicator library
   or activate a strategy. A complete breakout policy and its evaluation protocol
   are subsequent design work.

### Working agreement with the owner

- The owner relied heavily on AI during Phase 1 and wants to understand Phase 2
  deeply. Explain each small component with concrete numbers and observable test
  outcomes. Distinguish established definitions from untested trading hypotheses.
- **Every issue created must include explicit acceptance criteria and step-by-step
  testing/verification instructions**, including exact runnable commands once
  implementation paths are known and the expected outcomes. Do not mark an issue
  complete solely because code exists or a narrow test subset passes.
- Use feature branches and PRs; never push directly to main or merge without an
  explicit user request. There is no authorization for automatic queue execution
  or automatic merging.
- The owner reconfirmed no new account-specific provider permission evidence.
  Live qualification remains blocked in [#38](https://github.com/bharat619/AlphaSnitch/issues/38)
  and foundation release acceptance in [#18](https://github.com/bharat619/AlphaSnitch/issues/18).
  Use synthetic fixtures; do not clear rights/publication gates or dispatch live
  collection simply because offline tests pass. No provider/model calls are needed
  for #41. Bedrock and agent debate remain Phase 3.

### Breakout discussion: latest decision supersedes the initial example

The owner chose breakout as the first learning example. The initial illustration
used a close above the prior 20-session high and relative volume at least 1.5x.
After re-evaluation, those values remain **illustrative, unvalidated parameters**,
not frozen production defaults or a validated trading strategy. Zero price buffer
and one completed close likewise remain provisional. No empirical performance
test of this combination has been performed.

First build independently checkable observations, not a confirmed-breakout or
BUY/SELL decision. A rolling high is not proof of established resistance or a
consolidation. A passed numerical condition does not prove future price persistence.
Missing volume must not erase an independently valid price observation, and
packet/hash integrity failures must never be bypassed to salvage values.

### Issue #41: exact first increment

- Explicit independent positive lookbacks for price and volume; no hidden 20-session
  production default. The immediately preceding expected completed sessions supply
  each baseline, excluding assessment session t. Do not substitute older rows for
  missing sessions. N prior sessions plus t are needed when lookbacks are equal.
- Compute prior high, prior mean volume, relative volume, close-minus-prior-high
  distance, and the descriptive strict comparison close > prior high.
- Preserve fractional volume and numeric precision. Positive baseline with zero
  current volume gives RVOL=0; zero baseline gives unavailable. Define independent
  statuses/reasons and field-local versus whole-packet invalidity.
- Preserve consistent split-adjusted technical price evidence and a verified
  consistent volume adjustment/feed convention. Raw liquidity screening is a
  separate use. For the initial session-scope policy, shortened sessions in the
  current or volume-baseline window make RVOL unavailable; valid prices can remain
  available. No silent duration normalization or replacement with older sessions.
- Record immutable evidence lineage and causal times. A close timestamp does not
  prove the data was published or known then. Unknown historical availability
  cannot support earlier backtests; existing PUBLICATION_UNVERIFIED stays blocked.
- No new indicator package, operational scan command or schema migration is required.
  Preserve Phase 1 reports and NOT_RUN strategy stages. No volume threshold, ATR
  buffer, entry/exit policy or profitability claim is enabled by #41.

Owner walkthrough fixture (N_price=N_volume=5): prior highs
`98, 100, 99, 101, 100`; prior volumes
`800000, 1000000, 1200000, 900000, 1100000`; current high `104`, close `102`,
volume `1600000`. Expected prior high **101**, prior mean volume **1000000**,
relative volume **1.6**, close distance **1**, above-prior-high **true**. Today's
high and volume are excluded from both baselines. Supply consistent remaining
OHLC and synthetic calendar/provenance metadata. Also demonstrate close equal to
101 (above=false), close below despite a higher intraday high, and missing volume.

Required verification includes independent hand-computed fixtures, N=1/5/20,
insufficient history, missing/duplicate sessions, fractional/zero/invalid volume,
mixed identity/feed/basis rejection, shortened sessions, publication/knowledge-time
checks, future-bar append invariance, immutable correction/replay, legacy regression
and no ledger/provider side effects. The issue lists the focused test commands,
Ruff checks and full foundation-release checklist; use an isolated PostgreSQL test
server and a new evidence output directory. Document the final example command
and test paths in the implementation PR.

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
- Relative volume: current completed-session volume divided by the preceding
  explicitly configured N completed sessions' mean volume, excluding the current
  session. The earlier N=20 illustration is not a production default. Preserve
  fractional volume. Zero mean denominator is unavailable. See the #41 session-scope
  policy above for shortened sessions.
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

Start with the narrowly scoped contracts, observations and walkthrough in #41
above. The following is the broader roadmap, not permission to implement every
step automatically before owner review.

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
