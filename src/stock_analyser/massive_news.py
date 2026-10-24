"""Bounded Massive discovery using the shared client/allowance and no raw retention."""

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from stock_analyser.massive import MassiveClient, MassiveError, ProviderCooldownError
from stock_analyser.research import NewsCoverage, NewsItem, source_url


class MassiveNewsProvider:
    def __init__(self, client: MassiveClient, *, clock=lambda: datetime.now(UTC)):
        self.client = client
        self.clock = clock

    def fetch(self, instrument, market, cutoff) -> NewsCoverage:
        market.validate_instrument(instrument)
        if market.market_id != "US" or market.route("research").provider != "massive":
            raise ValueError("Massive news requires the configured US research route")
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("News cutoff must be timezone aware")
        settings = self.client.settings
        start = cutoff - timedelta(days=settings.news_lookback_days)
        symbol = instrument.provider_symbol("massive", cutoff.date())
        # A renamed ticker requires multiple separately scoped discovery requests.
        mapping = next(
            m
            for m in instrument.provider_symbols
            if m.provider == "massive" and m.symbol == symbol and m.contains(cutoff.date())
        )
        if not mapping.contains(start.date()):
            raise ValueError("News window crosses a provider symbol change")
        items, ids, urls = [], set(), set()
        pages = duplicates = rejected = 0
        findings = []
        stopped = False
        status, reason = "EMPTY", "No items returned in this provider publication window."
        try:
            for page in self.client.pages(
                "/v2/reference/news",
                {
                    "ticker": symbol,
                    "published_utc.gte": start.astimezone(UTC).isoformat(),
                    "published_utc.lte": cutoff.astimezone(UTC).isoformat(),
                    "sort": "published_utc",
                    "order": "desc",
                    "limit": settings.news_page_size,
                },
            ):
                pages += 1
                rows = page.get("results", [])
                if not isinstance(rows, list) or len(rows) > settings.news_page_size:
                    raise MassiveError("Invalid news page")
                retrieved = self.clock()
                for row in rows:
                    try:
                        # All other fields, including headline/description/insights, are dropped
                        # before persistence. No raw news bytes or cursors go to the cache.
                        provider_id = row["id"]
                        if not isinstance(provider_id, str) or not provider_id.strip():
                            raise ValueError
                        url = source_url(row["article_url"])
                        tickers = row["tickers"]
                        if not isinstance(tickers, list) or any(
                            not isinstance(t, str) for t in tickers
                        ):
                            raise ValueError
                        item = NewsItem(
                            evidence_id=uuid5(NAMESPACE_URL, f"massive:news:{provider_id}"),
                            provider_id=provider_id,
                            source_url=url,
                            published_at=row["published_utc"],
                            retrieved_at=retrieved,
                            ticker_associated=symbol in tickers,
                        )
                        if not start <= item.published_at <= cutoff:
                            raise ValueError
                    except (ValueError, TypeError, KeyError, AttributeError):
                        rejected += 1
                        continue
                    if provider_id in ids or url in urls:
                        duplicates += 1
                    else:
                        items.append(item)
                    ids.add(provider_id)
                    urls.add(url)
                if page.get("next_url") is not None and pages >= settings.news_max_pages:
                    status, reason = "TRUNCATED", "News page bound reached; more results may exist."
                    findings.append("PAGE_LIMIT")
                    break
            else:
                status = "PARTIAL" if rejected else "COMPLETE" if items else "EMPTY"
                reason = (
                    "Invalid items were excluded; coverage is incomplete."
                    if rejected
                    else "All pages of this provider query were consumed; relevance is unreviewed."
                    if items
                    else "No items returned in this provider publication window."
                )
        except (MassiveError, ValueError, TypeError) as exc:
            # Never persist exception text, upstream error bodies or request URLs.
            stopped = isinstance(exc, ProviderCooldownError)
            status = "PARTIAL" if items else "FAILED"
            reason = "News retrieval did not finish; retained items do not establish full coverage."
            findings.append("PROVIDER_COOLDOWN" if stopped else "RETRIEVAL_FAILED")
        if rejected:
            findings.append("INVALID_ITEMS")
        if duplicates:
            findings.append("DUPLICATES_REMOVED")
        return NewsCoverage(
            provider="massive",
            provider_symbol=symbol,
            window_start=start,
            window_end=cutoff,
            retrieved_at=self.clock(),
            status=status,
            reason=reason,
            items=tuple(items),
            pages=pages,
            duplicates=duplicates,
            rejected=rejected,
            findings=tuple(findings),
            collection_stopped=stopped,
        )
