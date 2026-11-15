# Informational weekly trend context, version 1

This slice implements the accepted weekly research convention after the daily SMA
observations in PR #44. It describes agreement or conflict with a long setup;
it does not veto daily eligibility, rank candidates, add a confidence vote, alter
sizing or stops, or enable trading. No provider, database schema or report pipeline
changes are included. The executable walkthrough demonstrates the new observation
contract before later report integration.

## Definition

Let w be the latest expected completed exchange week at the assessment cutoff.
W[w] is its final regular session's split-adjusted close. M[w] is the arithmetic
mean of the 30 completed weekly closes ending at w. M[w-4] is the same mean ending
four completed weeks earlier. This needs 34 completed weekly closes, without
recurrence warm-up. It is an optional weekly dependency, separate from the existing
260-session daily eligibility floor.

- SUPPORTS: W[w] > M[w] and M[w] > M[w-4].
- OPPOSES: W[w] < M[w] and M[w] < M[w-4].
- MIXED: valid evidence satisfying neither rule, including equality or a flat mean.
- UNAVAILABLE: any missing, invalid, incompatible or unverified required evidence.

The slope is a net change over four weeks, not four consecutive increases.
Comparisons use exact rational window sums and exact Decimal input closes:
30 * W[w] is compared with the current sum; current and lagged sums are compared
directly. Separately displayed means use 50 significant digits and ROUND_HALF_EVEN.
A change smaller than display precision can therefore change classification.
The numeric helper is isolated from ambient Decimal precision, rounding and traps.

## Calendar, publication and constituent contract

`observe_weekly_context` accepts an authenticated daily snapshot, explicit input
provenance, a session calendar, an assessment cutoff and a calculation timestamp.
`WeeklyContextSpec` fixes the 30/4 parameters, split basis, formula, aggregation and
implementation versions, output units and informational role.

The existing calendar resolver groups exchange-local Monday–Sunday weeks. A
completed week ends at its final scheduled regular session, which can be Thursday
when Friday is a holiday. Scheduled holidays and shortened price sessions are valid.
The supplied `CutoffPolicy` records publication grace and timing status. The default
policy retains unverified publication status and produces unavailable context;
synthetic examples explicitly declare synthetic timing. A grace interval alone
never proves that any input was published.

The current incomplete week is excluded. After the next week becomes eligible
under the calendar/grace policy, missing evidence for it produces UNAVAILABLE;
the calculation never substitutes an older week. Each of the 34 expected weeks
requires all scheduled daily constituents on a consistent split-adjustment basis,
even though only its final close contributes numerically to the weekly average.
Missing volume alone does not invalidate a price week. The daily RVOL shortened-
session restriction is not applied to weekly price context.

The implementation reuses the daily SMA observation's authenticated window checks
to validate the full constituent span once. Its daily mean is retained only as
part of that evidence object and never enters a weekly formula. Weekly closes are
read from the authenticated source rows identified by the validated contributors.
This preserves existing OHLC, identity, publication, ordering and packet-wide
quality checks without a second interpretation of their rules. Integrity failures
raise; data or calendar unavailability is represented explicitly.

Every constituent must be proven available by the assessment cutoff. Week
availability is the maximum of calendar/grace eligibility and the first proven
time of all its constituents. Overall availability is the maximum across all 34
weeks, including the lagged window. As with the prior observations, retrieval is
part of this proof; the implementation does not backdate historical availability.

The result retains the snapshot and provenance, both specification/provenance
hashes, calendar resolution and policy, daily assessment session, weekly start/end
dates, constituent session lists, closes, availability times, row/source lineage,
exact sums, rounded means, classification and unavailable reasons. The daily and
weekly dates can differ during the week. Daily observation functions and their
qualification behavior remain independent. Unavailable context exposes no weekly
aggregate values and is never relabeled mixed.

## Worked examples and reproduction

The example uses 34 explicitly synthetic weekday weeks:

- Weekly closes 100 through 133: close 133, M[w]=118.5, M[w-4]=114.5; SUPPORTS.
- Weekly closes 133 through 100: close 100, M[w]=114.5, M[w-4]=118.5; OPPOSES.
- All closes 100: both means 100; MIXED.
- Closes 100 through 132, then 110: close below the current mean but a positive
  four-week change; MIXED.
- A missing midweek constituent: UNAVAILABLE, even when the final close exists.

Using the locked development environment from this checkout:

```sh
uv run --locked python -m stock_analyser.weekly_example --output /absolute/path/to/weekly-example
uv run --locked pytest -q tests/test_weekly_context.py tests/test_sma_observations.py tests/test_indicator_contracts.py tests/test_breakout_observations.py tests/test_calendars.py
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
```

The walkthrough prints separate daily/weekly dates and writes full structured
observations. Two fresh processes at the same fixture paths reproduce identical
output. Replay with a later calculation timestamp preserves semantic bytes.

For the complete regression gate, explicitly set `STOCK_ANALYSER_TEST_DATABASE_URL`
to a disposable PostgreSQL 18 server and run:

```sh
uv run --locked python scripts/foundation_release.py --output /absolute/path/to/new-weekly-evidence
```

This checks complete test collection and the fresh-process persistence checklist.
It does not opt into live provider qualification.

Verification on 15 November 2026, using the unchanged lock and Python 3.12:

- 180 focused tests passed, including 35 weekly-context cases.
- The complete PostgreSQL 18 gate passed all 748 tests with zero failures, errors
  or skips; complete collection and fresh-process persistence checks passed.
- Ruff lint/format, whitespace checks and direct code review passed.
- The prior breakout and daily-SMA walkthroughs retained byte-identical stdout
  and structured observations at identical fixture paths.

The retained record in `docs/evidence/weekly-context-verification.json` identifies
tested source hashes, the dependency lock and final gate evidence. An earlier gate
run was interrupted by a tooling restart and is not counted as a successful run.
Live provider and sector qualification remain uncleared.

These are implementation and reproducibility checks. They do not establish the
predictive value of the 30-week mean, four-week slope or any trading strategy.
