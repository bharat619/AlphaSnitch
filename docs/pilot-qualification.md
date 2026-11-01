# Pilot qualification (US-019)

Current criteria: `us-technology-qualification-3`; aggregate report schema: `2`.
The workflow implements bounded native inventories, complete-reference Yahoo
profile acquisition, automatically calculated workload plans, and qualification
of retained selection/benchmark evidence. **Pilot acceptance remains FAIL**.
The operator confirmed no account-specific permissions for Massive or Yahoo.
All nine required rights dispositions remain UNKNOWN, so affected live operations
are blocked. No purchase, subscription change or live collection was performed.

## Stages and reproduction

Use a new output directory for every command. Existing directories are refused
before provider activity. Planning is offline: no credentials, provider clients
or raw vendor data acquisition are needed.

```sh
uv run --locked stock-analyser qualify-universe --mode plan \
  --max-pages 2 --output /private/tmp/us019-inventory-plan
```

This writes `findings.json` and a proposed `workload.json`. The default reviewed
inventory caps remain two pages per source, ten total HTTP attempts (six Massive,
four Yahoo), 180 seconds and one worker. UNKNOWN rights produce a zero-dispatch
FAIL. These caps are not a population estimate. A proposed manifest is not a
rights grant. Nonzero historical `--profile-limit` samples are rejected.

After rights and the exact manifest have been independently reviewed, the
inventory contract is:

```sh
uv run --locked stock-analyser qualify-universe --mode inventory \
  --manifest /absolute/path/to/reviewed-inventory-workload.json \
  --manifest-sha256 EXACT_FILE_SHA256 --output /private/tmp/us019-inventory
```

With current UNKNOWN rights it writes blocked evidence and makes no provider
calls. With sufficient rights it enumerates Massive active US stocks and Yahoo
US Technology discovery using the existing checked enumerators. Complete queries
produce `population-workload.json` automatically. Truncation, inconsistent totals,
duplicates, malformed pages, errors or an exhausted budget preserve partial
findings and cannot produce a complete-population plan.

Rights-permitted details are stored under `private/`, with a private directory
and immutable files. They include projected reference rows, private inventory
state and actual HTTP counters. Only the allowlisted aggregate `findings.json`
and aggregate workload plans belong in committed observations. Never commit
`private/`, mappings, cookies, tokens, price histories or local population exports.

Review the calculated population plan and bind the exact dated mappings:

```sh
uv run --locked stock-analyser qualify-universe --mode plan \
  --manifest /private/tmp/us019-inventory/population-workload.json \
  --identity-mappings /absolute/path/to/permitted-mappings.json \
  --output /private/tmp/us019-population-plan
```

Alternatively, `--inventory-evidence /path/to/findings.json` calculates a new plan
from fresh, complete, compatible aggregate inventories without reading raw rows.
`--inventory-inputs` can bind the exact private inventory file hash. No external
population upper-bound number needs to be invented or supplied.

The bounded enrichment contract is:

```sh
uv run --locked stock-analyser qualify-universe --mode enrich \
  --manifest /private/tmp/us019-population-plan/workload.json \
  --manifest-sha256 EXACT_FILE_SHA256 \
  --inventory-inputs /private/tmp/us019-inventory/private/inventory.json \
  --identity-mappings /absolute/path/to/permitted-mappings.json \
  --output /private/tmp/us019-enrichment
```

Every potentially supported reference listing is visited, including listings
absent from discovery. Explicit unsupported types/venues/currencies are measured
exclusions; unknown metadata/identities stay in the denominator. Missing mappings
allow only a diagnostic exact-symbol lookup: the profile cannot establish verified
identity or Technology membership. Symbol equality, punctuation changes or company
names never manufacture a mapping. Returned symbols are preserved and checked.
Actual exchange/currency/security-type contradictions remain conflicts; missing
metadata remains unknown. One provider symbol cannot identify two distinct share
classes/listings at the same effective date. Mapping dates use the US market day.

Enrichment emits sanitized findings, retained private profiles/source references,
and `enrichment-workload.json`. Once every required classification and identity is
resolved, it automatically recalculates history costs using the measured distinct
Technology population. Unknown/conflicting/failed/unattempted members prevent this
refinement. Reviewed ceilings are never raised automatically. Metadata enrichment
may run within its separately calculated prefix budget; history remains prohibited
until its full refined plan fits the reviewed limits.

