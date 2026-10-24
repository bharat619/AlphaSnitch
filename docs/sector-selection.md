# Reproducible Technology selection (US-012)

The selection workflow freezes the enumerated population, enrichment, policy,
calendar windows, history references, exclusions, ranking observations and ordered
selection. Replaying that snapshot verifies the retained price evidence and
reproduces the order without provider requests or calendar recalculation.

**All current selections are labeled PROTOTYPE.** The live qualification findings
from [US-011](technology-universe-findings.md) still block full-sector acceptance.
There is no override that promotes a supplied population or curated list into a
qualified sector. Buy remains unavailable. This implements deterministic selection
machinery; it does not establish live coverage or investment performance.

## Demonstration

With the existing local PostgreSQL configuration:

```sh
uv run --locked stock-analyser init
uv run --locked stock-analyser universe demo
# Use the snapshot ID printed above, including after restarting the CLI:
uv run --locked stock-analyser universe replay <snapshot-id>
uv run --locked stock-analyser universe replay <snapshot-id> --json
uv run --locked stock-analyser universe demo --shortfall
```

The first fixture contains twelve eligible synthetic securities with tied traded
values, an alternate listing, an ETF, an ADR, an OTC listing, an unknown sector,
a low-price listing, an illiquid listing and incomplete history. Ten distinct
securities are selected, with ties resolved by ascending canonical instrument UUID.
The shortfall fixture has seven eligible securities and reports a shortfall of
three. Enumeration completeness and unresolved classifications are separate fields;
ten selections never imply complete coverage. Fixture sessions are explicitly
synthetic and are not a live exchange calendar claim. No provider credentials or
requests are needed. Exit zero means that the prototype operation succeeded,
including an explicit shortfall, not that full-sector coverage is accepted.

## Input boundary and enrichment

`stock_analyser.universe.ReferenceUniverseAdapter` consumes the paginated
`Enumeration` returned by the US-011 reference enumerator, a `SourceRecord`, an
identity resolver and a classification loader. It retains every enumerated row,
including unsupported listings and failed enrichment. It enriches every
reference-eligible row rather than sampling only discovery matches or the final
ten. The adapter preserves COMPLETE/TRUNCATED/FAILED status, reason, page count,
the population source/hash and scope. COMPLETE describes only the supplied query.

The identity resolver must return an existing, verified `Instrument`, binding the
reference share-class/listing FIGIs to immutable UUIDs and effective provider
symbols; missing or failed resolution is excluded. The adapter does not infer
cross-provider identity from ticker equality or invent new IDs during a rename.
Both callbacks have typed protocols (`IdentityResolver` and `ClassificationLoader`).
The classification loader returns the original sector, industry, source taxonomy,
retrieval time, optional effective date, source/hash and OBSERVED/UNKNOWN/
CONFLICTING/FAILED state. Each candidate retains sanitized enrichment failures by
stage (identity/classification): `UNRESOLVED` for a missing result,
`PROVIDER_UNAVAILABLE` for an `OSError` or explicit `EnrichmentProviderError`, and
`INVALID_RESPONSE` for schema validation failures. No exception text or provider
payload is retained. SDK-specific availability failures should be wrapped in
`EnrichmentProviderError`; unexpected callback errors propagate so programming
errors are not silently treated as missing classification. Callers may wrap the
loader in `EvidenceCache.metadata` to reuse current enrichment; preserve the
original source retrieval time on cache hits.

Mapping `yahoo-sector-map-1` recognizes exact `Technology` and the other known
Yahoo sectors. Unknown labels, missing profiles and conflicting share-class
classifications cannot enter the selection. Effective dates are compared with the
market-local date of the requested instant, independently of its supplied timezone;
future classifications are excluded. Retrieval-time classifications without an effective date do not establish
point-in-time membership for historical backtests. The input preserves them as
such. Excluded security types do not generate spurious unresolved-sector counts.

`SelectionRequest` is the versioned JSON input boundary. It contains the full
market configuration, as-of time, cutoff policy, `UniversePopulation`,
`SelectionPolicy` and existing `SnapshotReference` objects. All retained data must
be permitted for local use; this change does not resolve the live retention gap.
US-011 aggregate hashes cannot be converted back into a reproducible population.

For permitted inputs backed by the maintained US calendars:

```sh
uv run --locked stock-analyser universe select --input /path/to/selection-request.json
```

This command is offline: it consumes supplied metadata and existing evidence
manifests. It does not silently fetch missing profiles or histories. The Python
`select_universe(request, resolver)` API also accepts an explicit fixture resolver.
Input shapes can be inspected through `SelectionRequest.model_json_schema()` and
the complete request retained by `universe replay --json`.

## Eligibility, history and ranking

Selection version `us-technology-selection-1` implements the US-011 proposal:

- Require active US, USD, common shares (`CS`) on the proposed supported venues.
  Exclude ETFs, funds, ADRs, preferreds, warrants, units, OTC and unknown types.
  Missing FIGIs, conflicting listing identities or inconsistent FIGI-to-UUID
  mappings exclude the affected securities. A configured, supported calendar
  is also required: the current maintained calendar adapter does not support
  every venue in the proposal.
