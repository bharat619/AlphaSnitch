# Technical breakout qualification, version 1

This slice combines the accepted daily breakout, stock-trend and volume conditions
into an offline technical assessment. It follows ATR14 PR #46. `PASS` describes
only the technical setup: `recommendation_status` remains `NOT_EVALUATED`.
Foundation eligibility, rights/provider qualification, usable ATR and risk levels,
forward earnings coverage, costs and advisory sizing remain separate gates.
No buy/sell workflow, report pipeline, provider calls or database schema changes
are enabled here.

## Policy and outcomes

`BreakoutPolicy` requires an explicit profile: `stock` or `stock_with_spy`.
Both use policy `daily-breakout-1` and implementation `exact-breakout-1`.
Every enabled group must pass on the same completed assessment session t:

1. Breakout: close[t] strictly exceeds the maximum high of the preceding twenty
   expected sessions. Equality fails. The current high is excluded, and an
   intraday touch with a lower close cannot qualify.
2. Stock trend: close[t] > SMA50[t] > SMA200[t], and SMA50[t] > SMA50[t-10].
   These three comparisons form one group. The slope is the net change over ten
   exchange sessions, not a requirement to rise every day.
3. Volume: current volume divided by the preceding twenty-session mean is at
   least 1.5. Equality passes. This requires twenty prior sessions plus t, with
   verified split-consistent volume and 23,400-second full regular sessions.
   Missing/shortened sessions are not replaced with older full sessions.

The stock-with-SPY profile additionally requires the same three trend comparisons
for the configured SPY benchmark. Its permanent use is not adopted: this is the
accepted research comparison variant. The stock profile reports this group as
`NOT_ENABLED`; supplied SPY arguments are not evaluated or retained in that profile.
This does not disable any benchmark requirement imposed by the foundation.
The breakout-only and breakout-plus-trend diagnostic baselines for later research
are not exposed as qualifying recommendation profiles.

Each enabled group is `PASS`, `FAIL`, or `UNAVAILABLE`. Any missing mandatory
comparison or blocking evidence makes that group unavailable. All groups are
evaluated: a failed price gate cannot hide missing volume. Aggregate unavailability
takes precedence over failure, but every failed comparison and unavailable reason
is retained. With complete evidence, any failed group yields `FAIL`; otherwise
the technical result is `PASS`. There are no confidence scores, weighted votes,
compensating indicators, or implicit exit instructions.

## Exact decisions and retained inputs

The prior-high comparison uses validated Decimal prices without tolerance.
The assessment retains the twenty prior highs and signal close; `breakout_level`
derives their exact maximum for later freezing with risk evidence.

Stock and SPY trend use the existing exact SMA sums. For example, comparing
SMA50 to SMA200 is equivalent to comparing four times the fifty-close sum to the
two-hundred-close sum. No displayed SMA is used for a policy decision.

The volume gate retains all twenty validated prior volumes and the current volume,
including fractional shares. With a positive prior sum, its exact comparison is
`40 * current_volume >= 3 * sum(prior_volumes)`, evaluated with Fraction arithmetic.
A zero prior sum is unavailable; zero current volume with a positive baseline is
a valid failure. A ratio slightly below 1.5 can round to the displayed value 1.5
and still fail. Display precision never supplies an epsilon or changes a boundary.

The result embeds the full descriptive observations, their versions, specification
and provenance hashes, source/row references, exact policy inputs, policy hash,
calendar resolution, cutoff, availability, individual comparisons and group
outcomes. The calculation timestamp is separate from evidence availability.

## Sessions, publication and source compatibility

`qualify_breakout` resolves the latest expected completed daily session from the
supplied versioned calendar and explicit `as_of`/`CutoffPolicy`. Before the next
session becomes due under that policy, the preceding completed session is the
legitimate target. Once t is due, missing t produces unavailable evidence; the
engine never searches backwards for a passing setup. Replaying an older assessment
uses its original cutoff, not a new cutoff with an older date label.

Calendar grace does not establish publication. The default cutoff policy is
unverified and blocks qualification. Explicit publication assertions remain
bound to the snapshot and do not override existing packet publication blockers.
Mandatory evidence must be available by the cutoff. Historical retrieval cannot
be backdated into historical availability.

Existing observation validation handles OHLC, volume, duplicate/missing dates,
input ordering, basis, timestamps, field-local quality and whole-packet blockers.
All retained source hashes are authenticated before calculation; a broken hash
chain raises instead of returning salvaged values. Input/calendar unavailability
is represented explicitly. The technical policy is US/USD only; synthetic
second-market inputs remain unavailable rather than inheriting US settings.

