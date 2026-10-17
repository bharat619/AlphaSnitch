"""Synthetic persistence demonstration: run create and show in separate processes."""

import argparse
import json
import os
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from stock_analyser.calendars import CutoffPolicy, HistoryResolver
from stock_analyser.contracts import (
    AnalysisRequest,
    HistoryRequirement,
    OperationalStatus,
    Security,
)
from stock_analyser.fixtures import synthetic_markets, synthetic_session_calendars
from stock_analyser.persistence import Database
from stock_analyser.persistence.records import (
    ArtifactRecord,
    RunRecord,
    SnapshotReference,
    StageRecord,
)
from stock_analyser.version import source_version


def create_fixture_run(database: Database) -> UUID:
    markets, instruments, adapters = synthetic_markets()
    market = markets.resolve("US")
    instrument = instruments.resolve("US", "ACME")
    cutoff = datetime(2026, 10, 16, 15, tzinfo=UTC)
    history = HistoryRequirement(timeframe="daily", minimum_bars=1)
    _, calendars = synthetic_session_calendars()
    resolution = HistoryResolver(
        calendars[0],
        CutoffPolicy(earliest_session=date(2026, 10, 5), publication_timing="synthetic"),
    ).resolve(market, instrument.venue, history, cutoff)
    evidence = adapters[0].history(instrument, market, history, cutoff).metadata
    run = RunRecord(
        run_id=uuid4(),
        request=AnalysisRequest(
            request_id=uuid4(),
            mode="buy",
            market_id="US",
            instrument_ids=(instrument.instrument_id,),
            requested_as_of=cutoff,
            strategy_version="synthetic-1",
            history=(history,),
        ),
        market=market,
        configuration={"fixture": True},
        source_version=source_version(),
        component_versions={"fixture": "1", "provider": "fixture-us"},
        history_resolutions=(resolution,),
    )
    with database.write() as repo:
        repo.put_security(Security(security_id=instrument.security_id, name="Synthetic ACME"))
        repo.put_instrument(instrument)
        repo.create_run(run)
        repo.put_stage(
            StageRecord(
                run_id=run.run_id,
                name="collection",
                instrument_id=instrument.instrument_id,
                status=OperationalStatus.SUCCEEDED,
            )
        )
        for name in ("indicators", "setups", "debate", "verdict"):
            repo.put_stage(
                StageRecord(
                    run_id=run.run_id,
                    name=name,
                    instrument_id=instrument.instrument_id,
                )
            )
        repo.attach_snapshot(
            run.run_id,
            SnapshotReference(
                metadata=evidence,
                reference=evidence.sources[0].reference,
                sha256=evidence.sources[0].sha256,
            ),
        )
        for name in ("report.md", "report.json", "manifest.json"):
            repo.put_artifact(
                ArtifactRecord(
                    artifact_id=uuid4(),
                    run_id=run.run_id,
                    name=name,
                    reference=f"fixture://reports/{run.run_id}/{name}",
                )
            )
        repo.set_run_status(run.run_id, OperationalStatus.SUCCEEDED)
    return run.run_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "show"))
    parser.add_argument("run_id", nargs="?", type=UUID)
    args = parser.parse_args()
    url = os.environ.get("STOCK_ANALYSER_DATABASE_URL")
    if not url:
        parser.error("Set STOCK_ANALYSER_DATABASE_URL first")
    if args.action == "show" and args.run_id is None:
        parser.error("show requires a run UUID")
    database = Database(url)
    try:
        if args.action == "create":
            database.initialize()
            print(create_fixture_run(database))
        else:
            with database.read() as repo:
                print(
                    json.dumps(
                        {
                            "run": repo.get_run(args.run_id).model_dump(mode="json"),
                            "stages": [
                                s.model_dump(mode="json") for s in repo.list_stages(args.run_id)
                            ],
                            "snapshots": [
                                s.model_dump(mode="json") for s in repo.list_snapshots(args.run_id)
                            ],
                            "artifacts": [
                                a.model_dump(mode="json") for a in repo.list_artifacts(args.run_id)
                            ],
                        },
                        indent=2,
                    )
                )
    finally:
        database.close()


if __name__ == "__main__":
    main()
