# Earnings exposure gate

`earnings-exposure-1` evaluates the accepted earnings-avoidance research policy
against retained, normalized evidence. It produces a separate PASS, FAIL or
UNAVAILABLE result. It does not turn a technical breakout into a recommendation,
suggest a quantity, modify holdings or execute an order.

The existing news/research records are unchanged. A known date or an empty news
response still cannot clear forward earnings coverage. This slice supplies the
offline evidence contract and deterministic gate, with synthetic verification.
No live earnings provider has been selected or qualified. Version 1 deliberately
accepts only `SYNTHETIC` source qualification for a gate pass; observed evidence
remains UNAVAILABLE. Supporting a real provider requires a separately reviewed
qualification contract covering identity, coverage, freshness, publication
provenance, retention rights and corroboration.

## Explicit policy choices

The caller must supply maximum holding sessions and the pre-open cutoff buffer.
Neither has a default. `None` produces UNAVAILABLE with the missing choice named.
The walkthrough uses 20 sessions and 900 seconds solely as the provisional
research example. The holding period counts the intended next exchange session
as session 1, includes early-close sessions once, and excludes holidays/weekends.

Each source has an explicit freshness limit and qualification reference. An unset
freshness limit blocks the gate. The example's 24 hours is synthetic, not an
accepted expiry for a real source. Freshness measures age from `coverage_as_of`,
not the time an old record was downloaded. The configured age boundary is
inclusive. Publication, retrieval, observed availability and calculation times
are retained separately. Future evidence never supplies historical comparisons.

At least one issuer and one independent source are required, with distinct
declared publisher identities. Every configured source must be present once;
unconfigured or duplicate revisions block evaluation rather than silently
selecting one. The synthetic qualification declarations exercise this contract;
they are not verification of real organizational independence or usage rights.

## Retained evidence

`EarningsSourceEvidence` binds the immutable listing/security IDs, exchange
timezone, source/publisher identity and revision to:

- An explicitly verified inclusive coverage interval, coverage observation time,
  public source URL and coverage reference. Coverage must extend from the signal
  date through the complete maximum holding window. Empty event lists can pass
  only under these explicit coverage assertions and source qualifications.
- Every scheduled earnings event, its stable event identity and original
  `EarningsObservation`: source URL, publication/retrieval time, original date,
  timezone and optional timestamp, and normalized exchange-local date.
- A separate recent-event assertion: UNKNOWN, NONE_CONFIRMED or RETAINED. A recent
  event needs actual release confirmation and either confirmed call completion
  or confirmation that no call is scheduled. Unknown recent-event history blocks
  the gate even if the forward calendar is complete.

Original date-only observations in a different timezone remain unknown. They
cannot supply an exit deadline. Event publication must be known, and a record
cannot contain event information published or confirmed after its coverage
observation. Confirmation timestamps cannot postdate retrieval.

Sources must agree on event identities, dates, reported timestamps and release/
call states. Source-specific evidence IDs, URLs and retrieval times may differ.
Event list order is immaterial. A timestamp supplied by only one source is treated
as unresolved disagreement in this first contract. Conflicts are never resolved
by choosing the passing date. CANCELLED or UNRESOLVED forward events block until
a new, corroborated evidence revision resolves the calendar. Old revisions and
their original assessments remain retained.

The source records are normalized evidence assertions, not parsed issuer pages
or authenticated facts about a company. `freeze_earnings_evidence` writes their
canonical JSON under its SHA-256 using immutable storage. It neither fetches nor
qualifies a provider. Do not relabel observed data as synthetic to clear the gate.

## Calendar and comparisons

The context includes the market configuration, immutable instrument, completed
signal session, signal evidence availability and assessment cutoff. It must
refer to the latest completed scheduled session. The context's signal availability
is an explicit input; this gate does not authenticate price bars or a breakout.
Technical setup validation remains the responsibility of `qualify_breakout` and
`replay_breakout`. Composition with risk/sizing and recommendation eligibility is
later work.