### Candidate and benchmark history measurement

The offline preparation command constructs history requests from retained enrichment
and reviewed instrument definitions. The instrument file is a private JSON object
keyed by reference ticker; each value follows the existing `Instrument` contract
(immutable listing/security UUIDs, market, venue, symbol, currency and dated
`provider_symbols`). Include every supported reference listing and SPY. Massive
symbol intervals must cover the entire history window; a current reference ticker
or Yahoo profile alone does not establish historical rename mapping. Definitions
must come from reviewed identity evidence, not guessed ticker aliases.

```sh
uv run --locked python scripts/prepare_qualification_history.py \
  --enrichment-dir /private/tmp/us019-enrichment \
  --manifest /private/tmp/us019-enrichment/enrichment-workload.json \
  --instruments /absolute/path/to/reviewed-instruments.json \
  --output /private/tmp/us019-history-plan
shasum -a 256 /private/tmp/us019-history-plan/history-workload.json
# Only after rights and the exact manifest are reviewed:
uv run --locked stock-analyser qualify-universe --mode measure \
  --manifest /private/tmp/us019-history-plan/history-workload.json \
  --manifest-sha256 EXACT_FILE_SHA256 \
  --population-inputs /private/tmp/us019-history-plan/private/history-inputs.json \
  --output /private/tmp/us019-history
```

Preparation performs no provider requests. With UNKNOWN rights it refuses to read
the detailed inputs. Measurement also checks rights before loading credentials or
opening input files. Both commands refuse existing output directories. Input bytes
and the complete calculated plan must match the reviewed manifest.

`measure` fetches every distinct qualified Technology security and the independently
verified SPY benchmark for each policy-required daily/weekly window. It repeats the
fetches for a warm phase with metadata already available; these are **same-window
full refetch measurements**, not next-session cache-performance claims. Inventory
and enrichment requests and elapsed review time carry into the shared budget.
No retries, sources, caps, policy thresholds, or permissions are silently changed.

`measurements.json` records required/attempted/valid/invalid/failed/not-attempted
windows and HTTP/time deltas for each phase. Handled failures, interruption and
budget exhaustion preserve an aggregate FAIL and the private completed evidence.
Existing price validation and universe replay compute history completeness and
60-session traded value. Warm work is not marked measured when only cold work ran.
Synthetic adapters are available only to offline tests and never grant live PASS.

The native Massive provider still labels publication timing `PUBLICATION_UNVERIFIED`.
This remains an error in retained snapshots; preparation uses an unverified cutoff
policy and never promotes it to observed. Current calendar/feed support and actual
source publication evidence remain qualification requirements. Complete downloads
therefore do not by themselves imply full history qualification.

The command saves immutable snapshots under `private/`, builds
`private/population-export.json`, and binds its hash and actual source/account
records into `execution-workload.json`. Re-evaluation does not recollect data:

```sh
uv run --locked stock-analyser qualify-universe --mode execute \
  --manifest /private/tmp/us019-history/execution-workload.json \
  --manifest-sha256 EXACT_FILE_SHA256 \
  --population-inputs /private/tmp/us019-history/private/population-export.json \
  --output /private/tmp/us019-qualified
```

The existing import path also accepts a separately reviewed complete export:

```sh
uv run --locked stock-analyser qualify-universe --mode plan \
  --manifest /private/tmp/us019-enrichment/enrichment-workload.json \
  --population-inputs /absolute/path/to/permitted-complete-export.json \
  --output /private/tmp/us019-execution-plan
uv run --locked stock-analyser qualify-universe --mode execute \
  --manifest /private/tmp/us019-execution-plan/workload.json \
  --manifest-sha256 EXACT_FILE_SHA256 \
  --population-inputs /absolute/path/to/permitted-complete-export.json \
  --output /private/tmp/us019-qualified
```

