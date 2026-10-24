# Evidence cache design (US-009)

## Identity and responsibilities

`EvidenceCache` indexes existing immutable `EvidencePacket` files; PostgreSQL
continues to own runs, snapshot associations, and frozen report inputs. No schema
migration is needed. `CollectionService` accepts an optional cache so existing
fixture callers can keep collecting independent snapshots. Live smoke collection
enables it under `<cache_dir>/evidence/`.

The canonical JSON identity contains the complete market configuration and
instrument revision (including market, venue, currency and effective provider
symbol mappings), selected provider and feed, timeframe, adjustment basis,
provider parameters, calendar contract/data version, cutoff policy, source version,
component versions, collection/quality implementation versions and cache format.
Canonical SHA-256 names separate incompatible evidence. Requested counts and
session ranges are matched separately so overlapping windows can share history.

Callers can freeze semantic provider parameters in `run.configuration.cache_parameters`.
Otherwise the entire configuration participates in the identity. Live collection
records Massive settings, yfinance package version and adjustment/action options;
run IDs, smoke groups and cache mode do not affect its evidence identity. Credentials
must never be passed as cache parameters. Implementers must change their component
version when normalization semantics change. The source revision also prevents
automatic reuse across different builds; reopening a saved report remains offline.

## Validity and expiration

- Metadata is stored as immutable observations with an independent 24-hour TTL.
  `metadata(identity, loader)` returns a fresh observation or calls the loader and
  appends a revision. Identities should include source and implementation version.
  The live smoke workflow loads explicit local listing definitions; it does not
  claim to validate provider reference metadata. Changed definitions select a new
  identity immediately. Metadata refresh does not delete price snapshots.
- Price reuse requires the same resolved periods and history requirement, plus
  an observation less than 24 hours old. A newer request time alone does not make
  already completed sessions invalid. Future-dated observations are not fresh.
- Ordinary reuse and incremental merging consider only the newest verified
  overlapping snapshot for the identity. A newer overlapping window supersedes
  an older exact match even while its TTL remains valid, conservatively including
  refreshes without detected corrections. If the newest window does not match,
  reuse misses and refresh uses that window as its merge base. If its session
  cutoff or request time is later than the requested history allows, refresh
  fetches the full requested window instead of reviving older values. Disjoint
  windows do not invalidate each other. Explicit replay may still select the
  older exact snapshot; no saved evidence or report input is rewritten.
- Missing sessions, expiry, or explicit `refresh` fetch missing history plus five
  overlapping periods by default. Weekly overlap consists of whole exchange weeks.
  Earlier requested history expands the contiguous query to include it. Disjoint
  windows get a full request. `CachePolicy` allows callers to set both TTLs and
  overlap independently; zero TTL disables ordinary freshness reuse.
- Explicit `replay` ignores TTL but still verifies identity, session coverage,
  manifest hashes and all retained source hashes. A miss never calls a provider.
  `load_snapshot(SnapshotReference)` inspects an exact historical snapshot offline,
  independent of current implementation identity or freshness policy.
- Invalid, missing, duplicate, mislabelled or incomplete data is never published
  as a reusable cache entry. Live `PUBLICATION_UNVERIFIED` evidence is reusable
  only with its original partial availability preserved. This flag alone does
  not indicate an interrupted write. Errors in a refreshed window are not filled
  using old rows to manufacture complete evidence.

## Refresh and correction lineage

The provider receives a bounded `ResolvedHistory` for missing periods plus overlap.
yfinance explicitly bypasses its in-memory full-window frame for these calls.
The exact response is saved before validation. Valid new rows replace overlapping
old rows in a newly assembled snapshot. OHLCV comparisons use `Decimal`, so even
small corrections to fractional volume are detected without float rounding.

Every successful refresh gets a new snapshot ID, even if values did not change.
Its `cache_refresh` records the parent snapshot and manifest hash, queried interval,
overlap policy and corrected sessions. Its source records retain both generations'
raw bytes, hashes, observation times and query intervals. Composite normalized
bytes have their own hash; these are assembled evidence, not a newly fetched full
provider response. The original snapshot, database references and frozen report
input remain unchanged. Reports show corrected sessions and parent snapshot ID.

Overlap detects corrections only inside the queried window. It does not establish
that older adjusted history remains correct after a split or provider restatement.
Use `off` for a fresh full-window collection when that is required, or configure
overlap to cover the full window. A failed refresh does not silently fall back to
an older snapshot. It saves failed collection evidence, while explicit replay of
the older snapshot remains possible. Corporate-action discontinuity checks still
apply to the composed history.

## Publication and recovery

Raw bytes and manifests use `immutable_write`: write a sibling temporary file,
flush and fsync, then publish by an exclusive hard link. Existing names cannot be
overwritten. Only after the complete snapshot is attached to its run does the cache
publish a hash-addressed commit under `prices/<identity-hash>/<commit-hash>.json`.
Readers discover only these commits and verify their hashes plus referenced files.
Temporary files, orphaned sources/manifests, truncated entries and missing sources
are misses, never complete evidence. A failure after committing a complete snapshot
may leave a reusable snapshot even if the enclosing run later fails in another
timeframe; that run still reports its failure.

Concurrent writers publish independent immutable commits. Readers order them by
provider observation time, commit time, then snapshot ID. No mutable latest pointer
can expose a half-written generation. PostgreSQL continues to serialize writes to
run associations. Two simultaneous misses can make duplicate provider requests;
this cache is not a cross-process request coalescer or provider rate limiter.

No automatic deletion is performed. Cache source files referenced by snapshots
are evidence and must be retained along with snapshots and reports. Clearing the
entire cache directory may break refreshed snapshots' source verification. Index
commits may be discarded without rewriting report inputs; garbage collection of
source files requires a future reference-aware retention workflow.

## Demonstration

`tests/test_cache.py` covers restart replay, identity isolation, independent TTLs,
incremental windows, precise corrections, superseded exact windows, missing-overlap
rejection, corruption and interrupted publication. `tests/test_persistence.py -k cache` uses disposable
PostgreSQL databases to replay with a provider that fails if called, inject a
corrected bar, inspect both snapshots, and regenerate the original report with
identical bytes and artifact hashes. No provider credentials or live requests are
needed. `tests/test_price_routing.py` verifies bounded yfinance refresh requests.