The first scheduled session after the signal is the intended entry session.
Assessment must be strictly before its open minus the explicit policy buffer;
equality fails. Historical replay retains the original assessment cutoff even
when recalculated later. An old signal does not become current again after earnings.

The pre-event deadline is the close of the last exchange session strictly before
the exchange-local announcement date, including after-close announcements.
The full holding window passes when its scheduled close is at or before that
deadline. Equality passes. An event on a closed date still uses the last actual
session before that date. No-event coverage has no pre-event deadline, rather
than an invented distant event date.

For post-event eligibility, the signal session must open strictly after both the
confirmed release and any confirmed call completion. It must then finish with
usable signal evidence. A release exactly at the open fails the whole-session
requirement. A later earnings call moves this boundary; a released announcement
does not establish that its call occurred. Entry remains in the next session.

The result retains each comparison, all failures in canonical policy order,
unavailability reasons, deadlines, source records/references, policy hash and
calendar version/sessions. UNAVAILABLE takes precedence over FAIL and PASS;
valid failures remain visible alongside missing evidence. A deadline derived
from disputed or stale evidence is retained context, not a cleared exit plan.
JSON comparison key order has no significance.

Calendar evidence is obtained from the supplied adapter. Missing required sessions
produce UNAVAILABLE; malformed schedules are rejected. Scheduled future sessions
can be revised, so this is a plan under the retained calendar version, not a
guarantee that the exchange will open. Calendar replay must reproduce the retained
schedule and version.

## Replay and example

`EarningsAssessment.model_validate_json` checks internal consistency.
`replay_earnings` additionally reads and hashes every original evidence file,
queries the declared calendar and recomputes the assessment at its original
cutoff. Missing or changed evidence cannot replay a previous pass. This verifies
integrity and reproducibility, not the truth of source assertions.

Run with the locked Python environment and this checkout's `src` on `PYTHONPATH`:

```sh
python -m stock_analyser.earnings_example --output /tmp/earnings-example
```

No provider, database or model call is made. The maintained exchange-calendar
adapter supplies scheduled sessions, while all event evidence is fictitious.
The example retains JSON and exercises authenticated replay for five cases:

- `fits`: entry 9 November 2026, holding session 20 closes 7 December; earnings
  8 December has the same pre-event deadline, so PASS.
- `overlap`: earnings 7 December requires exit 4 December, so FAIL.
- `unknown-coverage`: known dates with unverified coverage remain UNAVAILABLE.
- `conflicting-date`: both original dates and the overlap failure remain visible,
  with overall UNAVAILABLE.
- `unqualified-source`: otherwise complete synthetic records remain UNAVAILABLE
  when source qualification is absent.

Thanksgiving is excluded and the 27 November early-close session counts once.
Separate tests cover timezone normalization, publication/freshness boundaries,
post-event completion, calendar changes, corruption and immutable revisions.

Risk/entry/stop/target calculations, execution-date price mapping, costs, sizing,
holding assessments, persistence/report composition and live qualification remain
subsequent slices. This contract does not select a production holding period or
authorize live use of its example freshness and timing values.

## Verification

On 15 November 2026, implementation commit `c373caa` passed 183 focused tests,
including 64 earnings cases. The complete PostgreSQL 18 release checklist passed
all 931 tests with zero failures, errors or skips, complete test collection and
the fresh-process persistence checklist. Both GitHub CI jobs passed on that
implementation revision, including all 931 Python 3.12 tests and Windows
CLI/provider locking. Ruff lint/format and whitespace checks passed.

Source hashes, artifact hashes and gate summaries are retained in
`docs/evidence/earnings-exposure-verification.json`. The verification follow-up
changes documentation/evidence only. The first sandboxed local attempt could not
connect to localhost PostgreSQL; the authorized complete rerun is the recorded
result. No live source or strategy-performance verification was performed.
