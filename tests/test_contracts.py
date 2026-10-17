from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from stock_analyser.cli import app
from stock_analyser.contracts import (
    AnalysisRequest,
    DataAvailability,
    DataReadinessReport,
    EvidenceMetadata,
    HistoryRequirement,
    Instrument,
    OperationalStatus,
    PriceBar,
    Security,
    StrategyStages,
    StrategyVerdict,
    SymbolMapping,
)
from stock_analyser.fixtures import FixtureLLMProvider, synthetic_markets
from stock_analyser.markets import InstrumentRegistry, MarketRegistry
from stock_analyser.providers import PriceHistory

AS_OF = datetime(2026, 10, 16, 15, tzinfo=UTC)


def request(instrument, **overrides):
    values = dict(
        request_id=uuid4(),
        mode="buy",
        market_id=instrument.market_id,
        instrument_ids=(instrument.instrument_id,),
        requested_as_of=AS_OF,
        strategy_version="test-1",
        history=(HistoryRequirement(timeframe="daily", minimum_bars=1),),
    )
    return AnalysisRequest(**(values | overrides))


def test_same_ticker_isolated_by_market_currency_provider_and_cutoff():
    markets, listings, adapters = synthetic_markets()
    snapshots = []
    for market_id, currency, expected_session, adapter in zip(
        ("US", "TEST"),
        ("USD", "INR"),
        (date(2026, 10, 15), date(2026, 10, 16)),
        adapters,
        strict=True,
    ):
        market = markets.resolve(market_id)
        listing = listings.resolve(market_id, "ACME")
        history = adapter.history(
            listing,
            market,
            HistoryRequirement(timeframe="daily", minimum_bars=1),
            AS_OF,
        )
        assert listing.quote_currency == history.metadata.currency == currency
        assert history.bars[0].session == expected_session
        assert history.metadata.sources[0].provider == market.route("prices").provider
        assert history.metadata.sources[0].provider_symbol == f"ACME.{market_id}"
        assert history.bars[0].volume in (Decimal("1.5"), Decimal("2.5"))
        assert adapter.instruments(market, AS_OF) == (listing,)
        assert adapter.research(listing, market, AS_OF) == ()
        assert adapter.corporate_actions(listing, market, AS_OF) == ()
        snapshots.append(history)
    us, second = (listings.resolve(m, "ACME") for m in ("US", "TEST"))
    assert us.instrument_id != second.instrument_id
    assert us.security_id != second.security_id
    with pytest.raises(ValueError, match="another market"):
        adapters[0].history(second, markets.resolve("TEST"), request(second).history[0], AS_OF)
    with pytest.raises(ValueError, match="market or currency"):
        adapters[0].history(second, markets.resolve("US"), request(second).history[0], AS_OF)
    assert PriceHistory.model_validate_json(snapshots[0].model_dump_json()) == snapshots[0]


def test_identity_is_immutable_and_contracts_are_versioned():
    _, listings, _ = synthetic_markets()
    instrument = listings.resolve("US", "ACME")
    security = Security(security_id=instrument.security_id, name="Synthetic")
    for obj, field in ((instrument, "instrument_id"), (security, "security_id")):
        with pytest.raises(ValidationError, match="frozen"):
            setattr(obj, field, uuid4())
    assert Instrument.model_validate_json(instrument.model_dump_json()) == instrument
    assert instrument.schema_version == "1"
    with pytest.raises(ValidationError):
        Instrument.model_validate(instrument.model_dump() | {"schema_version": "2"})


def test_effective_dated_symbols_keep_identity_and_provider_scope():
    _, listings, _ = synthetic_markets()
    original = listings.resolve("US", "ACME")
    values = original.model_dump() | {
        "provider_symbols": (
            SymbolMapping(
                provider="fixture-us",
                symbol="OLD",
                effective_from=date(2020, 1, 1),
                effective_to=date(2025, 1, 1),
            ),
            SymbolMapping(provider="fixture-us", symbol="NEW", effective_from=date(2025, 1, 1)),
        )
    }
    listing = Instrument.model_validate(values)
    registry = InstrumentRegistry((listing,))
    assert listing.provider_symbol("fixture-us", date(2024, 12, 31)) == "OLD"
    assert listing.provider_symbol("fixture-us", date(2025, 1, 1)) == "NEW"
    assert registry.resolve_provider_symbol("US", "fixture-us", "OLD", date(2024, 1, 1)) == listing
    for market, provider, symbol, day in (
        ("US", "fixture-us", "OLD", date(2025, 1, 1)),
        ("TEST", "fixture-us", "NEW", date(2026, 1, 1)),
        ("US", "other", "NEW", date(2026, 1, 1)),
    ):
        with pytest.raises(ValueError):
            registry.resolve_provider_symbol(market, provider, symbol, day)
    with pytest.raises(ValidationError, match="overlap"):
        Instrument.model_validate(
            values
            | {
                "provider_symbols": listing.provider_symbols
                + (
                    SymbolMapping(
                        provider="fixture-us", symbol="OVERLAP", effective_from=date(2024, 1, 1)
                    ),
                )
            }
        )
    with pytest.raises(ValidationError):
        SymbolMapping(
            provider="p", symbol="x", effective_from=date(2025, 1, 1), effective_to=date(2024, 1, 1)
        )


