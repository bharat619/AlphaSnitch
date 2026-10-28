# Technology universe qualification (US-011)

**Decision: full-sector acceptance remains blocked.** This spike qualifies the
sources and proposes policy `us-technology-proposed-1`. It does not enable Buy,
produce recommendations, or implement the subsequent persisted selection workflow.
A completed provider query and a successful test suite do not clear the gate.
A curated list can only be called a **prototype** and cannot close this gate.

## Current qualification workflow (US-019)

Use [schema 2 pilot qualification](pilot-qualification.md) for current commands,
criteria, offline planning, reviewed bounds and rights blockers. The command now
defaults to offline preflight; nonzero historical profile samples are rejected.
The following US-011 experiment describes historical v1 behavior and is retained
for interpreting the immutable October 24 evidence.

## Historical bounded experiment

```sh
uv run --locked stock-analyser qualify-universe \
  --output /absolute/path/to/new-coverage-run --max-pages 40 --profile-limit 10
```

No PostgreSQL connection is needed. Credentials and Massive pacing use the same
configuration, `.env.local` fallback and per-user lock as `collect-massive`.
The default shared allowance is five attempts/minute, at most 100 attempts,
20-second transport timeouts and two retries. Each source has at most 40 pages;
each Yahoo page has at most 250 records, each Massive page at most 1,000. Yahoo
profile enrichment is an alphabetical sample of at most ten discovered symbols
(configurable from zero to 25). Yahoo SDK calls may perform authentication and
internal retries: the bound is on SDK calls, not a guaranteed HTTP request count
or wall-clock duration. No paid subscription or new credential is needed.

An existing output directory is rejected before provider calls. A normal run saves
`findings.json`, including failures/partial enumeration, then exits **1** while
the full-sector gate is closed. Setup failures are reported separately and may
produce no file. An interrupted process may also produce no final file; rerun
with a new directory. Exit zero is reserved for acceptance, which this bounded
spike cannot establish. It has no manual override that can promote a sample.

Massive consumes the terminal page of the active US stock query without a type
filter, allowing excluded types to be counted. Yahoo uses a custom `region=us`
and `sector=Technology` query, sorted by ticker, with increasing offsets. It
requires a stable reported total, exact returned start/count and unique symbols
before calling enumeration complete. Page caps, missing totals, empty premature
pages, changed totals, duplicates, malformed records and provider failures cannot
pass. Massive's existing transport validates next-page hosts and paths, strips
cursor credentials and detects cycles. A zero-row terminal response can complete
an empty provider query; it cannot establish sector coverage.

The two populations have different semantics. `region=us` is Yahoo's classification,
not proof of all US-listed common equities. Matching ticker strings is only a
diagnostic overlap; punctuation conversions, effective symbol dates and exchange
differences need verified mappings. A symbol absent from Yahoo's Technology
screen is **unknown**, not evidence that it belongs to another sector. A stable
total also cannot prove the provider has no undocumented universe cap or omission.

## Proposed policy, version 1

The exact machine-readable proposal is `POLICY` in
`src/stock_analyser/universe_spike.py`; each report freezes it and its SHA-256.
Any changed threshold, taxonomy, identity rule or feed convention needs a new
version and a fresh universe snapshot. Values below are evaluation hypotheses,
not demonstrated profitable rules.

- **Scope:** active US-listed, USD-quoted common shares (`Massive type=CS`) on
  XNAS, XNYS, ARCX, XASE or BATS. This is a declared supported-venue population,
  not a claim to cover every US trading venue or company domiciled in the US.
  Exclude ADRs/depositary receipts, ETFs, ETNs, funds/closed-end funds, preferreds,
  warrants, rights, units, debt, OTC instruments, inactive/delisted listings and
  unknown types/venues/currencies. Foreign ordinary shares meeting these rules
  remain eligible; domicile is not an additional filter. Expanding venues needs
  calendar and feed validation; the production market currently supports fewer
  venues, so enumeration does not imply executable analysis on all five.
