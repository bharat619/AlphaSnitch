# Approved long-history price evidence (US-008)

The router in `stock_analyser.routing` selects one approved price route before
collection. Pass all stock and compatible benchmark history resolutions to
`select_price_route`; persist its `PriceRoutingDecision` on every associated
`RunRecord`. The collection service and snapshot repository enforce this decision.
The market configuration stays intact; the selected route is a separate frozen
run input. Historical runs without this field retain their original primary route.

Massive must have the requested timeframe capabilities, a sufficient configured
history limit and an operator-verified inclusive coverage window with a reference.
The router checks actual calendar windows, including minimum plus warmup bars.
yfinance is the approved longer-history route, subject to actual per-instrument
validation. An unbounded advertised capability never guarantees returned data.
No capable route produces a decision with `route=None` and an `UNASSESSABLE`
reason. Collection records failure without contacting a provider. Runtime errors
do not alter a decision. Optional history never expands the mandatory routing
window, and an unsupported optional request can still fail its own collection.

## Explicit upstream options

The locked yfinance dependency is called through `Ticker.history` with an inclusive
start and exclusive end, `period=None`, `interval="1d"`, `auto_adjust=False`,
`back_adjust=False`, `actions=True`, `repair=False`, `keepna=True`,
`rounding=False`, `prepost=False`, `timeout=30`, and `raise_errors=True`.
The adapter preserves timezone-aware labels and normalizes them to
`America/New_York`; naive labels and mismatched exchange timezone/currency block
quality. The SDK's metadata accessor may make its own additional metadata request.

These options are defined in upstream
[history implementation](https://github.com/ranaroussi/yfinance/blob/0.2.66/yfinance/scrapers/history.py)
and the [Ticker API](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.history.html).
Yahoo's already split-adjusted OHLC is used for technical prices. Adjusted Close
includes a different adjustment basis and remains inspection-only. No synthetic
unadjusted series or validated total-return series is constructed. Dividend and
split columns remain in the source frame; no independent action-entitlement or
action-completeness guarantee is made. Repair is disabled so the adapter never
silently repairs a suspicious price. Split-like discontinuities remain subject
to the common blocking quality heuristic.

## Frozen evidence and complete weeks

The adapter receives all resolutions for a collection and requests one union
daily window per listing. Its cache lives only inside that collection's provider
instance; daily and weekly packets refer to the same retrieved frame. A new
collection constructs a fresh instance. Benchmark frames are fetched and
validated independently using the same selected provider and price basis.

`source.json` retains SDK frame rows (including nonfinite values as strings),
original timestamps, action/Adjusted Close columns, explicit request options,
currency, timezone, SDK version, retrieval metadata and normalized bars. These
are SDK response frames, not raw Yahoo HTTP bytes. SHA-256 covers the exact saved
bytes. Snapshot manifests retain resolution, quality, provenance and returned
counts through the common collection service.

Weekly aggregation requires every scheduled regular daily session between the
resolved week's first and last session. Holiday-shortened weeks are valid;
missing interior sessions, partial current weeks and recent listings cannot
satisfy the requirement. Invalid numeric values, duplicate/unordered source
labels, unexpected sessions and bad timezone/currency metadata block readiness
even when a normalized bar could otherwise be produced. Common validation then
checks OHLCV bounds, cutoffs, requested counts, stale/missing periods and
discontinuities. Invalid received rows remain inspectable in the frozen source.

Publication timing remains `unverified`. Returned data is a retrieval-time view:
Yahoo may revise old prices and apply later corporate-action adjustments. This
adapter does not establish point-in-time historical availability or validate a
total-return methodology. The CLI produces data-readiness reports and leaves
all strategy stages `NOT_RUN`.

The existing immutable run, snapshot and report-input lifecycle gives every
invocation fresh IDs. Changing provider policy requires a new run and fresh
snapshots; saved reports regenerate from their retained inputs. No indicators
are currently implemented; future computations must consume those run/snapshot
identities rather than reusing results across providers.