The local export has exactly `origin`, `started_at`, `finished_at`, `snapshot`,
`inventories`, `mappings`, `profiles`, `budget`, and `benchmark` (a separate SPY
snapshot or null). Mappings contain verified stable IDs or explicit dated,
corroborated mappings with evidence hashes. Profiles must agree with selection's
classification records. Reference page hashes cover the full frozen reference
population in provider order. Live source bindings identify each actual source
hash, provider, dataset and account scope; source bytes are reopened and matched
to all applicable collect/retain/derive permissions. Retained benchmark prices are
checked independently. Unknown/prohibited/expired/wrong-scope rights block reads
of detailed inputs as well as native acquisition.

## Enforced transport and calculated bounds

The qualification Yahoo client uses the narrowly scoped endpoints present in the
[official yfinance source](https://github.com/ranaroussi/yfinance/blob/0.2.66/yfinance/screener/screener.py)
and quote adapter. It uses direct HTTPS; its historical application provider key
remains `yfinance`, but **SDK calls are zero**. Cookie/crumb acquisition, auth
refresh, retries and failed requests consume the shared HTTP counter. It does not
invoke the SDK's internal retry/normalization paths. Cookies and crumbs remain
in memory, not persistent library caches or reports. Redirects and consent flows
stop with a failure; no automated consent submission or source fallback occurs.

A logical operation has an explicit HTTP cap (default eight), bounded data retries
(default one), and the whole client session has at most two authentication attempts
and one credential-refresh replay. Each authentication attempt has two HTTP calls.
For O operations, the HTTP upper bound is the smaller of `O * operation_cap` and
`O * (retries + 1) + 5`; zero operations cost zero. These are enforced counters,
not an assumed SDK amplification factor. Inventory and enrichment sessions are
budgeted separately. Native HTTP makes one request per dispatch, never follows
redirects or retries implicitly, and shares the absolute deadline with DNS, TCP,
TLS, header reads and body reads. DNS subprocesses are killed/reaped on timeout or
interruption. No background collection survives a stopped operation.

Massive retains its existing persistent shared allowance, credentials and retries.
Qualification additionally caps every non-reference paginated operation using
`controls.massive_pages_per_operation`. Inventory pagination has its own explicit
page cap. Yahoo uses a separate persistent per-user allowance (`yahoo.lock`), with
at most 60 requests/minute and preserved cooldowns. Workers do not create separate
allowances; concurrency remains one. No quota or subscription is increased.

The planner requires both complete exact inventories. Initial N is all reference
rows, including exclusions/unknowns: this supplies a conservative upper bound.
Let R/D be reference/discovery pages, P the Massive operation page cap, A Massive
attempts/page (`retries + 1`), H independently configured mandatory history windows,
and Y(O) the enforced Yahoo bound above:

- Cold Massive: `R*A + (N+1)*H*P*A` (all candidates plus benchmark).
- Cold Yahoo: `Y(D) + Y(N)` (discovery and complete-reference classifications).
- Warm metadata-fresh Massive: `(N+1)*H*P*A`; Yahoo zero.
- Identity resolution uses reviewed local mappings: zero hidden HTTP calls.
- Once complete classification proves T distinct Technology securities, history
  terms use T; metadata acquisition remains budgeted across N.

Every input, control, history-requirements hash, phase/source subtotal and overall
sum is retained. A fresh complete classification proof is required to reduce the
history population. Metadata-expired refresh needs another cold budget. Network
wait/transport bounds include pacing, timeouts, bounded retry/cooldown behavior;
CPU/filesystem time is also measured against the overall deadline. Estimates do
not assert that a provider can complete inside them: exceeding a cap fails closed.

Stage preflight and report consumption independently recalculate the plan and
compare its exact values. Altered plans, incomplete inventories, mismatched
controls, insufficient per-source/overall ceilings, excessive waiting, or stale
classification cannot authorize broad collection. Inventory counts and elapsed
review time carry into enrichment accounting. Limits are explicit and configurable
(up to 100,000 overall attempts and 24 hours); defaults remain unchanged and the
24-hour metadata freshness policy remains mandatory. Larger limits require review
of a new exact manifest, not an automatic quota increase.

## Evidence gates and history

Acceptance requires every mandatory gate PASS and no blockers. The evaluator uses
an injected clock, supported versions and independent policy/criteria/history
hashes, code/lock compatibility and consistent disjoint denominators. A supplied
summary PASS never grants acceptance. Metadata expires after 24 hours; rights
review after 30 days or its earlier expiry. Synthetic fixtures cannot qualify a
live release. Historical criteria remain readable observations, never upgraded.

Daily and configured mandatory weekly windows are independently recomputed at
qualification as-of using maintained calendars. Every completed period, cutoff,
calendar-data version and adjustment requirement must match. Same-window weekend
reuse is valid; replaying an old snapshot at a new date is insufficient. Fixture
providers, synthetic/discovery feeds and unmaintained live calendars are rejected
independently of publication flags. Synthetic exports require an explicit injected
test resolver, which live qualification rejects.

The existing evaluator preserves at least 260 completed daily bars and every
configured mandatory minimum/warmup, exactly 60 completed regular sessions of raw
close × raw share volume, USD 5 minimum price and USD 10 million mean traded value,
with exact arithmetic and inclusive boundaries. Current criteria have no extra
mandatory weekly feature configured; configuring one requires supported criteria.
Missing/invalid retrieval is separate from measured low-price/liquidity exclusion.
Candidate shortfall and incomplete candidate/benchmark coverage remain blockers.

Only aggregates, fixed query/source IDs, timestamps, stable reason codes, hashes
and approved workload metadata enter findings. Hashes cannot reconstruct discarded
provider populations. Detailed replay requires separately retained, rights-permitted
files. Reports are observations, not signed attestations against a malicious author
fabricating source measurements. Handled interruption/budget/provider failures
preserve partial findings; SIGKILL, process/disk failure or failure during publication
may leave no final file. Output JSON is published atomically without overwrite.

## Permissions and remaining live evidence

The operator's 2026-10-27 confirmation remains authoritative: no account-specific
confirmation exists for either provider. To unblock, obtain applicable official
terms or written confirmation covering each account/product and dataset:

1. Massive active US reference inventory collection, metadata/identifier retention
   and internal derivation.
2. Yahoo automated API classification acquisition, verified cross-provider mappings,
   taxonomy/as-of evidence, retention and internal derivation. A complete permitted
   classification export with stable identifiers is an alternative data input.
3. Massive raw and mandatory adjusted daily/weekly histories and SPY benchmark,
   retained reproducible snapshots and internal liquidity derivation.

Each operation needs source/version/date and document hash or written-confirmation
reference, review/expiry dates, exact account/product scope, retention duration
(current intended replay period: 365 days), permitted use and no unresolved
conditions. Working API access and software licenses are not rights evidence.
The [permission request drafts and closure checklist](provider-permissions.md)
describe the external evidence still needed. No provider message has been sent.
Also required before pilot PASS: complete live inventories, classifications and
mappings, supported calendars/feeds/publication evidence, actual candidate and
benchmark coverage, and measured cold/warm workload within reviewed limits.

Official sources reviewed on 2026-10-27:
[Massive All Tickers](https://massive.com/docs/rest/stocks/tickers/all-tickers),
[Massive Market Data Terms](https://massive.com/legal/market-data-terms-of-service)
(last updated August 28, 2025),
[yfinance documentation](https://ranaroussi.github.io/yfinance/), and
[Yahoo terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html).
Yahoo terms could not be retrieved by the documentation reader. Applicable
account entitlements, automated access and retention/derivation permission remain
UNKNOWN. No source purchase is implied.

## Verification and immutable observations

```sh
uv sync --locked
uv run --locked pytest -q tests/test_qualification_collection.py tests/test_qualification.py \
  tests/test_universe_spike.py tests/test_universe.py tests/test_release_checklist.py \
  tests/test_calendars.py tests/test_massive.py tests/test_price_routing.py
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
```

Repository-wide Ruff also sees existing unrelated untracked `evaluation/` files
with lint failures; those files are preserved outside this change. Full fixture
acceptance uses a separate disposable PostgreSQL server, never the application DB:

```sh
STOCK_ANALYSER_TEST_DATABASE_URL=postgresql+psycopg://stock_analyser:local-test-password@127.0.0.1:55437/stock_analyser \
  uv run --locked python scripts/foundation_release.py \
  --output /private/tmp/alphasnitch-us019-native-release-003 \
  --qualification-evidence docs/evidence/pilot-qualification-native-final-preflight-2026-10-27.json
```

All earlier evidence remains unchanged. The initial f3438d5 run recorded 534
fixtures but missed stale-history, fixture-provenance and deadline failures.
The 98957ff repair recorded 553 passing fixtures and separate immutable
[review preflight](evidence/pilot-qualification-review-2026-10-27.json),
[release](evidence/foundation-qualification-review-release-2026-10-27.json) and
[reopen](evidence/foundation-qualification-review-reopen-2026-10-27.json) observations.
Its statement that native Yahoo collection and calculated planning were incomplete
was accurate then; those code paths are implemented by the current follow-up.
No previous preflight sample is reinterpreted as live qualification.


Current verification: **584 full fixture tests passed**, with zero failures/errors/
skips, exact suite reconciliation and mandatory fresh-process reopen PASS. The
actual full run used `/private/tmp/alphasnitch-us019-native-release-002`. After that
run, the final profile endpoint was aligned with the locked source (`query2` for
quoteSummary; `query1` for screener/crumb). **All 298 focused tests passed again**,
including explicit endpoint routing checks. Tracked-code Ruff lint/format and
`git diff --check` passed. The source was the working feature branch, marked dirty;
this is not a clean-checkout or live-provider qualification claim.

The [full checklist](evidence/foundation-qualification-native-release-2026-10-27.json)
and [reopen evidence](evidence/foundation-qualification-native-reopen-2026-10-27.json)
retain the actual full-run results and its
[preflight observation](evidence/pilot-qualification-native-preflight-2026-10-27.json).
The separately captured
[final preflight](evidence/pilot-qualification-native-final-preflight-2026-10-27.json)
and [current gate recomputation](evidence/qualification-native-final-gate-2026-10-27.json)
record the final implementation hash and reference the fixture evidence exactly.
Final preflight SHA-256:
`6cb875a5e8fb8ed99fa09ff22262b317b44baa1c25b19a909c512591a9e5b9f2`.
At observation time contract/freshness PASS; inventory, classification, identity,
history/liquidity, rights, provenance, workload and live-origin FAIL. Massive and
Yahoo HTTP attempts are both zero. All eleven earlier committed observations are
unchanged. Live acceptance and the release milestone remain blocked.

### History-stage verification (2026-10-28)

The implementation adds nine regressions covering complete cold/warm measurement
and export replay, insufficient bars, publication uncertainty, provider failure,
deadline/interruption/rights-expiry stops, unknown-rights zero dispatch, corrupted
inputs/live fixture rejection, and offline preparation from reviewed identities.
The focused suite passed **307 tests**; after final metadata-source/date checks,
all nine new tests passed again. The final full disposable-PostgreSQL checklist
passed **593 tests with zero failures/errors/skips**, exact suite reconciliation
and fresh-process reopen PASS. Ruff lint/format and diff checks passed.

The command used for the final full checklist was:

```sh
STOCK_ANALYSER_TEST_DATABASE_URL=postgresql+psycopg://stock_analyser:local-test-password@127.0.0.1:55437/stock_analyser \
  .venv/bin/python scripts/foundation_release.py \
  --output /private/tmp/alphasnitch-us019-history-release-002 \
  --qualification-evidence /private/tmp/alphasnitch-us019-history-preflight-002/findings.json
```

The exact inputs/results are retained separately as the
[preflight](evidence/pilot-qualification-history-preflight-2026-10-28.json),
[release](evidence/foundation-qualification-history-release-2026-10-28.json), and
[reopen](evidence/foundation-qualification-history-reopen-2026-10-28.json) observations.
The copied preflight SHA-256 is
`9431737cdd8d84e3aee5bb2f6a4c4e3698c126994b42e87bf500b1d2214ee3ea`.
Source was a dirty feature worktree at `8838d3e`, with exact implementation hashes
recorded in the qualification evidence. This does not claim a clean installation.
All prior observations remain unchanged. There were zero provider calls and no
provider correspondence, permission grant, subscription purchase or upgrade.
