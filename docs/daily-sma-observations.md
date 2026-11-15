# Daily SMA observations, version 1

This slice extends merged PR #43 with completed daily-close means and a descriptive
three-condition trend assessment. It implements SMA50[t], SMA200[t] and
SMA50[t-10]. It does not enable breakout recommendations, weekly context, ATR,
position sizing, execution or live qualification. The broader planning drafts
remain separate from this implementation branch.

## Calculation and history contract

`SMASpec` declares a positive integer period and a nonnegative integer session
offset (default zero). Booleans, fractional numbers and strings are rejected for
these parameters. Formula `sma-close-1` and implementation `daily-sma-1` use daily,
split-adjusted closes and no recurrence warm-up. The mean includes the window's
last completed close. For an offset of ten, that last close is ten exchange
sessions before the explicit assessment session, not ten calendar days.

The declared history requirement is period plus offset: 50, 200 and 60 sessions
for the three daily-trend components. `sma_history_requirement` combines these
daily split-price dependencies into 200 sessions. It does not replace the separate
260-session universe eligibility floor, or other mandatory benchmark/provider
qualification. The same calculation accepts either stock or benchmark evidence;
no ticker-specific behavior or SPY market filter is enabled.

`observe` continues to authenticate the entire snapshot and raw source chain
before interpreting rows. Each SMA revalidates its own expected window's OHLC,
identity and publication evidence. Missing volume alone does not block usable
prices. Missing or duplicated required sessions cannot be replaced with older
bars. Unknown packet-wide quality failures, unordered source sessions and
publication blockers remain blocking. Shortened completed price sessions are
valid inputs; the stricter RVOL session-duration rule remains separate.

The assessment session must itself be a completed calendar session, including
for a lagged observation. Missing close fields in the ten sessions after a lagged
window do not invalidate that historical mean, subject to whole-packet blockers.
Every contributing input must have proven availability by the assessment cutoff;
as with PR #43, this implementation does not backdate availability before retrieval.

## Exact comparisons and representation

The calculation retains an exact rational sum of the supplied finite Decimal
closes. `SMAObservation.exact_sum` stores numerator and denominator as decimal
integer strings, with the period in the specification. This avoids JSON numeric
precision loss. The observation also retains ordered window sessions, contributing
source/row references, availability, specification hash, snapshot hash and input
provenance. Current means include the validated assessment close; lagged means
do not invent a current-close dependency.

The separate mean value is rounded to 50 significant decimal digits using
ROUND_HALF_EVEN for serialization/display. Comparisons never use that rounded
value. `DailyTrendObservation.comparisons()` computes:

- close > SMA50 as 50 * close > exact_sum_50;
- SMA50 > SMA200 as 4 * exact_sum_50 > exact_sum_200;
- SMA50[t] > SMA50[t-10] by comparing their exact 50-close sums.

Equality fails each comparison. A comparison with an unavailable dependency is
`None`, and `confirmed` is `None` if any required comparison is unavailable.
Otherwise it is the conjunction of the three comparisons. Component observations
retain their own unavailable reasons. The composite rejects different snapshots,
identities, provenance, calendars, assessment times and component specifications.
It is an observation of the daily trend, not an eligibility or trading verdict.

Values immediately above/below an exact threshold may display identically at
50 digits while producing different comparisons. Consumers should show the exact
comparison result and sufficient explanation instead of recomputing from display
strings. In tests, a 1e-60 change to the last close of an otherwise constant 100
series distinguishes passing/failing trend comparisons even though all three
displayed averages remain 100.

The Decimal conversion and exact close-subtraction helpers now use an explicitly
configured local context, including traps and exponent limits. This fixes the
PR #43 edge case where an ambient Inexact/Rounded trap caused valid recurring
RVOL division to raise. Successful existing results and serialized contracts
retain their prior meaning; ordinary-context walkthrough output was compared
byte-for-byte with the merged implementation.

## Walkthrough and verification

From this checkout with its locked development environment:

```sh
uv run --locked python -m stock_analyser.sma_example --output /absolute/path/to/sma-example
uv run --locked pytest -q tests/test_sma_observations.py tests/test_indicator_contracts.py tests/test_breakout_observations.py tests/test_calendars.py
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
```

For the full regression gate, set `STOCK_ANALYSER_TEST_DATABASE_URL` to an
explicitly disposable PostgreSQL 18 server, then run:

```sh
uv run --locked python scripts/foundation_release.py --output /absolute/path/to/new-sma-evidence
```

The output directory must be new. This runs the complete suite and verifies its
fresh-process persistence checklist; it does not request live provider checks.

The example uses 200 explicitly synthetic weekday sessions and closes 100 through
299. The current close is 299, SMA50 is 274.5, SMA200 is 199.5, and SMA50 ten sessions
earlier is 264.5. All three comparisons are true. A flat series produces three
false comparisons. Removing the oldest close makes only SMA200 unavailable,
preserving the shorter observations while leaving combined confirmation unknown.
Full structured observations are written beside the printed walkthrough. Two
fresh processes using the same output directory reproduce identical outputs.

Verification on 15 November 2026 used Python 3.12 and the unchanged dependency
lock, with `PYTHONPATH` pointing explicitly to this isolated checkout:

- 145 focused tests passed, covering the new SMA cases, prior observations and
  calendar behavior.
- The complete regression gate passed all 713 tests with zero failures, errors or
  skips, using isolated PostgreSQL 18. Full test collection and the fresh-process
  persistence checklist both passed. The earlier run without a database passed
  626 tests and skipped 87; this full run closes that verification gap.
- Ruff lint/format and diff whitespace checks passed.
- The merged PR #43 walkthrough's stdout and four structured scenario files were
  byte-identical when run against this implementation at the same fixture paths.

The retained summary in `docs/evidence/daily-sma-observations-verification.json`
binds these results to implementation hashes and the dependency lock. The fixture
gate passed; live provider and sector qualification remain uncleared.

Tests cover 49/50, 59/60 and 199/200 history boundaries; exact threshold equality
and near misses; missing, duplicate and invalid inputs; independent field/window
availability; hash tampering; future append and corrected-snapshot replay; mixed
evidence rejection; holiday-aware offsets; and altered ambient Decimal settings.
Synthetic evidence and passing tests do not qualify live data or establish returns.
