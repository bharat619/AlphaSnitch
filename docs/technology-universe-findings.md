# Saved coverage findings — 2026-10-24

**Full-sector acceptance: FAIL.** Both provider queries paginated completely;
classification, cross-source identity, liquidity/history and retention remain
unqualified. This is a completed bounded spike, not an accepted sector scanner.

The unmodified [machine-readable live findings](evidence/technology-universe-2026-10-24.json)
were collected from 07:30:03 to 07:33:00 UTC on October 24, 2026 using
`qualify-universe --max-pages 40 --profile-limit 10`. Elapsed time was 177.155
seconds. The artifact freezes policy `us-technology-proposed-1`, its hash,
the implementation hash, query settings, per-page hashes/timestamps and yfinance
0.2.66. The recorded source commit is `b2a92f0`; its dirty flag includes unrelated
untracked planning files. The implementation hash was verified against the
committed module before saving this artifact. No provider payloads or credentials
are committed.

This artifact predates the alternate-listing reconciliation fix. Its implementation
hash identifies that earlier code, not the current module. The saved observation
had no eligible duplicate listings, so this fix does not change its coverage counts;
the historical JSON remains unmodified.

## Measured checks

- **PASS — Massive enumeration:** 13,260 unique active US stock records in 14
  pages (thirteen pages of 1,000 and a terminal page of 260). Fourteen HTTP 200
  responses, each on the first attempt. No pagination cap, duplicate identifier
  or unsafe cursor was encountered. This establishes completion of the supported
  provider query, not universal exchange-directory completeness.
- **PASS — Yahoo discovery enumeration:** 2,102 unique Technology/US results in
  nine SDK screener calls, with offsets 0 through 2,000. The total remained 2,102
  and the final page contained 102 results. No price, market-cap or liquidity
  filter was used. This is the complete reported query population, not just its
  displayed top companies. Yahoo HTTP requests, including SDK authentication,
  were not instrumented and remain unknown.
- **PASS, sample only — classification access:** ten alphabetically selected
  discovery profiles returned Technology, with no other-sector or unknown result.
  This tests access for those ten, not classification coverage for all candidates.
- **FAIL — reference/discovery reconciliation:** only 846 discovery symbols
  matched any Massive reference symbol exactly; 1,256 were unmatched. Of the
  exact matches, 594 met the proposed reference eligibility/identity policy.
  Venue, punctuation, security type and symbol-history differences have not been
  resolved. Exact ticker matches alone are not verified security mappings.
- **FAIL — classification completeness:** 4,168 reference securities met the
  proposed common-equity, venue, currency and identity rules. Of these, 3,574
  were outside the discovery match set. They cannot be classified as non-Technology
  solely because they were absent. No duplicate share-class listings survived
  eligibility in this observation; synthetic tests exercise the deduplication rule.
- **NOT MEASURED — price/history:** zero price/history calls were made. The 60-day
  liquidity basis, minimum price and mandatory daily/weekly coverage still need
  per-candidate verification before selection.
- **UNRESOLVED — retention:** API access did not establish snapshot retention
  rights or point-in-time membership. Counts and hashes cannot reconstruct the
  source population after the metadata rows have been discarded.

The reference exclusion counters overlap: 7,935 records had non-common or unknown
security types, 2,403 lacked listing identity, 2,566 lacked security identity, and
22 had unsupported or unknown venues. These are diagnostic reasons over all
13,260 records, not mutually exclusive buckets or counts of Technology stocks.
Unknown identity is a coverage gap, not evidence that a security is outside the
sector. The eligible population of 4,168 is therefore a qualified subset of the
reference population, not all US common shares.

## Estimated workload, kept separate from measurements

Using the observed R=14 reference pages, D=9 discovery pages and N=4,168 eligible
reference securities, the saved scenario uses T=2,102 raw discovery candidates
before filtering. Under the [documented call assumptions](technology-universe.md#request-workload):

- Cold start: **6,326 logical calls**.
- Next-session refresh with fresh metadata: **2,135 logical calls**.
- Refresh after metadata expiry: **6,326 logical calls**.
- Exact snapshot replay: **zero external calls**.
- Three pages and three attempts per logical operation: **56,934 calls** as a
  sensitivity scenario, not a guaranteed cap.

T includes discovery records that may be excluded after reconciliation. Conversely,
unresolved reference classifications could add Technology securities absent from
discovery. Neither 594 nor 2,102 is a verified full eligible-sector size, so these
estimates are scenario inputs rather than a measured workload or strict bounds.

With Massive price collection, the base scenario allocates 2,149 calls to Massive
and 4,177 to Yahoo. Five Massive attempts/minute implies at least **429.8 minutes
(about 7.2 hours)** of pacing, excluding latency, retries and cooldowns. With
yfinance prices instead, the allocation is 46 Massive and 6,280 Yahoo logical
calls; the minimum Massive pacing component is 9.2 minutes, while Yahoo timing
and HTTP amplification are unknown. Neither allocation proves the required
unadjusted liquidity or long-history coverage. No full cold or warm scan was run.

## Next decision

Keep full-sector acceptance blocked. The smallest next no-subscription experiment
is classification enrichment and verified symbol/venue reconciliation for all
4,168 currently identified reference candidates, plus resolution of missing
identities that could conceal sector members. Confirm retention rights before
retaining a reproducible universe. If these sources cannot close those gaps,
request the narrowly scoped, identified and retention-permitted classification
export described in the [policy and alternatives](technology-universe.md#gate-retention-and-smallest-alternative).
No paid plan is verified, selected or purchased. A curated prototype cannot
replace this evidence or clear the gate.