For the SPY variant, `resolve_spy_binding` resolves the US market's `broad_market`
mapping (SPY, ARCX, USD) through an `InstrumentRegistry`, retaining the immutable
instrument and a reference-verification audit reference. The caller is responsible
for establishing that verification, just as for publication sidecars. The snapshot
must match this binding and the stock market configuration, selected provider/feed
and price basis. It must support trend observations at the stock's exact t.
An older SPY snapshot, alternate source, conflicting identity or unknown benchmark
provenance blocks the variant while preserving all stock comparisons.

## Dependency expansion and informational weekly context

The combined stock technical window needs 200 split-adjusted daily sessions:
SMA200 dominates the 60-session lagged-SMA and 21-session breakout/volume windows.
It does not need 210 bars. A technical PASS with 200 bars does not clear the separate
260-session foundation eligibility floor or prove ATR readiness.

`breakout_dependencies(policy, selection, benchmark_foundation)` expands the
existing selection requirements while preserving their longer histories and
adjustment bases. It keeps the raw-price 260-session minimum, adds the stock's
200-session split-price requirement, and adds 200 split-price benchmark sessions
only for the SPY variant. Existing foundation benchmark requirements must be
supplied explicitly and are retained even when that variant is off. A previously
registered ATR or other longer dependency is never shortened by this expansion.

Weekly context requests 34 completed weeks separately with `mandatory=False`.
The existing 30-week SMA/four-week slope calculation is embedded for information,
with its own daily/weekly dates, availability and unavailable reasons. Supports,
opposes, mixed and unavailable cannot change technical status, rank, risk levels,
size or confidence. Optional weekly evidence also does not delay the assessment's
mandatory-evidence `available_at`. Stock/sector relative strength remains explicitly
`NOT_ENABLED`.

## Reproduction and worked examples

The synthetic example uses enumerated weekday sessions of 6.5 hours. It does not
claim these are actual US holiday sessions or live evidence:

- Close 360, prior high 359, rising stock trend, RVOL exactly 1.5: `PASS`.
- Close equals prior high 359: `FAIL`, with the trend and volume groups retained.
- RVOL 1.4996: `FAIL`, even though a two-decimal display could read 1.50.
- Missing current volume: `UNAVAILABLE`, with the passing price/trend groups kept.
- Passing stock conditions with flat SPY trend: the enabled SPY variant `FAIL`s.

From the implementation checkout with its correctly installed locked environment:

```sh
uv run --locked python -m stock_analyser.breakout_example --output /absolute/path/to/breakout-example
uv run --locked pytest -q tests/test_breakout_qualification.py tests/test_atr_observations.py tests/test_sma_observations.py tests/test_weekly_context.py tests/test_indicator_contracts.py tests/test_breakout_observations.py tests/test_calendars.py
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
```

The example writes complete structured assessments and repeats identically in fresh
processes. If sharing another worktree's editable environment, explicitly set
`PYTHONPATH` to this checkout's `src` so tests import the intended implementation.

`replay_breakout(assessment, calendar, calculated_at=...)` recalculates from the
retained snapshots and original cutoff, then verifies semantic equality while
excluding calculation time. Schema validation alone checks internal consistency;
it cannot authenticate altered exact inputs just because rounded displays agree.
Replay verifies those outputs against retained evidence. Corrections use new
snapshots/assessments; replay of original evidence retains the original result.
Database/report integration for these assessments remains a later slice.

For the full PostgreSQL 18 gate, set `STOCK_ANALYSER_TEST_DATABASE_URL` to an
isolated disposable server and run:

```sh
uv run --locked python scripts/foundation_release.py --output /absolute/path/to/new-breakout-evidence
```

Do not use `--live` for fixture verification. The implementation tests do not
qualify live providers or establish strategy profitability. Verification results
and tested source hashes are retained in `docs/evidence/breakout-qualification-verification.json`.

Verification on 15 November 2026: 286 focused tests passed, including 54 new
qualification cases. The full committed-source PostgreSQL 18 gate passed all
855 tests with zero failures, errors or skips, including complete test collection
and the fresh-process persistence checklist. Lint/format, whitespace, direct code
review and byte compatibility of all four existing walkthroughs passed. The
implementation revision is `0efdd5f`; the verification commit changes documentation
and evidence only.
