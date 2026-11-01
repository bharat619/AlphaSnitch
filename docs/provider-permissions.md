# Provider permission evidence needed for US-019

Reviewed 2026-10-28. The operator has not supplied account-specific permission for
Massive or Yahoo. Rights dispositions remain UNKNOWN; this document grants none.
The drafts below are for the account owner to review and send. No provider has
been contacted and no subscription purchase is authorized.

## Public-source findings

Massive's market-data terms (version dated August 28, 2025) limit the default
license to personal, non-business/non-commercial use. Section 2 describes
display-only use unless superseded; section 5(d) restricts non-display/derived
uses without the required license. Section 8 requires deletion on termination.
Account and dataset agreements therefore need review before the proposed retained
liquidity calculation can be approved. [Official terms](https://massive.com/legal/market-data-terms-of-service).

Yahoo's official US terms, section on using the services, require express prior
permission for automated collection. The official page's indexed text was
accessible on this review date; direct retrieval returned HTTP 999. Applicability
to this account, any regional terms and any separate permission remain unresolved.
The native adapter's technical ability to send requests does not resolve that
condition. [Official terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html).

## Draft for Massive

Subject: Confirmation of permitted retained market-data analysis for my account

Please identify the agreement and dataset entitlements applicable to my existing
account/product. I am developing a local US Technology research workflow and need
confirmation of whether the following operations are permitted under that account:

- Automated enumeration of active US stocks and retention of listing/share-class
  identifiers and reference metadata for reproducible snapshots.
- Collection and local retention of daily OHLCV and derived completed weekly bars
  for eligible candidates and SPY, including unadjusted close/share-volume data.
- Internal computation of a 60-session mean close × share-volume traded value,
  screening/ranking, and reproducible replay, without redistribution or model use.
- Retention for up to 365 days while licensed, including any deletion obligations
  when access ends, dataset-specific limits, and permitted backups.

Please clarify whether this is permitted non-display/derived use, whether further
exchange permission is required, and the relevant feed/venue, history, rate-limit
and publication-timing scope. Please provide the controlling terms/version or
written confirmation and any unresolved conditions. This request is for a scope
determination only and does not authorize a purchase or upgrade. I will separately
provide my actual personal/business and subscriber classification; please do not
assume either from this draft.

## Draft for Yahoo

Subject: Permission for automated classification collection and local retention

Please confirm whether my account may use automated HTTPS collection of the US
equity screener and company profiles for a local US Technology research workflow.
The workflow enumerates discovery results and retrieves sector/industry, returned
symbol, exchange, currency and security type for all potentially eligible US
reference listings. It retains dated classifications and independently verified
cross-source identity mappings for reproducibility, for up to 365 days, and uses
them for internal filtering and ranking without redistribution or model use.

Please identify the applicable regional/account terms and any express prior
permission required for automation, retention and derived internal use. If these
endpoints are not permitted, please identify a permitted complete classification
export with stable listing/share-class IDs, taxonomy and as-of dates. Please state
limits, expiry/deletion obligations and unresolved conditions. This request does
not authorize a purchase or upgrade.

## Recording a response and completing the issue

Keep account identifiers, agreement text and correspondence private. Record the
actual document hash, review/expiry dates, account/product scope hash, dataset,
operation, permitted retention and conditions in the reviewed rights manifest.
Do not replace UNKNOWN with permitted from an API key, a plan name, silence or a
general permission unrelated to this dataset and use. Negative or conditional
responses remain blocking until the intended workflow is permitted.

Once rights are established, the operator still needs to:

1. Review explicit inventory limits, collect complete or explicitly incomplete
   inventories, and review the resulting calculated population budget.
2. Supply verified dated identity mappings; collect and reconcile all classifications.
3. Supply reviewed instrument definitions/history symbol intervals and SPY identity;
   run the bounded history measurement stage described in the pilot runbook.
4. Resolve actual calendar/feed/publication evidence. In particular the current
   Massive provider deliberately retains unverified publication timing; no new
   collector bypasses that status.
5. Preserve a new sanitized immutable observation, rerun the release gate, and
   retain FAIL for every remaining blocker. Close #38 only when its acceptance
   criteria are actually satisfied; fixture success is not live qualification.
