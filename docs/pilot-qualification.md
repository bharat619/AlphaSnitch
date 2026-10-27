# Pilot qualification (US-019)

Pilot acceptance remains **FAIL**. No account-specific confirmation is available
for Massive or Yahoo (operator confirmation, 2026-10-27). Rights are **UNKNOWN**;
affected live collection is blocked. No subscription purchase or upgrade is
permitted by this workflow. The October 24 observations remain unchanged.

## Evidence and acceptance

`qualification.py` defines schema `2` and criteria
`us-technology-qualification-1`. Every mandatory gate is recomputed using an
injected clock. The criteria freeze the existing US Technology policy, selection
policy, exact 60 completed-session raw close × raw share-volume arithmetic,
USD 5 price and USD 10 million mean traded-value boundaries, ten selections, and
at least 260 completed daily bars. Independent mandatory history requirements
remain part of `SelectionPolicy.requirements()` and its hash. The initial
qualification criteria use the default selection policy (no additional weekly
feature is configured). A different mandatory history configuration requires
explicitly supported criteria; supplying a different policy cannot silently pass.

Reports partition reference listings into explicit measured exclusions and
potentially supported listings. Missing identities or metadata stay visible.
Classification covers every potentially supported reference listing, including
Technology securities absent from discovery. Stable share-class and listing
identifiers require matching venue, currency, type and effective interval.
Punctuation and rename fallbacks require an explicit dated corroborated mapping;
symbol equality and names do not establish identity. Distinct share classes stay
separate. Duplicate listings and sector/identity conflicts remain accounted for.

Local population execution reopens selection input files through the existing
`replay_universe` evaluator, including independently resolved daily/weekly windows,
feed/identity/adjustment checks and exact liquidity arithmetic. Price/liquidity
exclusions are separate from invalid or missing mandatory histories. SPY benchmark
history is verified independently when supplied. Missing benchmark evidence blocks
acceptance. Complete selection alone cannot prove benchmark or cold/warm workload
coverage.

The report separately records source/implementation/lock hashes, criteria/policy/
history hashes, provenance, query scope, page hashes/times/counts, denominators,
SDK versus HTTP counts, ceilings, scheduled cooldown totals, measured versus
estimated work, rights and derived gate reasons. Implementation and lock hashes
must match supported code when a report is consumed. Metadata expires after 24
hours; rights evidence expires after 30 days or its earlier explicit expiry.
Future, stale, unsupported, inconsistent or conflicting evidence fails closed.
Historical schema 1 observations remain readable JSON but cannot qualify schema 2.
Synthetic evidence can pass the evaluator in fixture mode; it cannot pass a live
release. A stored summary PASS is never sufficient.

Only aggregate counts, fixed source/query names, stable reason codes, timestamps
and allowlisted hashes enter `findings.json`. Raw rows, histories, mappings,
credentials, provider request URLs, upstream errors and cursors do not. Local
export inputs stay outside committed evidence and need retention rights. Hashes
cannot reconstruct the discarded population: aggregate reports do **not** provide
full detailed replay without those separately retained, rights-permitted files.
An aggregate report is an observation, not a signed attestation against a malicious
author fabricating all its underlying measurements.

## Offline planning and reviewed stages

Use a **new** directory for every observation:

```sh
uv run --locked stock-analyser qualify-universe \
  --mode plan --output /private/tmp/us019-plan-001 --max-pages 2
shasum -a 256 /private/tmp/us019-plan-001/workload.json
```

This command loads no credentials and makes no provider calls. It writes schema 2
`findings.json` and a proposed `workload.json`, and exits 1 while blocked. The
initial reviewed inventory bounds are two pages per source, ten total HTTP
attempts (six Massive, four Yahoo), one worker and 180 seconds. These are caps,
not estimates of the full population. The proposed manifest carries UNKNOWN
rights and cannot authorize collection. `--profile-limit` is retained only to
reject nonzero historical sample controls.

After independently resolving rights and reviewing the exact manifest bytes, the
explicit inventory contract is:

```sh
uv run --locked stock-analyser qualify-universe --mode inventory \
  --manifest /absolute/path/to/reviewed-workload.json \
  --manifest-sha256 EXACT_FILE_SHA256 --output /private/tmp/us019-inventory-001
```