- Deduplicate by share-class FIGI before classification/history/price ranking.
  Choose the proposal's venue order, then composite FIGI and ticker ascending.
  An alternate listing cannot replace the preferred listing simply because it
  has better classification, liquidity or history. Different share classes stay
  distinct. Conflicting classifications on the same share class block inclusion.
- Require raw daily history for at least 260 completed sessions, extended to
  the largest mandatory daily minimum plus warmup. Independently require every
  configured mandatory daily/weekly history with its own adjustment basis and
  warmup; weekly history cannot be inferred from a daily row count. Missing or
  invalid evidence excludes that candidate before ranking.
- Verify cached manifest/source hashes, instrument/configuration, adjustment,
  feed, calendar/data version, cutoff policy and expected completed periods.
  Only AVAILABLE evidence with no error findings is eligible. Unverified live
  publication timing remains blocking. Gaps, stale dates, insufficient warmup,
  split-adjusted liquidity inputs and a wrong instrument are rejected. No older
  bars substitute for missing required sessions.
- Use the final **exact 60 expected daily sessions** at the common completed
  cutoff. Multiply each raw regular-session close by raw share volume in USD.
  A valid zero-volume session contributes zero. Require latest raw close **at
  least $5** and mean traded value **at least $10 million**, inclusive, by
  default. Both values are configurable nonnegative decimals in the request's
  policy and remain evaluation hypotheses. Compatibility sets include only
  candidates passing every eligibility, history, price and liquidity filter.
  Different feeds or 60-session windows among those candidates prevent a combined
  ranking; excluded candidates retain their own reasons and cannot block it.
- Rank by exact sum of the 60 traded values descending, then canonical instrument
  UUID ascending. Because every divisor is 60, this is exactly mean-value order.
  Products and rational sums/comparisons avoid ambient Decimal precision and
  premature rounding; the saved mean uses at least 80 digits of precision.
  Select at most ten distinct eligible securities and save all candidates'
  reasons and ranking inputs. A shortfall is `10 - selected count` and is
  independent of incomplete enumeration and unknown classifications.

The frozen policy values, source/enrichment timestamps, original metadata,
instrument identities and calendar resolutions are part of the input hash.
Candidate and history-reference order is normalized before hashing. Changing a
threshold or history generation creates a new snapshot identity. Semantic changes
to this implementation must bump its version; old snapshots must keep their
existing evaluation semantics. New snapshots use `sector-selection-2`, which fixes
eligible-only compatibility and market-local classification dates. Existing
`sector-selection-1` snapshots replay with their original rules (including these
two historical bugs), retaining their input hashes and results. Reselect their
frozen request to obtain a corrected v2 snapshot with a new identity; replay
never silently replaces saved evidence. The v1 compatibility path is for historical
replay, not new selections.

Eligibility, taxonomy and listing order live in the shared immutable
`TECHNOLOGY_RULES_V1` in `stock_analyser.universe_policy`. Its tuple-valued rules
are independent of the qualification spike's exported proposal dictionary.
Changing that dictionary cannot change selection or replay. New rule semantics
require a new version, not mutation of v1.

## Persistence and history reuse

Migration `0005` adds immutable PostgreSQL `universe_snapshots` records. Each JSON
payload includes the complete request, resolved history windows, selected
instruments, complete ordered ranking, each listing's exclusion reasons,
unresolved classifications, all 60 close/volume/value observations for candidates
with usable raw history, and the exact history snapshot IDs/references/hashes.
The UUID is derived from the frozen input hash. Repeated writes are idempotent;
conflicting payloads under the same UUID are rejected.

`save_universe` first commits the database record, then atomically publishes a
read-only `<snapshots-dir>/universes/<snapshot-id>/snapshot.json`. Provider and
file verification happen outside the write transaction. A file-publication
failure leaves the DB snapshot available for replay. Calling `save_universe`
again with `repo.get_universe(snapshot_id)` retries publication; no prices are
recollected and no selection is recalculated from newer inputs.

`FrozenUniverseAdapter` implements the existing `UniverseProvider.instruments`
boundary for an explicit prototype at the exact frozen market/as-of time. Its
`histories(instrument_id)` returns the same evidence references used for selection.
Downstream collection can reuse these snapshots rather than refetch the ranking
history. The demo publishes through `EvidenceCache.commit` and obtains exact
cache replay hits before ranking, exercising the US-009 path. Raw liquidity
evidence does not replace mandatory split-adjusted technical history; configure
both requirements when both are needed.

Replay reopens and verifies evidence, recomputes decisions using the frozen
calendar resolutions and compares the entire saved result. It fails on a changed
order, missing/corrupted usable price evidence or changed decisions. Keep the
referenced immutable cache/source files with the database backup; the database
retains references, not duplicate price payloads. Replaying never refreshes the
cache, loads credentials or silently adopts a corrected price generation.

## Validation

```sh
uv run --locked pytest -q tests/test_universe.py
# Uses disposable databases with the repository's test database setting:
uv run --locked pytest -q tests/test_persistence.py -k 'universe or migration_idempotency'
```

Tests cover ten selections and shortfall, input reordering, exact ties, threshold
boundaries, zero-volume contribution, mandatory weekly warmup, stale/missing
histories, unknown/conflicting classification, duplicate security identity,
retained enrichment failures, tampered evidence, unchanged cached snapshot IDs,
immutable SQL writes, publication interruption, CLI demonstration and replay in a
separate process.
