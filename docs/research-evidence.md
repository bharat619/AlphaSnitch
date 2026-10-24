# News coverage and unresolved earnings evidence (US-010)

News discovery is opt-in and independent of price routing:

```sh
uv run stock-analyser collect-massive --news
uv run stock-analyser collect-prices --news
```

`--news` uses Massive even when prices use yfinance. It shares the process lease,
rate allowance, timeout, retries and total request budget with prices/actions.
Research failures preserve price evidence. Long provider cooldowns stop further
collection, including the next smoke listing.

The `[massive]` settings are `news_lookback_days` (default 7, maximum 365),
`news_page_size` (default 100, maximum 1000), and `news_max_pages` (default 3,
maximum 100). Queries end at the frozen run cutoff. A window crossing a provider
symbol change fails explicitly instead of silently querying only one symbol.

## Coverage and relevance

Safe pagination restores ticker, publication window and sort on every page.
Deduplication uses provider IDs and canonical source URLs. Article-identifying
URL query parameters are preserved; credential/tracking parameters and fragments
are removed. Raw responses and pagination cursors are never persisted.
Credential matching uses complete names (case-insensitive, accepting `_` and `-`
variants); identifiers such as `storyKey`, `keyword` and `signatureId` are preserved.

Coverage states are `COMPLETE` (all query pages consumed), `EMPTY` (successful
query with no items), `PARTIAL` (invalid items or interrupted retrieval),
`TRUNCATED` (more pages at the configured bound), `FAILED` (retrieval failed
without usable items), and `NOT_REQUESTED`. Reasons, counts, rejected items and
query/retrieval times are retained. `COMPLETE` covers only this provider query.
Empty or failed results never establish that no news exists.

Items retain stable evidence IDs, provider IDs, source URLs, publication and
retrieval times, ticker association and relevance findings. Live relevance is
`UNASSESSED`: ticker association does not establish material relevance. Synthetic
reviewed fixtures demonstrate `RELEVANT` and `IRRELEVANT` findings. Publication
filtering is not point-in-time certification; later-retrieved metadata may have
been added or changed after the requested cutoff.

## Retention boundary

`discovery-metadata-1` is a closed metadata schema for local inspection. Provider
fields retained are article ID, public source URL and publication time; ticker
association is a boolean derived from the ticker list. Other stored fields are
application query/coverage/relevance metadata. Headlines, descriptions, authors,
branding, images, keywords, sentiment prose, article bodies and unknown fields
are discarded before storage. Publisher pages are not fetched. No excerpts are
retained and nothing is sent to a model (`model_use=NOT_SENT`).

The existing [account-permissions limitation](massive-evidence.md#retained-field-allowlist)
still applies: a technical allowlist does not establish account-specific rights.
This opt-in workflow is for authorized local personal inspection; no broader
retention, redistribution, commercial, non-display or model-use permission is
inferred. A future content-retention profile needs applicable rights verification
before adding fields. No live provider content was collected for this change.

References: [Massive News API](https://massive.com/docs/rest/stocks/news) documents
publication filters, response metadata, pagination and the 1,000-item page limit;
[market data terms](https://massive.com/terms/market_data_terms.pdf) describe the
personal-use scope and additional dataset/account restrictions.

## Earnings uncertainty

Typed earnings evidence preserves `KNOWN`, `UNKNOWN` and `CONFLICTING` states and
every observation. Observations include source/evidence IDs, retrieval/publication
times, original date/timezone and optional timestamp, exchange timezone, normalized
exchange-local date, and corroboration source when available. Date-only values in
another timezone remain unnormalized and unknown. Disagreeing normalized dates
or timestamps produce a conflict; invalid timezone/date/offset combinations fail
validation. `normalized_session_date` means the exchange-local calendar date,
not proof the exchange opens that day. After-close events are not shifted.

Live discovery records earnings as unknown: this endpoint is not a verified
forward calendar. Every state keeps `forward_coverage=UNVERIFIED` and
`mandatory_gate_cleared=false`. Even a known date alone cannot certify forward
coverage. The follow-up for later strategy acceptance is concrete: check the
issuer investor-relations earnings calendar/release, corroborate the next
announcement with an independent dated source, retain both URLs and original
timing, and resolve disagreements before a mandatory event gate can pass.

## Persistence, replay and demonstration

Migration `0004` adds immutable research records scoped by run and instrument,
saved before report publication and included in frozen inputs, Markdown, JSON
and the report manifest. Research cannot change after report freeze. Showing or
regenerating a report never calls providers or models. Renderer v2 adds research;
existing v1 inputs retain their serialization and rendering. US configuration
`us-2` adds the research route without altering saved `us-1` configurations.

For runs requesting news, a finished price stage alone does not make reporting
eligible. Every requested instrument must have a saved research coverage record,
including failed or offline coverage. Both report generation and the database
write boundary enforce this requirement, so regeneration during a news request
cannot freeze empty research and prevent the fetched evidence from being saved.

`--cache-mode replay --news` makes no network calls and explicitly records that
news was not retrieved in the new price-replay run. Reopen the original run's
report for its retained research. Research does not enter the price cache.

With PostgreSQL configured, demonstrate the distinct outcomes offline:

```sh
uv run stock-analyser collect-fixture --research-scenario relevant
uv run stock-analyser collect-fixture --research-scenario irrelevant
uv run stock-analyser collect-fixture --research-scenario empty
uv run stock-analyser collect-fixture --research-scenario failed
uv run stock-analyser collect-fixture --research-scenario conflicting-event
uv run stock-analyser collect-fixture --research-scenario known-event
uv run stock-analyser collect-fixture --research-scenario unknown-event
uv run stock-analyser report show <run-id>
uv run stock-analyser report regenerate <run-id>
```

`--research-scenario` also works alongside `--scenario provider-failure` so a
failed price collection can still display independently retained research.