Do **not** run this with current UNKNOWN rights expecting collection. Preflight
writes a zero-dispatch failure observation instead. With sufficient rights,
Massive enumeration uses the existing shared persistent allowance and credentials;
all workers/retries share one request/deadline budget. Timeout and pacing waits
are clamped to remaining time. Provider cooldown is preserved even when it cannot
fit the deadline. Pagination rejects duplicate/malformed rows, page count mismatch,
empty nonterminal pages, unsafe/repeated cursors and provider failures. Massive's
`count` is a per-request result count, not a population total. Completeness uses
its terminal page. Yahoo discovery uses a stable reported total, start and count.
Retrieval windows do not prove transactional snapshot consistency.

The installed Yahoo SDK's authentication/internal retries do not provide a
verified HTTP amplification ceiling. The live Yahoo adapter therefore does not
dispatch broad collection; the report records HTTP counts as unknown. A deadline
is not an HTTP cap. No background worker is spawned. Handled enumeration errors,
budget stops and KeyboardInterrupt retain sanitized partial evidence. SIGKILL,
process crash, disk/setup failure or interruption during final publication may
leave no final report; use a new directory. Final JSON is flushed and atomically
linked without replacing an existing file. Existing inventory output directories
are refused before network dispatch.

A complete permitted export is the supported fallback for population-dependent
classification/mapping/history evaluation. `population_manifest()` refuses
incomplete inventories as an upper bound. A reviewed population manifest binds
canonical schema-2 inventory hashes, reference count, an independently established
HTTP upper bound, per-source/overall ceilings and the exact local input file hash.
No multiplication of sampled counts is treated as an upper bound.

```sh
uv run --locked stock-analyser qualify-universe --mode execute \
  --manifest /absolute/path/to/reviewed-population-workload.json \
  --manifest-sha256 EXACT_FILE_SHA256 \
  --population-inputs /absolute/path/to/rights-permitted-local-export.json \
  --output /private/tmp/us019-population-001
```

The local export has exactly these fields:

- `origin`: `live` or `synthetic`; synthetic publication/calendar evidence cannot
  be relabeled live.
- `started_at`, `finished_at`: acquisition timestamps, distinct from evaluation time.
- `snapshot`: existing `UniverseSnapshot` including retained local history references.
- `inventories`: two `InventoryEvidence` objects; reference page hashes must cover
  the exact snapshot reference population in original provider order.
- `mappings`: per-reference-ticker stable IDs or dated corroborated mappings with
  venue/currency/type, provider symbol and evidence reference hash.
- `profiles`: per-reference-ticker symbol, sector, retrieval timestamp and optional
  conflict indication. They must agree with selection's classification inputs.
- `budget`: `BudgetEvidence` for actual acquisition, including cold/warm work,
  actual HTTP attempts, SDK calls, retries, scheduled cooldown, elapsed duration
  and conservative upper bound. Unknown amplification cannot pass.
- `benchmark`: separate `UniverseSnapshot` containing SPY's independently resolved
  history, or `null` (which keeps history qualification blocked).

No live population acquisition is implemented through an unbounded SDK fallback.
The source/export must provide actual workload evidence; local evaluation does not
pretend its own execution time measures collection. See the tested local export
construction in `tests/test_qualification.py` for the exact contracts. Rights are
checked before reading local detailed inputs. Absent rights or incomplete budgets
produce a blocked observation. Invalid input hashes/schema are sanitized setup
errors and do not cause provider calls.

## Source feasibility and permissions needed

Reviewed official sources on 2026-10-27:

- [Massive All Tickers](https://massive.com/docs/rest/stocks/tickers/all-tickers)
  documents pagination, identifiers and per-request count. Query completion does
  not establish Yahoo classification or account rights.
- [Massive Market Data Terms](https://massive.com/legal/market-data-terms-of-service),
  last updated August 28, 2025, constrain personal/non-business use and describe
  additional dataset entitlements and display-only restrictions unless superseded.
  These terms do not establish permission for this account's retained derivation
  workflow. Account/product entitlement and any superseding agreement are unknown.
- [yfinance official documentation](https://ranaroussi.github.io/yfinance/)
  directs users to Yahoo's data terms and identifies personal-use scope.
  The software license is not a data-retention grant.
- [Yahoo terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html) could not
  be retrieved by the documentation reader during this review. Applicable local
  account terms, automated-access permissions and retention rights remain unknown.

To unblock, obtain official applicable terms or written provider confirmation
identifying the account/product, datasets and permitted operations, specifically:

1. Massive active US reference inventory collection and retention of listing and
   share-class IDs/metadata for reproducible internal universe snapshots.
2. Yahoo automated classification/profile acquisition across the entire supported
   population, verified cross-provider mappings, taxonomy/as-of evidence, and
   retention of those inputs. Alternatively, obtain a complete classification
   export with stable IDs, taxonomy, as-of and explicit permitted retention/use.
3. Massive unadjusted historical price/share-volume collection and retained daily
   and mandatory weekly snapshots, benchmark work, liquidity derivation and
   intended internal use. Confirm feed/venue coverage and publication support.
4. For each operation: source/version/date, review date and expiry, applicable
   account/product scope, permitted retention duration (the current intended
   local replay period is 365 days), use restrictions and no unresolved conditions.
   Record document bytes' SHA-256 or a written-confirmation reference. A boolean
   approval, working API key or subscription tier alone is insufficient.

Even resolved rights would leave Yahoo HTTP transport bounds, complete inventory/
classification/identity, calendar/feed support, all candidate/benchmark histories,
and measured cold/warm work to verify. No source purchase is implied.

## Verification and current outcome

```sh
uv sync --locked
uv run --locked pytest -q tests/test_qualification.py tests/test_universe_spike.py \
  tests/test_universe.py tests/test_release_checklist.py tests/test_calendars.py \
  tests/test_massive.py tests/test_price_routing.py
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
# Disposable server only; do not use the application database.
STOCK_ANALYSER_TEST_DATABASE_URL=postgresql+psycopg://stock_analyser:local-test-password@127.0.0.1:55437/stock_analyser \
  uv run --locked python scripts/foundation_release.py \
  --output /private/tmp/alphasnitch-us019-release-001 \
  --qualification-evidence docs/evidence/pilot-qualification-preflight-2026-10-27.json
```

Run-wide `ruff check .` additionally includes unrelated untracked `evaluation/`
files in this workspace; those existing files have lint failures and are excluded
from the implementation's tracked-code check. No live collection belongs to the
unit suite or fixture release checklist.

The final full offline checklist completed with **534 passed, zero failures/errors/
skips**, all 534 expected test IDs reconciled, and the mandatory fresh-process
checklist PASS. The actual final command used the URL above and output directory
`/private/tmp/alphasnitch-us019-release-003`. The first sandboxed attempt could not
connect to the local test server (85 setup errors); the authorized rerun with local
network access passed. The application database was not used. `uv sync --locked`
succeeded. Scoped Ruff checks passed; the 57 unrelated untracked-file lint issues
remain outside this change.

Retained observations:

- [Preflight](evidence/pilot-qualification-preflight-2026-10-27.json), SHA-256
  `d947a0131e27e518f1230284f5e3f9b0fb1425b7350bbfbf6bf57637aadad0ab`.
  Massive dispatched requests: 0; Yahoo SDK calls: 0; Yahoo HTTP amplification
  qualification: unknown; history requests: none. There was no live collection.
  Proposed caps were 10 overall requests, 6 Massive/4 Yahoo, two pages per source,
  180 seconds and one worker. Cold/warm measurements were not performed.
- [Full release result](evidence/foundation-qualification-release-2026-10-27.json)
  and [fresh-process reopen result](evidence/foundation-qualification-reopen-2026-10-27.json).
  These were produced from the working feature branch; the source was marked dirty
  and qualification records its exact implementation hashes. This is not a new
  clean-checkout installation claim.

At observation time, **contract and freshness PASS**. **Inventory, classification,
identity, history/liquidity, rights, workload and live origin FAIL**. Rights remain
UNKNOWN for collection, retention and internal derivation across both providers'
applicable datasets. The concrete blockers are the missing permissions listed
above, unbounded Yahoo HTTP amplification, incomplete population inventories,
classification/mappings, candidate and benchmark histories, venue/feed validation,
and unmeasured cold/warm work. The release smoke was NOT_RUN and the milestone
remains incomplete. This verifies the implemented blocked workflow; it does not
qualify the pilot. Freshness and compatibility are recomputed whenever evidence
is consumed, so these stored results are not perpetual acceptance.
