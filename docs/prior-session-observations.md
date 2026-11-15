# Prior-session observations, version 1

Issue #41 adds five offline descriptive features: `prior_high`, `prior_mean_volume`,
`relative_volume`, `close_distance`, and `close_above_prior_high`. It does not
activate a strategy. Phase 1 schemas, report stages, ledger and live qualification
blockers are unchanged. N=5 in the walkthrough and N=20 in tests are illustrative
parameters, with no production default or volume threshold.

## Calculation contract

`ObservationSpec` requires explicit positive integer price and volume lookbacks,
plus the regular session duration in seconds. Booleans and coerced strings are
rejected as lookbacks. Formula `prior-session-1` and implementation
`decimal-fraction-1` identify these conventions:

- Prior high: maximum high of the preceding N_price expected exchange sessions.
- Prior mean volume: sum of the preceding N_volume volumes / N_volume.
- RVOL: current completed-session volume / that prior mean.
- Close distance: current close minus prior high, in quote currency.
- Above prior high: strict current close > prior high, with zero tolerance.

Each lookback is resolved independently with `HistoryRequirement(N+1)` and the
existing `HistoryResolver`. The assessment session is explicit: an incomplete or
non-session assessment never silently moves to yesterday. Expected sessions come
from the supplied versioned calendar, including holidays. Missing sessions never
get replaced with older bars. The baseline features themselves only depend on
prior bars; the distance, comparison and RVOL also depend on the current bar.
All assessments require the assessment session to have closed.

Prices use Decimal and comparisons are exact. Volume sums and ratios use exact
rational arithmetic, including fractional shares; division is rounded only at
output to 50 significant decimal digits with ROUND_HALF_EVEN, independent of the
process decimal context. RVOL uses the exact mean rather than a previously rounded
mean. Close subtraction chooses sufficient precision for an exact decimal result.
JSON writes decimal strings and actual boolean comparisons. Non-finite inputs are
invalid. A positive baseline with zero current volume gives zero. A zero mean
itself is available; RVOL with that denominator is unavailable.

## Trust, field-local availability and time

`observe` first calls the existing snapshot loader to verify the manifest, metadata
and every retained source hash, including cache ancestry. Integrity failures raise
an exception before any values can be returned. Raw rows are reread as verified
bytes. This is necessary because Phase 1 `PriceBar` requires volume, and legacy
normalization drops valid OHLC along with a row's missing volume.

Only explicitly revalidated row-quality findings can be localized. Price features
validate all required OHLC; volume features validate required volume. Both validate
row timestamps, optional row identity labels, calendar scope, provider/feed and
split-adjusted price basis. Invalid/missing volume does not invalidate otherwise
valid OHLC, and invalid OHLC does not invalidate independently valid volume.
Missing and duplicate required sessions block the affected window. Unknown quality
errors, incompatible packet identity/feed/basis and publication blockers apply to
the whole packet. Session-specific findings after the assessment are outside its
input prefix; their rows cannot affect earlier calculations.

`InputProvenance` is a separate explicit, versioned assertion, bound to the exact
snapshot hash, hashed and embedded in the observation. It records a verified consistent split-volume convention and
its audit reference, plus per-session publication evidence. Old packets lacking
these assertions are not automatically qualified. A source's claimed publication
time is retained separately from a verified time. Verification requires an audit
reference; a caller is responsible for establishing that proof. Synthetic examples
label their references `synthetic://`; these are never live qualifications.

An existing `PUBLICATION_UNVERIFIED` packet, metadata or resolution blocker remains
blocking, even with a supplemental assertion. With no verified publication, the
historical availability is `unknown`; no usable numeric observation is emitted.
The conservative first proven knowledge time is source retrieval, bounded below by
exchange close and any later verified publication. Even verified historical
publication does not backdate this implementation's availability before retrieval.
An as-of before that time is unavailable. The observation records close, retrieval,
publication, first proven time, evaluation as-of and calculation-run time separately.

RVOL uses `full-regular-sessions-only-1`: a shortened current or baseline session
makes it unavailable; valid prices survive. There is no duration normalization or
substitution. Initial live support is US regular equity sessions (23,400 seconds).
Synthetic fixtures explicitly declare their artificial regular duration (21,600
seconds). A different live duration policy requires a new reviewed version.

## Reproduction and walkthrough

Run with the locked development environment, without credentials or a database:

```sh
uv sync --locked
uv run --locked python -m stock_analyser.indicator_example --output /absolute/path/to/example
uv run --locked pytest -q tests/test_indicator_contracts.py tests/test_breakout_observations.py
```

The example prints formulas, six contributing rows and their session labels,
statuses, reasons and availability times, and writes full structured observations.
The five prior highs 98, 100, 99, 101, 100 produce **101**. Prior volumes 800000,
1000000, 1200000, 900000, 1100000 produce **1000000**. Current close 102 and volume
1600000 produce **distance 1, above true, RVOL 1.6**. Current high 104 and volume
1600000 never enter the baselines. The equal and below cases produce false; the
missing-volume case retains the price observations with unavailable RVOL.

Repeat the command with the same absolute output directory in fresh processes:
outputs are deterministic. Snapshot files are immutable and content addressed.
Corrections create new snapshot identities and hashes. Old snapshot replay retains
its old values. `Observation.semantic_bytes()` excludes only `calculated_at`;
specification, provenance and their hashes, source/row references, calendar version,
input availability, as-of, snapshot identity and all results remain serialized.
Future bars cannot alter prior values/statuses at the same as-of input prefix;
a genuinely changed snapshot carries different lineage, even if its result agrees.

Run the full regression gate with an explicitly exported disposable PostgreSQL URL:

```sh
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
export STOCK_ANALYSER_TEST_DATABASE_URL='postgresql+psycopg://...disposable...'
uv run --locked python scripts/foundation_release.py --output /absolute/path/to/new-evidence
```

Owner walkthrough is required before closing the issue. Neither synthetic fixture
success nor these descriptive values clear #38/#18, establish multi-touch resistance
or consolidation, or demonstrate profitability. Strategy decisions remain separate.
