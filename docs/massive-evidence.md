# Massive evidence policy, adapter version 1

## Request and provenance boundaries

The provider uses HTTPS to `api.massive.com` with a bearer header. Redirects are
errors. Pagination must stay on the same endpoint (and ticker for aggregates);
aggregate pagination may advance its date range. Scheme, authority, path, query,
cycles and a shared attempt budget are checked. Credentials in provider
pagination URLs are removed before requesting the next page. Query invariants
are restored on every page. Credentials, opaque cursors, error bodies and
arbitrary upstream text never enter the immutable evidence files.

`MassiveClient` owns HTTP/retry/pagination behavior, `ProviderAllowance` owns the
process lease and thread-safe pacing, and price/actions adapters own their
contract checks. The composed provider feeds the existing `CollectionService`
and `ReportService`; it does not duplicate persistence or reporting logic.
The allowance is injected and must be shared by every worker/client in a run.
The CLI does this for stocks, benchmarks, actions, pages and retries. Embedded
callers must hold the same process lease throughout collection. The file is never
unlinked, and its next permissible request time is flushed to disk before a
request. A new process waits at least one configured interval before starting.
The request budget includes failed attempts. Retry-After deadlines are persisted
even when they exceed the configured wait limit. That condition stops the shared
allowance and the remaining smoke listings after saving the failed run/report.
On restart, an excessive remaining cooldown fails without sending a request or
waiting beyond the configured bound. HTTP timeouts and response size
are bounded; retries never bypass pacing. No response cache currently exists.

## Retained-field allowlist

For private local inspection, version 1 retains only:

- Daily source session, aggregate-start milliseconds, decimal OHLC and volume;
  normalized daily/weekly bars and their scheduled-close labels.
- Explicitly requested and verified split-adjustment flag, configured feed
  identifier/kind/venues, configured provider symbol, page status and row count.
- Split effective date and numeric split factors; dividend effective date,
  cash amounts, currency and optional numeric historical adjustment factors.
- Local retrieval times, HTTP status/attempt observations, query session window,
  quality findings, calendar/policy/component versions, local references and hashes.

Ticker, adjustment and numeric contracts must match. Unrecognized fields are
discarded; untrusted invalid responses are rejected with sanitized errors rather
than retained verbatim. Empty corporate-action results mean only that the
queried endpoints returned no actions in the specified window. Failure does not
establish an empty history. Actions are included inside each price source file,
covered by its hash. Provider observations are also included there. A transport
failure before a complete packet leaves a failed run/report with a sanitized
reason, rather than a claim of usable evidence.

This is a technical retention allowlist, not a finding about this account's
contractual rights. [Massive's individual terms](https://massive.com/legal/individuals-terms-of-service)
and [market-data terms](https://massive.com/legal/market-data-terms-of-service)
limit personal use and impose further restrictions. Account-specific retention,
non-display/derived use and model-use rights remain unverified. This workflow
keeps evidence local and does not publish market data or send it to an LLM.
Do not commit live evidence to Git. No subscription, professional status or
billing details are inferred from endpoint success.

## Price and corporate-action semantics

The adapter uses [daily aggregates](https://massive.com/docs/rest/stocks/aggregates/custom-bars)
with explicit `adjusted=true` for split adjustment or `false` for raw prices;
total-return requests fail. Provider timestamps label the start of the aggregate
window in Eastern Time. These timestamps are retained separately; normalized
rows use the calendar's scheduled close as a session label, not as a claim that
the provider published or finalized a bar at that instant.

Weekly bars are computed from daily bars over the resolved exchange sessions,
including holiday-shortened weeks. Every constituent must be present. Missing,
duplicate, unordered, out-of-window, invalid-price or incorrect-provenance data
cannot silently produce a ready history. Decimal fractional volume is preserved.
A provider-symbol change within a query window is rejected. The SPY benchmark
uses the same client, adjustment basis and feed configuration as AAPL.
The declared consolidated feed is configuration backed by provider documentation;
the API does not independently attest an account's exchange entitlements.

Actions use the current [splits](https://massive.com/docs/rest/stocks/corporate-actions/splits)
and [dividends](https://massive.com/docs/rest/stocks/corporate-actions/dividends)
endpoints with inclusive effective-date filters and complete pagination. Actions
are evidence; they do not automatically repair prices or turn split-adjusted
prices into total-return prices.

## Publication and freshness evidence

The current 900-second calendar grace is a candidate policy, not an observed
Massive publication SLA. [Massive documents end-of-day aggregate corrections](https://massive.com/knowledge-base/article/how-much-does-massives-feeds-handle-canceled-trades).
The timing of finalization relative to regular and early exchange closes is
unresolved. The adapter always emits `PUBLICATION_UNVERIFIED`; even a caller
supplying an observed/synthetic cutoff policy cannot remove this finding.
No current live result can claim fresh readiness.

To establish a later policy, collect bounded observations before/after actual
regular and early closes: record scheduled close, retrieval instant, source
session timestamps, returned coverage and hashes. Repeat in a later run to
measure revisions. Include delayed availability, missing sessions and corrections
in the evidence. Repeated command invocations create new immutable snapshots;
they never overwrite prior observations. A successful Saturday smoke run checks
historical access, not publication latency around Friday's close. There is no
background polling, assumed finalization time, cache reuse or automatic fallback.
Refresh overlap, correction handling and a validated grace interval must be
specified before changing the readiness gate.

## Verification

`tests/test_massive.py` uses controlled transports and clocks for 429, 5xx,
network errors, seconds/date Retry-After, bounded retries/budgets, unsafe/cyclic
pagination, decimal/metadata contracts, weekly constituent gaps, thread pacing
and a competing subprocess lock. PostgreSQL integration tests exercise both
successful transport (PARTIAL due to timing) and failed transport (UNAVAILABLE),
including hash-verified reports and deterministic offline regeneration.

The local smoke on 17 October 2026 collected AAPL and SPY in twelve paced HTTP
requests, all returning 200. Each listing retained three daily and two weekly
bars and two corporate-action query results per timeframe. Reports were saved,
artifact hashes verified, and every saved file scanned for the credential
without finding it. Both reports were PARTIAL solely because publication timing
remained unverified. Observations occurred on Saturday, after Friday's session;
this does not measure latency at an actual close. No plan/billing conclusion was
made. Live values and credentials are excluded from the repository.