- **Security identity:** use share-class FIGI, not ticker, company name or CIK.
  Different share classes remain different securities; issuer identity is not
  security identity. Missing share-class/composite FIGIs or conflicting composite
  listing identities block eligibility. For multiple listings of the same share
  class choose the first supported primary venue in the order above, then
  composite FIGI and provider ticker ascending. The later selection workflow must
  bind these identifiers to immutable security/instrument UUIDs and effective
  symbol intervals. It must not mint a new identity on each ticker rename.
  Discovery coverage matches exact symbols against every eligible listing and
  counts distinct share-class FIGIs, including matches on alternate listings.
- **Taxonomy:** `yahoo-sector-map-1` maps exact Yahoo sector `Technology` to
  application sector `technology`. Other known Yahoo sectors are outside this
  scope; missing, unfamiliar or conflicting classification is unknown and blocks
  inclusion. This is Yahoo's taxonomy, **not GICS Information Technology**.
  Industry strings and Massive SIC codes cannot silently substitute for sectors.
  Future snapshots must preserve original sector/industry, provider, retrieval
  time and effective date if supplied; no effective date means a retrieval-time
  classification, not point-in-time backtest membership. The spike hashes the
  allowlisted original metadata rather than publishing a vendor database.
- **Price and liquidity:** latest completed regular-session **unadjusted close
  at least USD 5** and trailing **60-session mean traded value at least USD
  10,000,000**, inclusive boundaries. For each of the exact 60 expected exchange
  sessions ending at the common completed cutoff, use unadjusted close times
  unadjusted share volume from one validated feed, then sum and divide by 60.
  This is a close-times-volume proxy, not actual intraday dollar turnover or VWAP.
  Record USD and consolidated/venue coverage. Do not compare unlike feeds.
  A valid zero-volume session contributes zero; missing sessions, stale bars,
  nonfinite/negative values, missing adjustment basis or mixed price/volume bases
  block ranking. Never replace missing sessions with older bars or divide by fewer
  than 60. Splits must preserve each day's price/volume basis. Yahoo's already
  split-adjusted series is not approved as unadjusted liquidity evidence without
  a separate validation; the current technical-price adapter is not sufficient.
- **History:** require at least 260 completed daily sessions and independently
  satisfy every mandatory feature's minimum plus warmup. The daily requirement
  is the maximum of 260 and mandatory daily requirements; mandatory weekly
  history is separately resolved with every constituent session. Two hundred
  weekly bars cannot be inferred from 260 daily bars. Missing mandatory history
  excludes recent listings; missing optional features remains disclosed.
- **Selection:** after eligibility, classification, identity, price, liquidity
  and history gates, rank by unrounded Decimal average traded value descending,
  then immutable instrument UUID in ascending canonical form. Select at most ten
  distinct securities. Fewer than ten must be reported explicitly; never backfill
  from excluded candidates. Save the complete candidate/exclusion population,
  the 60 observations, cutoff, source hashes, policy and ranking inputs. Actual
  ranking/snapshot persistence belongs to the subsequent implementation story.

## Request workload

`measured` contains observed page counts, elapsed time, Massive HTTP responses,
Yahoo SDK call attempts and profile outcomes. Yahoo HTTP request count is unknown.
The spike makes **zero price/history requests**. Nothing in this section is a
measured cold-start or warm-refresh benchmark.

`estimated_workload` is a separate scenario with its inputs included. Let R be
Massive reference pages, D Yahoo discovery pages, N distinct common-equity
candidates needing classification, and T the Technology discovery population
(conservatively before eligibility filtering). Assume one profile call per N,
one daily union-history request per T plus one benchmark, two action pages per
ten selections plus benchmark, and one news page per selection:

- Cold start: `R + D + N + (T + 1) + 22 + 10` logical calls.
- Next completed session, metadata still fresh: `(T + 1) + 22 + 10` calls.
- Refresh after 24-hour metadata expiration: same base count as cold start.
- Exact immutable snapshot replay: zero external calls.