def test_ambiguous_ticker_requires_venue():
    _, listings, _ = synthetic_markets()
    listing = listings.resolve("US", "ACME")
    second = Instrument.model_validate(
        listing.model_dump()
        | {
            "instrument_id": uuid4(),
            "venue": "XNYS",
        }
    )
    registry = InstrumentRegistry((listing, second))
    with pytest.raises(ValueError, match="ambiguous"):
        registry.resolve("US", "ACME")
    assert registry.resolve("US", "ACME", "XNYS") == second


@pytest.mark.parametrize(
    "overrides",
    [
        {"instrument_ids": ()},
        {"sector": "tech"},
        {"requested_as_of": datetime(2026, 1, 1)},
        {"strategy_version": ""},
        {"history": ()},
        {"mode": "sell"},
        {"unknown": True},
    ],
)
def test_invalid_analysis_requests(overrides):
    _, listings, _ = synthetic_markets()
    with pytest.raises(ValidationError):
        request(listings.resolve("US", "ACME"), **overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"minimum_bars": 0},
        {"minimum_bars": True},
        {"warmup_bars": -1},
        {"timeframe": "hourly"},
    ],
)
def test_invalid_history_requirements(overrides):
    with pytest.raises(ValidationError):
        HistoryRequirement.model_validate({"timeframe": "daily", "minimum_bars": 1} | overrides)


def test_data_readiness_cannot_imply_strategy_verdict():
    markets, listings, adapters = synthetic_markets()
    listing = listings.resolve("US", "ACME")
    analysis = request(listing)
    history = adapters[0].history(listing, markets.resolve("US"), analysis.history[0], AS_OF)
    report = DataReadinessReport(
        run_id=uuid4(),
        request=analysis,
        status=OperationalStatus.SUCCEEDED,
        availability=DataAvailability.AVAILABLE,
        evidence=(history.metadata,),
    )
    assert report.stages.verdict == StrategyVerdict.NOT_RUN
    assert set(report.stages.model_dump(exclude={"schema_version"}).values()) == {"NOT_RUN"}
    assert DataReadinessReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        StrategyStages(verdict="BUY")
    with pytest.raises(ValidationError):
        StrategyStages(indicators="SUCCEEDED")
    with pytest.raises(ValidationError):
        DataReadinessReport.model_validate(report.model_dump() | {"status": "BUY"})
    with pytest.raises(ValidationError, match="another market"):
        DataReadinessReport.model_validate(
            report.model_dump()
            | {
                "request": request(listings.resolve("TEST", "ACME")),
            }
        )


def test_evidence_validation_and_insufficient_history():
    markets, listings, adapters = synthetic_markets()
    listing = listings.resolve("US", "ACME")
    history = adapters[0].history(
        listing,
        markets.resolve("US"),
        HistoryRequirement(timeframe="daily", minimum_bars=260),
        AS_OF,
    )
    assert history.metadata.availability == DataAvailability.PARTIAL
    assert history.metadata.reason
    for overrides in ({"currency": "INR"}, {"availability": "AVAILABLE"}, {"reason": None}):
        with pytest.raises(ValidationError):
            EvidenceMetadata.model_validate(history.metadata.model_dump() | overrides)
    with pytest.raises(ValidationError):
        PriceHistory(metadata=history.metadata, bars=())
    with pytest.raises(ValidationError):
        PriceBar(session=date.today(), open="10", high="9", low="8", close="10", volume="1.5")


def test_public_registry_and_cli_enable_only_us(tmp_path, monkeypatch):
    assert MarketRegistry().resolve("US").currency == "USD"
    with pytest.raises(ValueError):
        MarketRegistry().resolve("TEST")
    monkeypatch.setenv("STOCK_ANALYSER_CONFIG", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text('market = "US"')
    for market in ("TEST", "IN"):
        result = CliRunner().invoke(app, ["--market", market, "doctor"])
        assert result.exit_code == 1
        assert "Only US is enabled" in result.output
    assert FixtureLLMProvider().complete("test").provider == "fixture"


def test_evidence_rejects_wrong_provider_and_symbol():
    markets, listings, adapters = synthetic_markets()
    listing = listings.resolve("US", "ACME")
    evidence = (
        adapters[0]
        .history(
            listing,
            markets.resolve("US"),
            HistoryRequirement(timeframe="daily", minimum_bars=1),
            AS_OF,
        )
        .metadata
    )
    for overrides in ({"provider": "fixture-test"}, {"provider_symbol": "ACME.TEST"}):
        source = evidence.sources[0].model_dump() | overrides
        with pytest.raises(ValidationError):
            EvidenceMetadata.model_validate(evidence.model_dump() | {"sources": (source,)})


def test_fixture_history_counts_warmup_and_rejects_unsupported_requests():
    markets, listings, adapters = synthetic_markets()
    market = markets.resolve("US")
    listing = listings.resolve("US", "ACME")
    requirement = HistoryRequirement(timeframe="daily", minimum_bars=1, warmup_bars=2)
    result = adapters[0].history(listing, market, requirement, AS_OF)
    assert result.metadata.requested_bars == 3
    assert result.metadata.availability == DataAvailability.PARTIAL
    with pytest.raises(ValueError, match="daily"):
        adapters[0].history(
            listing,
            market,
            HistoryRequirement(timeframe="weekly", minimum_bars=1),
            AS_OF,
        )
    with pytest.raises(ValueError, match="timezone aware"):
        adapters[0].history(listing, market, requirement, datetime(2026, 10, 16))
    with pytest.raises(ValueError, match="No completed session"):
        adapters[0].latest_completed_session(
            market,
            "XNAS",
            datetime(2020, 1, 1, tzinfo=UTC),
        )
