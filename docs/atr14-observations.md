# Fixed-anchor ATR14 observations, version 1

This slice implements the accepted ATR calculation and checkpoint contract after
weekly-context PR #45. It exposes diagnostic observations and history readiness;
it does not qualify a breakout, suggest a trade or enable report integration.
The full strategy still needs its separate earnings, risk and sizing policies.

## Calculation and readiness

Use completed, authenticated daily split-adjusted OHLC. For each session after
the reference anchor, true range is the maximum of high minus low, absolute high
minus the immediately preceding expected close, and absolute low minus that
close. A missing previous session never substitutes an older close. Scheduled
weekends/holidays and shortened completed price sessions remain valid.

The seed is the mean of fourteen true ranges: one reference bar plus fourteen
subsequent bars, so the first diagnostic value requires fifteen bars. Each later
state is `(13 * previous canonical ATR + exact true range) / 14`. The seed and
each recurrence use exact rational arithmetic followed by one rounding to fifty
significant digits with ROUND_HALF_EVEN. Explicit Decimal contexts isolate
precision, rounding, traps and exponent settings from the ambient process.

`ATRSpec` versions the formula, numeric contract, implementation, timeframe,
price basis, units and engineering limits. Inputs allow at most 200 coefficient
digits and a Decimal exponent between -200 and 200. Series history is limited to
25,000 expected sessions and a 150-year calendar span. Exceeding these limits
returns unavailable state; no clipped or approximate ATR is substituted.

`strategy_ready` is derived from validated history: at least 245 updates after
the seed, or 260 bars from the anchor. Earlier available values expose
`WARMUP_INCOMPLETE` through `readiness_reasons`. ATR zero is a valid observation
and can be history-ready; a later risk policy must separately require positive
risk distance. The full canonical value is the strategy input, never a rounded
presentation value. Canonical arithmetic need not agree with infinite-precision
recurrence at every possible strict-comparison boundary.

## Fixed identity and required evidence

Call `declare_atr_series` with a policy-series identifier and the first intended
assessment session t0 before examining outcomes. It resolves calendar dates
without reading prices. The anchor is t0 minus 259 expected exchange sessions.
Instrument, market configuration, provider/feed, calendar data version, t0,
anchor and all calculation conventions are retained in the hashed series.
The same initialization is checked whenever a serialized series is used.

For XNYS t0=13 November 2026, the anchor is 3 November 2025. At the next session,
16 November, continuation keeps that anchor instead of shifting to 4 November.
A deliberate different t0 or policy-series identifier produces a separate series.
Historical/paper stage boundaries do not reset this state automatically.

`ATRSpec.history_requirement` requests 15 minimum bars plus 245 warm-up bars,
mandatory daily split-adjusted history. `atr_history_requirement` expands later
requests to the entire fixed-anchor span. The independent raw-price universe
eligibility floor is unchanged. Extra older rows never move the seed; appended
future rows do not enter an earlier observation.

The engine reuses the existing daily SMA observation as a validated price-window
evidence carrier, as weekly context does. Its mean is not used in ATR. Existing
OHLC, calendar, publication, identity, source-integrity and packet-wide checks
apply. Missing volume alone does not block ATR. Missing/duplicate sessions,
unordered payloads and unknown blocking quality errors cannot be repaired by
silently dropping rows. Current snapshot hash-chain failures raise; data and
calendar unavailability return explicit reasons and no usable ATR state.

`ATRProvenance` adds the split-adjustment revision and an audit reference to the
snapshot-bound publication sidecar. These are explicit evidence assertions; this
slice does not independently qualify a provider's adjustment/publication claims.
Availability includes retrieval and publication proof for every consumed input,
so corrections are never represented as available before their evidence exists.

## Checkpoints, authentication and corrections

`ATRCheckpoint` retains the complete observation, including configuration,
canonical state, prior close, anchor, seed session, last processed session,
update count, source/snapshot and row references, publication evidence and an
ordered consumed-prefix digest. Its creation time is separate from input
availability. The digest includes row contents, session closes, publication
assertions and the adjustment revision. Source-file hashes and retrieval times
remain retained separately: those can change when a newer snapshot appends bars.

Migration `0008` adds immutable `atr_checkpoints`, keyed by content identity and
bound by foreign key to the registered snapshot. Indexed series/session columns
support lookup; JSONB preserves complete versioned contracts and decimal strings.
`save_atr_checkpoint` reproduces state from original retained evidence before a
short database write transaction, and then requires an identical registered
snapshot. Identical writes are idempotent. A different checkpoint gets a new
identity; prior records are not overwritten.

`observe_atr_with_checkpoint` loads from this database trust root, checks its
content identity and registered snapshot, then authenticates state through full
original-evidence replay. The lower-level offline `observe_atr` also authenticates
supplied checkpoints through original replay; a self-consistent checksum alone
never suffices. Files and database records remain inside the application's
existing retained-evidence trust boundary, not a cryptographic external attestation.

Continuation requires the identical series, unchanged consumed prefix and the
immediately following expected session. Only then does the next recurrence use
the checkpoint value. Missing/corrupt checkpoints, forged state, changed prior
closes, adjusted-history revisions, duplicate processing and skipped sessions
reject continuation. With complete current evidence, the engine replays from the
same anchor and returns `REPLAYED` plus the retained rejection reason. Without
that history it returns `UNAVAILABLE`; it never rolls or silently reseeds.

**Version 1 requires complete anchor-to-assessment evidence even for continuation.**
It replays the old checkpoint for authentication and validates the full current
prefix. This favors verifiable state over computation savings; compact continuation
without retained original history is not supported. A rolling 260-bar snapshot
cannot keep a series available once the anchor falls outside it.

Corrections produce new evidence and observation/checkpoint identities. They do
not update the original checkpoint or its source files. Replaying the original
snapshot reproduces the original canonical values; report integration using
these observations remains a later delivery slice.

## Worked example and reproduction

The synthetic walkthrough declares t0 and its anchor before calculating:

- Bar 14: unavailable, because only thirteen true ranges exist.
- Bar 15: ATR=2, update count 0, diagnostic only.
- Bar 259: ATR=2, update count 244, warm-up incomplete.
- Bar 260: ATR=2, update count 245, history-ready.
- Bar 261 with prior close 100 and H=104/L=102/C=103: TR=4, ATR is the canonical
  rounding of 15/7, update count 246. Checkpoint continuation equals full replay.
- Correcting an earlier high invalidates the saved prefix and replays from the
  same anchor, retaining the rejection reason and the original evidence.

From a correctly installed locked environment in this implementation checkout:

```sh
uv run --locked python -m stock_analyser.atr_example --output /absolute/path/to/atr-example
uv run --locked pytest -q tests/test_atr_observations.py tests/test_sma_observations.py tests/test_weekly_context.py tests/test_indicator_contracts.py tests/test_breakout_observations.py tests/test_calendars.py
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
```

If sharing another worktree's editable environment, explicitly set `PYTHONPATH`
to this checkout's `src`. Otherwise tests can accidentally import older code.
Set `STOCK_ANALYSER_TEST_DATABASE_URL` to an isolated PostgreSQL 18 instance for
checkpoint persistence/migration tests and the complete release gate:

```sh
uv run --locked python scripts/foundation_release.py --output /absolute/path/to/new-atr-evidence
```

Do not add `--live` for this fixture verification. Verification results and source
hashes are retained in `docs/evidence/atr14-verification.json`. Tests establish
implementation behavior, not live data qualification or strategy performance.