The warm scan still refreshes **all T candidates** to rerank them, including five
overlapping sessions for corrections. It does not merely refresh yesterday's ten.
Price routing must validate the full daily and weekly history plus warmups first;
one call assumes the chosen source can return the union window without paging.
Metadata changes and corporate-action corrections may require additional history.
Forward earnings coverage is unresolved and not counted as a working endpoint.

If prices use Massive, the base Massive share is `R + T + 1 + 32` calls and Yahoo
share is `D + N`; if prices use the approved yfinance route, Massive is `R + 32`
and Yahoo is `D + N + T + 1`. At five attempts/minute, the first Massive scenario
takes at least `(R + T + 33) / 5` minutes including initial pacing, excluding
latency, retries and provider cooldowns. Yahoo's SDK HTTP amplification remains
unknown. These are alternative plans, not permission to mix liquidity bases.

The recorded nine-times-base scenario assumes three pages per operation and
three attempts per page; it is a sensitivity scenario, **not a guaranteed upper
bound**. Larger pagination, auth requests and separate feature routes can exceed
it. With incomplete enumeration the observed inputs are only lower bounds.
Sample calculations/tests use R=12, D=4, N=4,000, T=700: 4,749 cold calls and 733
warm calls. These example inputs are synthetic, not measured population sizes.

## Gate, retention and smallest alternative

The saved findings and accompanying measured evidence document which provider
enumerations passed. Full-sector acceptance still requires all of:

1. Complete reference pagination and a reconciled classification for every
   eligible supported security, including identities absent from the screener.
2. Verified cross-provider security/listing mappings and validation of every
   supported venue's calendar/feed; unresolved identities cannot disappear.
3. Actual per-candidate history and liquidity coverage under the chosen basis,
   plus a measured cold and warm workload within a workable request allowance.
4. Confirmed rights to retain the required reproducible metadata, classification
   and price snapshots, including duration and permitted use. Access alone does
   not establish retention or redistribution permission.

The probe retains only aggregates, safe request settings, timestamps and hashes
of allowlisted metadata in this repository. It discards raw payloads, names,
cursors, provider URLs, credentials and per-symbol metadata rows. Consequently,
the hashes support comparison but cannot reconstruct the underlying population;
this is itself a disclosed **retention/reproducibility gap**, not a frozen
production universe. Classification history and historical delisted membership
also remain unverified. No news text or issuer content is collected by this spike.

The smallest next experiment is to enrich **all N eligible Massive securities**
with Yahoo classifications, resolve the unmatched symbol/venue cases, and rerun
coverage reconciliation; profiling only the ten selected stocks is insufficient.
This reuses existing sources and spends no subscription fee. It still needs
confirmation of retention rights and does not guarantee closure.

If free discovery or profile access fails, the smallest concrete external
alternative is **one complete US common-equity classification export with stable
share-class/listing identifiers, source taxonomy, as-of date and explicit local
snapshot retention permission**, joined against the enumerated Massive reference
population. FMP's company screener is a candidate for that classification role;
its documented sector filter alone does not verify uncapped enumeration, free
entitlement, identifier coverage or retention. Request a sample/export and those
specific terms before choosing a plan. No paid plan has been verified or purchased,
and no price/research subscription is justified just to close this classification
gap. A reduced or curated prototype remains labeled and leaves the gate closed.

## Source references

- [Massive All Tickers](https://massive.com/docs/rest/stocks/tickers/all-tickers)
  documents reference filtering and cursor pagination.
- [Massive Ticker Overview](https://massive.com/docs/rest/stocks/tickers/ticker-overview)
  documents identifiers and industry metadata; it does not supply the chosen Yahoo taxonomy.
- [yfinance screen](https://ranaroussi.github.io/yfinance/reference/api/yfinance.screen.html)
  documents offsets, custom queries and the 250-result page size.
- [yfinance EquityQuery](https://ranaroussi.github.io/yfinance/reference/api/yfinance.EquityQuery.html)
  documents discovery filters. Runtime uses the repository's locked SDK version.
- [FMP company screener](https://site.financialmodelingprep.com/developer/docs/stable/search-company-screener)
  is a documented candidate, not a tested entitlement or retention agreement.
