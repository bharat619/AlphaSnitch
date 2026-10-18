"""Real PostgreSQL checks use disposable databases, never application records."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from stock_analyser.cli import app
from stock_analyser.contracts import OperationalStatus, Security, SymbolMapping
from stock_analyser.persistence import Database
from stock_analyser.persistence import schema as s
from stock_analyser.persistence.database import WRITE_LOCK, postgres_url
from stock_analyser.persistence.demo import create_fixture_run
from stock_analyser.persistence.records import ArtifactRecord, RunRecord, SnapshotReference
from stock_analyser.persistence.repositories import instrument_revision


@pytest.fixture
def database():
    url = os.environ.get("STOCK_ANALYSER_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set STOCK_ANALYSER_TEST_DATABASE_URL to run PostgreSQL integration tests")
    # CREATEDB is required only for tests. Each test owns a random new database.
    admin = sa.create_engine(postgres_url(url), isolation_level="AUTOCOMMIT")
    name = f"alphasnitch_test_{uuid4().hex}"
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    db = Database(make_url(url).set(database=name).render_as_string(hide_password=False))
    try:
        yield db
    finally:
        db.close()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin.dispose()


def test_migration_idempotency_and_schema(database):
    database.initialize()
    database.initialize()
    with database.engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0003"
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(s.market_configurations)) == 1
        )
        assert compare_metadata(MigrationContext.configure(connection), s.metadata) == []
    cfg = Config()
    from stock_analyser.persistence import database as module

    cfg.set_main_option("script_location", str(Path(module.__file__).parent / "migrations"))
    with database.engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.downgrade(cfg, "base")
    database.initialize()


def test_restart_in_separate_processes(database):
    url = database.engine.url.render_as_string(hide_password=False)
    env = {**os.environ, "STOCK_ANALYSER_DATABASE_URL": url}
    created = subprocess.run(
        [sys.executable, "-m", "stock_analyser.persistence.demo", "create"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    shown = subprocess.run(
        [sys.executable, "-m", "stock_analyser.persistence.demo", "show", created.stdout.strip()],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(shown.stdout)
    assert data["run"]["configuration"] == {"fixture": True}
    assert data["run"]["request"]["requested_as_of"] == "2026-10-16T15:00:00Z"
    assert data["run"]["source_version"]["package_version"] == "0.1.0"
    assert data["run"]["component_versions"]["provider"] == "fixture-us"
    assert data["run"]["market"]["configuration_version"] == "synthetic-1"
    resolution = data["run"]["history_resolutions"][0]
    assert resolution["requested_as_of"] == "2026-10-16T15:00:00Z"
    assert resolution["periods"][-1]["session_end"] == "2026-10-15"
    assert resolution["periods"][-1]["closes_at"] == "2026-10-15T20:00:00Z"
    assert resolution["policy"]["policy_version"] == "completed-sessions-1"
    assert data["run"]["status"] == "SUCCEEDED"
    assert {x["name"]: x["status"] for x in data["stages"]} == {
        "collection": "SUCCEEDED",
        "indicators": "NOT_RUN",
        "setups": "NOT_RUN",
        "debate": "NOT_RUN",
        "verdict": "NOT_RUN",
    }
    assert data["snapshots"][0]["metadata"]["sources"][0]["sha256"]
    assert all(a["status"] == "PENDING" for a in data["artifacts"])


def test_snapshot_must_match_frozen_history_resolution(database):
    database.initialize()
    run_id = create_fixture_run(database)
    with database.read() as repo:
        snapshot = repo.list_snapshots(run_id)[0]
    metadata = snapshot.metadata.model_dump() | {
        "snapshot_id": uuid4(),
        "session_cutoff": snapshot.metadata.session_cutoff + timedelta(hours=1),
    }
    changed = SnapshotReference.model_validate(snapshot.model_dump() | {"metadata": metadata})
    with pytest.raises(ValueError, match="frozen history resolution"), database.write() as repo:
        repo.attach_snapshot(run_id, changed)
    with database.read() as repo:
        assert repo.list_snapshots(run_id) == (snapshot,)


def test_immutable_records_and_rollback(database):
    database.initialize()
    run_id = create_fixture_run(database)
    with database.read() as repo:
        original = repo.get_run(run_id)
        instrument = repo.get_instrument(original.request.instrument_ids[0])
        snapshot = repo.list_snapshots(run_id)[0]
    with database.write() as repo:
        repo.create_run(original)
        repo.put_instrument(instrument)
        repo.attach_snapshot(run_id, snapshot)
    with pytest.raises(ValueError, match="immutable"):
        with database.write() as repo:
            repo.set_run_status(run_id, OperationalStatus.FAILED)
            repo.put_market(original.market.model_copy(update={"strategy_profile": "changed"}))
    with database.read() as repo:
        assert repo.get_run(run_id) == original
        assert len(repo.list_snapshots(run_id)) == 1
    with pytest.raises(ValueError, match="immutable"):
        with database.write() as repo:
            repo.attach_snapshot(run_id, snapshot.model_copy(update={"sha256": "0" * 64}))


def test_artifact_failure_and_retry_leave_run_unchanged(database):
    database.initialize()
    run_id = create_fixture_run(database)
    with database.read() as repo:
        artifact = repo.list_artifacts(run_id)[0]
        run = repo.get_run(run_id)
    with database.write() as repo:
        repo.put_artifact(artifact.model_copy(update={"status": "FAILED", "reason": "Disk full"}))
    with database.read() as repo:
        assert repo.get_run(run_id) == run
        assert repo.list_artifacts(run_id)[0].status == "FAILED"
    saved = ArtifactRecord.model_validate(
        {
            **artifact.model_dump(),
            "status": "SAVED",
            "sha256": "a" * 64,
            "saved_at": datetime.now(UTC),
        }
    )
    with database.write() as repo:
        repo.put_artifact(saved)
        repo.put_artifact(saved)
    with database.read() as repo:
        assert repo.list_artifacts(run_id)[0] == saved
        assert repo.get_run(run_id) == run
    other_run_id = create_fixture_run(database)
    with pytest.raises(ValueError, match="identity"):
        with database.write() as repo:
            repo.put_artifact(saved.model_copy(update={"run_id": other_run_id}))


def test_market_scope_and_snapshot_cutoff(database):
    from stock_analyser.fixtures import synthetic_markets

    database.initialize()
    run_id = create_fixture_run(database)
    _, instruments, _ = synthetic_markets()
    other = instruments.resolve("TEST", "ACME")
    with database.write() as repo:
        repo.put_security(Security(security_id=other.security_id, name="Other synthetic ACME"))
        repo.put_instrument(other)
        assert repo.resolve_instrument("TEST", "ACME").quote_currency == "INR"
        assert repo.resolve_instrument("US", "ACME").quote_currency == "USD"
        snapshot = repo.list_snapshots(run_id)[0]
        run = repo.get_run(run_id)
    bad = SnapshotReference.model_validate(
        {
            **snapshot.model_dump(),
            "metadata": {
                **snapshot.metadata.model_dump(),
                "session_cutoff": run.request.requested_as_of + timedelta(days=1),
            },
        }
    )
    with pytest.raises(ValueError, match="cutoff"):
        with database.write() as repo:
            repo.attach_snapshot(run_id, bad)
    with pytest.raises(ValueError, match="market"):
        with database.write() as repo:
            repo.create_run(
                RunRecord.model_validate(
                    {
                        **run.model_dump(),
                        "run_id": uuid4(),
                        "request": {
                            **run.request.model_dump(),
                            "instrument_ids": (other.instrument_id,),
                        },
                    }
                )
            )


def test_write_lock_is_shared_between_connections(database):
    database.initialize()
    other = Database(database.engine.url.render_as_string(hide_password=False))
    try:
        with database.write():
            with other.engine.begin() as connection:
                assert (
                    connection.scalar(
                        sa.text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": WRITE_LOCK}
                    )
                    is False
                )
        with other.engine.begin() as connection:
            assert (
                connection.scalar(
                    sa.text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": WRITE_LOCK}
                )
                is True
            )
    finally:
        other.close()
    # Simultaneous initializers must not race the Alembic version table or seed.
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: database.initialize(), range(2)))


def test_cli_initializes_database(database, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    arguments = ["--data-dir", str(tmp_path), "init"]
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(app, arguments)
        assert result.exit_code == 0, result.output
        assert "PostgreSQL schema are ready" in result.output


@pytest.mark.parametrize("url", ["sqlite:///state.db", "bad-password", "postgresql://"])
def test_only_postgres_urls_are_supported(url):
    with pytest.raises(ValueError, match="PostgreSQL database"):
        Database(url)


def test_saved_artifact_requires_hash_and_timestamp():
    with pytest.raises(ValueError, match="hash and timestamp"):
        ArtifactRecord(
            artifact_id=uuid4(),
            run_id=uuid4(),
            name="report.json",
            reference="/report.json",
            status="SAVED",
        )


def test_concurrent_empty_database_initialization(database):
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: database.initialize(), range(2)))
    with database.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(s.market_configurations)) == 1
        )


def test_instrument_rename_preserves_versions_and_historical_snapshots(database):
    database.initialize()
    run_id = create_fixture_run(database)
    with database.read() as repo:
        run = repo.get_run(run_id)
        old = repo.get_instrument(run.request.instrument_ids[0])
        snapshot = repo.list_snapshots(run_id)[0]
    boundary = date(2026, 10, 16)
    renamed = old.model_copy(
        update={
            "symbol": "RENAMED",
            "provider_symbols": (
                old.provider_symbols[0].model_copy(update={"effective_to": boundary}),
                SymbolMapping(
                    provider=old.provider_symbols[0].provider,
                    symbol="RENAMED.US",
                    effective_from=boundary,
                ),
            ),
        }
    )
    with database.write() as repo:
        revision = repo.put_instrument(renamed)
        assert repo.put_instrument(renamed) == revision
        assert repo.get_instrument(old.instrument_id) == renamed
        assert repo.get_instrument(old.instrument_id, instrument_revision(old)) == old
        assert repo.resolve_instrument("US", "RENAMED") == renamed
        with pytest.raises(ValueError, match="unknown"):
            repo.resolve_instrument("US", "ACME")
        assert len(repo.list_instrument_versions(old.instrument_id)) == 2
        assert renamed.provider_symbol("fixture-us", boundary) == "RENAMED.US"
        assert renamed.provider_symbol("fixture-us", date(2026, 10, 15)) == "ACME.US"
        # Previously collected evidence remains reusable after the current listing changes.
        replay = run.model_copy(update={"run_id": uuid4()})
        repo.create_run(replay)
        repo.attach_snapshot(replay.run_id, snapshot)
    with database.read() as repo:
        assert repo.list_snapshots(run_id) == (snapshot,)
        assert repo.list_snapshots(replay.run_id) == (snapshot,)
    for field, value in (
        ("security_id", uuid4()),
        ("market_id", "TEST"),
        ("venue", "XNYS"),
        ("series", "preferred"),
    ):
        with pytest.raises(ValueError, match="identity"):
            with database.write() as repo:
                repo.put_instrument(renamed.model_copy(update={field: value}))
    unregistered = old.model_copy(update={"symbol": "UNKNOWN"})
    with pytest.raises(ValueError, match="not registered"):
        with database.write() as repo:
            repo.attach_snapshot(
                run_id,
                snapshot.model_copy(
                    update={
                        "metadata": snapshot.metadata.model_copy(
                            update={"instrument": unregistered}
                        ),
                    }
                ),
            )


def test_invalid_artifact_copy_cannot_poison_stored_state(database):
    database.initialize()
    run_id = create_fixture_run(database)
    with database.read() as repo:
        artifacts = repo.list_artifacts(run_id)
        run = repo.get_run(run_id)
    invalid = artifacts[0].model_copy(update={"status": "SAVED"})
    # Catch inside the transaction too: validation must happen before any SQL mutation.
    with database.write() as repo:
        with pytest.raises(ValueError, match="hash and timestamp"):
            repo.put_artifact(invalid)
    with database.read() as repo:
        assert repo.list_artifacts(run_id) == artifacts
        assert repo.get_run(run_id) == run
    with pytest.raises(ValueError, match="hash and timestamp"):
        with database.write() as repo:
            repo.set_run_status(run_id, OperationalStatus.FAILED)
            repo.put_artifact(invalid)
    with database.read() as repo:
        assert repo.list_artifacts(run_id) == artifacts
        assert repo.get_run(run_id) == run


def test_repeated_fixture_collection_has_content_complete_identity(database):
    from stock_analyser.fixtures import synthetic_markets

    database.initialize()
    run_id = create_fixture_run(database)
    _, _, adapters = synthetic_markets()
    with database.read() as repo:
        run = repo.get_run(run_id)
        first = repo.list_snapshots(run_id)[0]
    adapter = adapters[0]
    later = run.request.requested_as_of + timedelta(minutes=1)
    second = adapter.history(
        first.metadata.instrument, run.market, run.request.history[0], later
    ).metadata
    repeated = adapter.history(
        first.metadata.instrument, run.market, run.request.history[0], later
    ).metadata
    assert second.snapshot_id == repeated.snapshot_id
    assert second.snapshot_id != first.metadata.snapshot_id
    assert second.sources[0].sha256 == first.metadata.sources[0].sha256
    assert second.sources[0].retrieved_at != first.metadata.sources[0].retrieved_at
    with database.write() as repo:
        repo.attach_snapshot(
            run_id,
            SnapshotReference(
                metadata=second,
                reference=first.reference,
                sha256=first.sha256,
            ),
        )
        repo.attach_snapshot(
            run_id,
            SnapshotReference(
                metadata=repeated,
                reference=first.reference,
                sha256=first.sha256,
            ),
        )
    with database.read() as repo:
        snapshots = repo.list_snapshots(run_id)
        assert len(snapshots) == 2
        assert first in snapshots
    with pytest.raises(ValueError, match="immutable"):
        with database.write() as repo:
            repo.attach_snapshot(run_id, first.model_copy(update={"reference": "changed"}))


def test_instrument_version_migration_backfills_existing_records(database):
    # Simulate a database created by the existing PR, before revision 0002.
    from pathlib import Path

    from stock_analyser.fixtures import synthetic_markets
    from stock_analyser.persistence import database as module

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(module.__file__).parent / "migrations"))
    _, registry, _ = synthetic_markets()
    instrument = registry.resolve("US", "ACME")
    security = Security(security_id=instrument.security_id, name="Existing instrument")
    with database.engine.begin() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "0001")
        connection.execute(
            s.securities.insert().values(
                security_id=security.security_id,
                payload=security.model_dump(mode="json"),
            )
        )
        connection.execute(
            s.instruments.insert().values(
                instrument_id=instrument.instrument_id,
                security_id=instrument.security_id,
                market_id=instrument.market_id,
                venue=instrument.venue,
                symbol=instrument.symbol,
                currency=instrument.quote_currency,
                payload=instrument.model_dump(mode="json"),
            )
        )
    database.initialize()
    with database.read() as repo:
        assert repo.get_instrument(instrument.instrument_id) == instrument
        assert (
            repo.get_instrument(instrument.instrument_id, instrument_revision(instrument))
            == instrument
        )
        assert repo.list_instrument_versions(instrument.instrument_id) == (instrument,)


def test_currency_change_is_rejected_without_breaking_historical_run_operations(database):
    database.initialize()
    run_id = create_fixture_run(database)
    with database.read() as repo:
        run = repo.get_run(run_id)
        instrument = repo.get_instrument(run.request.instrument_ids[0])
        snapshots = repo.list_snapshots(run_id)
        stages = repo.list_stages(run_id)
        versions = repo.list_instrument_versions(instrument.instrument_id)
    changed = instrument.model_copy(update={"quote_currency": "EUR"})
    with database.write() as repo:
        # Catching rejection inside the transaction must not leave a changed current
        # projection or an extra version when the transaction subsequently commits.
        with pytest.raises(ValueError, match="identity"):
            repo.put_instrument(changed)
    with database.read() as repo:
        assert repo.get_instrument(instrument.instrument_id) == instrument
        assert repo.list_instrument_versions(instrument.instrument_id) == versions
        assert repo.list_snapshots(run_id) == snapshots
        assert repo.list_stages(run_id) == stages
    stage = stages[0].model_copy(update={"status": OperationalStatus.RUNNING})
    with database.write() as repo:
        repo.create_run(run)
        repo.put_stage(stage)
    with database.read() as repo:
        assert repo.get_run(run_id) == run
        assert stage in repo.list_stages(run_id)
        assert repo.list_snapshots(run_id) == snapshots


@pytest.mark.parametrize(
    "scenario,status,corruption",
    [
        ("valid", "SUCCEEDED", None),
        ("missing-bar", "FAILED", None),
        ("valid", "FAILED", "NaN"),
        ("valid", "FAILED", "-1"),
    ],
)
def test_collection_freezes_raw_evidence_and_persists_manifest(
    database, tmp_path, monkeypatch, scenario, status, corruption
):
    import hashlib
    from pathlib import Path

    from stock_analyser.collection import CollectionService, EvidencePacket, encode
    from stock_analyser.fixtures import RawFixtureProvider

    if corruption is not None:
        original = RawFixtureProvider.fetch

        def corrupt(self, instrument, market, resolution):
            response = original(self, instrument, market, resolution)
            payload = json.loads(response.payload)
            payload["bars"][0]["volume"] = corruption
            return response.model_copy(update={"payload": encode(payload)})

        monkeypatch.setattr(RawFixtureProvider, "fetch", corrupt)

    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    result = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "collect-fixture", "--scenario", scenario]
    )
    assert result.exit_code == (0 if status == "SUCCEEDED" else 1), result.output
    output = json.loads(result.stdout)
    run_id = UUID(output["run_id"])
    with database.read() as repo:
        assert repo.get_run(run_id).status.value == status
        assert repo.list_stages(run_id)[0].status.value == status
        snapshots = repo.list_snapshots(run_id)
        assert len(snapshots) == 1
        snapshot = snapshots[0]
        manifest = repo.list_artifacts(run_id)[0]
    assert manifest.status == "SAVED"
    content = Path(manifest.reference).read_bytes()
    assert hashlib.sha256(content).hexdigest() == manifest.sha256
    assert json.loads(content)["status"] == status
    content = Path(snapshot.reference).read_bytes()
    assert hashlib.sha256(content).hexdigest() == snapshot.sha256
    packet = EvidencePacket.model_validate_json(content)
    assert packet.metadata == snapshot.metadata
    source = packet.metadata.sources[0]
    assert hashlib.sha256(Path(source.reference).read_bytes()).hexdigest() == source.sha256
    assert packet.bars[0].volume.as_tuple().exponent == -1
    assert packet.component_versions["collection"] == "1"
    if status == "FAILED":
        assert ("MISSING_SESSION" if scenario == "missing-bar" else "INVALID_OHLCV") in (
            packet.metadata.quality_flags
        )
        assert packet.metadata.availability.value == "PARTIAL"
    else:
        assert packet.metadata.availability.value == "AVAILABLE"
    # A fresh collection produces new identities and cannot modify earlier evidence.
    again = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "collect-fixture", "--scenario", scenario]
    )
    assert json.loads(again.stdout)["evidence"][0]["snapshot_id"] != str(
        packet.metadata.snapshot_id
    )
    assert Path(snapshot.reference).read_bytes() == content

    with pytest.raises(ValueError, match="already started"):
        CollectionService(database, tmp_path / "snapshots").collect(
            run_id, packet.metadata.instrument, RawFixtureProvider(scenario)
        )
    with database.read() as repo:
        assert repo.list_snapshots(run_id) == snapshots
        assert repo.get_run(run_id).status.value == status


@pytest.mark.parametrize(
    "field,code",
    [
        ("provider", "PROVIDER_MISMATCH"),
        ("feed", "FEED_MISMATCH"),
        ("provider_symbol", "SYMBOL_MISMATCH"),
    ],
)
def test_rejected_provenance_is_frozen_and_linked_to_failed_run(
    database, tmp_path, monkeypatch, field, code
):
    import hashlib
    from pathlib import Path

    from stock_analyser.fixtures import RawFixtureProvider

    original = RawFixtureProvider.fetch
    returned = []

    def mislabeled(self, instrument, market, resolution):
        response = original(self, instrument, market, resolution)
        value = (
            response.feed.model_copy(update={"feed_id": "WRONG"}) if field == "feed" else "WRONG"
        )
        response = response.model_copy(update={field: value})
        returned.append(response)
        return response

    monkeypatch.setattr(RawFixtureProvider, "fetch", mislabeled)
    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "collect-fixture"])
    assert result.exit_code == 1, result.output
    output = json.loads(result.stdout)
    run_id = UUID(output["run_id"])
    with database.read() as repo:
        assert repo.get_run(run_id).status == OperationalStatus.FAILED
        assert repo.list_stages(run_id)[0].status == OperationalStatus.FAILED
        assert repo.list_snapshots(run_id) == ()
        artifact = next(a for a in repo.list_artifacts(run_id) if a.name == "manifest.json")
    assert artifact.status == "SAVED"
    content = Path(artifact.reference).read_bytes()
    assert hashlib.sha256(content).hexdigest() == artifact.sha256
    manifest = json.loads(content)
    assert manifest["status"] == "FAILED"
    (rejection,) = manifest["rejected_responses"]
    content = Path(rejection["reference"]).read_bytes()
    assert hashlib.sha256(content).hexdigest() == rejection["sha256"]
    diagnostic = json.loads(content)
    assert diagnostic == rejection["diagnostic"]
    assert diagnostic["run_id"] == str(run_id)
    assert diagnostic["response"] == returned[0].model_dump(mode="json", exclude={"payload"})
    assert {finding["code"] for finding in diagnostic["findings"]} == {code}
    raw = Path(diagnostic["raw_reference"]).read_bytes()
    assert raw == returned[0].payload
    assert hashlib.sha256(raw).hexdigest() == diagnostic["raw_sha256"]


def collect_report(database, tmp_path, monkeypatch, scenario="valid"):
    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    result = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "collect-fixture", "--scenario", scenario]
    )
    assert result.exit_code == (0 if scenario == "valid" else 1), result.output
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "scenario,status,availability",
    [
        ("valid", "SUCCEEDED", "AVAILABLE"),
        ("missing-bar", "FAILED", "PARTIAL"),
        ("provider-failure", "FAILED", "UNAVAILABLE"),
    ],
)
def test_report_artifacts_cover_success_and_collection_failures(
    database, tmp_path, monkeypatch, scenario, status, availability
):
    import hashlib
    from pathlib import Path

    from stock_analyser.reports import ReportService

    output = collect_report(database, tmp_path, monkeypatch, scenario)
    run_id = UUID(output["run_id"])
    assert output["report_saved"] is True
    report = json.loads(Path(output["report_json"]).read_bytes())
    manifest = json.loads(Path(output["manifest"]).read_bytes())
    markdown = Path(output["report"]).read_text()
    assert report["run_id"] == manifest["run_id"] == str(run_id)
    assert report["schema_version"] == manifest["schema_version"] == "1"
    assert report["snapshot_ids"] == manifest["snapshot_ids"]
    assert report["report"]["status"] == status
    assert report["report"]["availability"] == availability
    assert set(report["report"]["stages"].values()) == {"NOT_RUN", "1"}
    assert "recommendation" not in report["report"]
    venue = report["instruments"][0]["venue"]
    for value in (str(run_id), "USD", venue, "cutoff", "NOT_RUN", "Coverage limits"):
        assert value in markdown
    for snapshot_id in manifest["snapshot_ids"]:
        assert snapshot_id in markdown
    if scenario == "missing-bar":
        assert "MISSING_SESSION" in markdown
    if scenario == "provider-failure":
        assert "FAILED" in markdown and "Snapshot IDs: none" in markdown
    for name, info in manifest["files"].items():
        content = (Path(output["manifest"]).parent / info["reference"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == info["sha256"]
        assert name in ("report.md", "report.json")
    service = ReportService(database, tmp_path / "reports")
    with database.read() as repo:
        before = repo.list_artifacts(run_id)
        run = repo.get_run(run_id)
        inputs = repo.get_report_input(run_id)
    assert all(a.status == "SAVED" for a in before)
    assert service.generate(run_id) == tuple(
        sorted(before, key=lambda a: ("report.md", "report.json", "manifest.json").index(a.name))
    )
    with database.read() as repo:
        assert repo.get_run(run_id) == run
        assert repo.get_report_input(run_id) == inputs
    assert service.show(run_id) == markdown
    history = CliRunner().invoke(app, ["report", "history"])
    assert history.exit_code == 0, history.output
    assert str(run_id) in history.output and "report=SAVED" in history.output
    menu = CliRunner().invoke(app, [], input="5\n6\n")
    assert menu.exit_code == 0 and str(run_id) in menu.output
    assert "Reports is unavailable" not in menu.output


def test_failed_report_write_keeps_analysis_and_regenerates_after_restart(
    database, tmp_path, monkeypatch
):
    from pathlib import Path

    from stock_analyser import reports

    database.initialize()
    original_write = reports.atomic_write

    def fail_second_file(path, content):
        if path.name == "report.json":
            raise OSError("Injected full disk; private-error-details")
        original_write(path, content)

    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    with monkeypatch.context() as patch:
        patch.setattr(reports, "atomic_write", fail_second_file)
        result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "collect-fixture"])
    assert result.exit_code == 1
    assert "Report write failed" in result.output
    assert "private-error-details" not in result.output
    assert '"report_saved": true' not in result.output
    with database.read() as repo:
        (run,) = repo.list_runs()
        run_id = run.run_id
        snapshots = repo.list_snapshots(run_id)
        failed = repo.list_artifacts(run_id)
        frozen = repo.get_report_input(run_id)
    assert run.status == OperationalStatus.SUCCEEDED
    assert snapshots and all(a.status == "FAILED" for a in failed)
    assert str(run_id) in result.output
    assert not list((tmp_path / "reports").rglob("*.tmp"))
    show = CliRunner().invoke(app, ["report", "show", str(run_id)])
    assert show.exit_code == 1 and "regenerate" in show.output
    history = CliRunner().invoke(app, ["report", "history"])
    assert "INCOMPLETE" in history.output
    # Regeneration depends on the committed input, even if evidence files are unavailable.
    for snapshot in snapshots:
        Path(snapshot.reference).unlink()
    env = {
        **os.environ,
        "STOCK_ANALYSER_DATABASE_URL": database.engine.url.render_as_string(hide_password=False),
    }
    code = (
        "from stock_analyser.fixtures import RawFixtureProvider; "
        "RawFixtureProvider.fetch = lambda *a: "
        "(_ for _ in ()).throw(AssertionError('network recollection')); "
        "from stock_analyser.cli import main; main()"
    )
    regenerated = subprocess.run(
        [sys.executable, "-c", code, "report", "regenerate", str(run_id)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Report saved" in regenerated.stdout
    shown = subprocess.run(
        [sys.executable, "-m", "stock_analyser", "report", "show", str(run_id), "--json"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(shown.stdout)
    assert report["run_id"] == str(run_id)
    assert report["report"]["availability"] == "AVAILABLE"
    service = reports.ReportService(database, tmp_path / "reports")
    with database.read() as repo:
        saved = repo.list_artifacts(run_id)
        assert repo.get_run(run_id) == run
        assert repo.list_snapshots(run_id) == snapshots
        assert repo.get_report_input(run_id) == frozen
    assert {a.artifact_id for a in saved} == {a.artifact_id for a in failed}
    service.generate(run_id)
    with database.read() as repo:
        assert repo.list_artifacts(run_id) == saved
    # Tampering is detected on reopen; regeneration restores the frozen artifact.
    markdown = next(a for a in saved if a.name == "report.md")
    Path(markdown.reference).write_bytes(b"damaged")
    with pytest.raises(ValueError, match="regenerate"):
        service.show(run_id)
    assert "INCOMPLETE" in CliRunner().invoke(app, ["report", "history"]).output
    service.generate(run_id)
    assert "Data-readiness report" in service.show(run_id)


def test_interrupted_collection_reports_completed_history_and_missing_timeframe(
    database, tmp_path, monkeypatch
):
    from stock_analyser.calendars import CutoffPolicy, HistoryResolver
    from stock_analyser.collection import CollectionService
    from stock_analyser.contracts import HistoryRequirement
    from stock_analyser.fixtures import RawFixtureProvider, synthetic_session_calendars
    from stock_analyser.reports import ReportService

    output = collect_report(database, tmp_path, monkeypatch)
    with database.read() as repo:
        original = repo.get_run(UUID(output["run_id"]))
        instrument = repo.get_instrument(original.request.instrument_ids[0])
    weekly = HistoryRequirement(timeframe="weekly", minimum_bars=1)
    _, calendars = synthetic_session_calendars()
    resolution = HistoryResolver(
        calendars[0],
        CutoffPolicy(earliest_session=date(2026, 10, 5), publication_timing="synthetic"),
    ).resolve(original.market, instrument.venue, weekly, original.request.requested_as_of)
    request = original.request.model_copy(
        update={"request_id": uuid4(), "history": (*original.request.history, weekly)}
    )
    run = RunRecord(
        run_id=uuid4(),
        request=request,
        market=original.market,
        configuration=original.configuration,
        source_version=original.source_version,
        component_versions=original.component_versions,
        history_resolutions=(*original.history_resolutions, resolution),
    )
    with database.write() as repo:
        repo.create_run(run)

    class FailWeekly(RawFixtureProvider):
        def fetch(self, instrument, market, resolution):
            if resolution.requirement.timeframe == "weekly":
                raise ConnectionError("Injected second-history failure")
            return super().fetch(instrument, market, resolution)

    with pytest.raises(ConnectionError):
        CollectionService(database, tmp_path / "snapshots").collect(
            run.run_id, instrument, FailWeekly()
        )
    service = ReportService(database, tmp_path / "reports")
    service.generate(run.run_id)
    report = json.loads(service.show(run.run_id, "report.json"))
    assert report["report"]["status"] == "FAILED"
    assert report["report"]["availability"] == "PARTIAL"
    assert len(report["snapshot_ids"]) == 1
    assert report["report"]["evidence"][0]["availability"] == "AVAILABLE"
    assert "weekly" in service.show(run.run_id)


def test_blocked_report_destination_does_not_prevent_collection(database, tmp_path, monkeypatch):
    blocker = tmp_path / "report-output"
    blocker.write_text("This file prevents creating the report directory")
    monkeypatch.setenv("STOCK_ANALYSER_REPORTS_DIR", str(blocker))
    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "collect-fixture"])
    assert result.exit_code == 1 and "Report write failed" in result.output
    with database.read() as repo:
        (run,) = repo.list_runs()
        assert run.status == OperationalStatus.SUCCEEDED
        assert repo.list_snapshots(run.run_id)
    blocker.unlink()
    retry = CliRunner().invoke(app, ["report", "regenerate", str(run.run_id)])
    assert retry.exit_code == 0, retry.output
    assert "Report saved" in retry.output
    assert len(list(blocker.rglob("report.md"))) == 1


@pytest.mark.parametrize(
    "scenario", ["complete", "http-error", "empty", "short", "quality-error", "long-cooldown"]
)
def test_massive_smoke_reports_with_controlled_transport(database, tmp_path, monkeypatch, scenario):
    from contextlib import contextmanager
    from urllib.parse import unquote, urlsplit

    from pydantic import SecretStr

    from stock_analyser import live, massive
    from stock_analyser.calendars import ExchangeCalendarAdapter
    from stock_analyser.live import collect_smoke
    from stock_analyser.markets import US_MARKET
    from stock_analyser.massive import (
        HttpResponse,
        MassiveClient,
        MassiveSettings,
        ProviderAllowance,
    )
    from stock_analyser.reports import ReportService

    database.initialize()
    now = [1000.0]
    settings = MassiveSettings()
    calls = []

    def transport(target, key, timeout):
        calls.append(target)
        if scenario == "http-error":
            return HttpResponse(403, {}, b'{"error":"private-live-key"}')
        if scenario == "long-cooldown":
            return HttpResponse(429, {"Retry-After": "300"}, b"{}")
        path = urlsplit(target).path
        if path.startswith("/v2/aggs/"):
            symbol = unquote(path.split("/")[4])
            start, end = map(date.fromisoformat, path.split("/")[-2:])
            sessions = ExchangeCalendarAdapter().schedule(US_MARKET, "XNAS", start, end)
            if scenario == "empty":
                sessions = ()
            elif scenario == "short":
                sessions = sessions[:-1]
            payload = {
                "status": "OK",
                "ticker": symbol,
                "adjusted": True,
                "results": [
                    {
                        "t": int((s.opens_at - timedelta(hours=9, minutes=30)).timestamp() * 1000),
                        "o": 10,
                        "h": 12,
                        "l": 9,
                        "c": 11,
                        "v": 1.25,
                    }
                    for s in sessions
                ],
                "ignored": "private-live-key",
            }
            if scenario == "quality-error":
                for field in ("o", "h", "l", "c"):
                    payload["results"][-1][field] *= 10
        else:
            payload = {"status": "OK", "results": []}
        return HttpResponse(200, {}, json.dumps(payload).encode())

    with ProviderAllowance(
        tmp_path / "provider.lock",
        5,
        clock=lambda: now[0],
        sleep=lambda delay: now.__setitem__(0, now[0] + delay),
    ) as allowance:
        client = MassiveClient(SecretStr("private-live-key"), allowance, settings, transport)

        @contextmanager
        def controlled_client(*args):
            yield client

        monkeypatch.setattr(massive, "live_client", controlled_client)
        monkeypatch.setattr(
            live,
            "collect_smoke",
            lambda *args: collect_smoke(*args, requested=datetime(2026, 10, 17, tzinfo=UTC)),
        )
        monkeypatch.setenv("MASSIVE_API_KEY", "private-live-key")
        monkeypatch.setenv(
            "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
        )
        command = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "collect-massive"])
        assert command.exit_code == (0 if scenario == "complete" else 1), command.output
        results = json.loads(command.stdout)["reports"]
    if scenario == "long-cooldown":
        assert len(calls) == 1
        assert float((tmp_path / "provider.lock").read_text()) == now[0] + 300
        assert [r["symbol"] for r in results] == ["AAPL"]
        assert results[0]["collection_stopped"] is True
    else:
        assert [r["symbol"] for r in results] == ["AAPL", "SPY"]
    assert all(r["history_complete"] == (scenario == "complete") for r in results)
    assert len({r["smoke_group"] for r in results}) == 1
    for result in results:
        service = ReportService(database, tmp_path / "reports")
        text = service.show(UUID(result["run_id"]), "report.json")
        data = json.loads(text)
        assert data["report"]["availability"] == (
            "UNAVAILABLE" if scenario in ("http-error", "empty", "long-cooldown") else "PARTIAL"
        )
        assert "private-live-key" not in text
        assert data["report"]["stages"]["verdict"] == "NOT_RUN"
        if scenario not in ("http-error", "long-cooldown"):
            assert len(data["packets"]) == 2
            for packet in data["packets"]:
                assert "PUBLICATION_UNVERIFIED" in packet["metadata"]["quality_flags"]
                raw = json.loads(Path(packet["raw_reference"]).read_bytes())
                assert raw["corporate_actions"]["actions"] == []
                assert len(raw["capability_observations"]) == 3
        service.generate(UUID(result["run_id"]))
        assert service.show(UUID(result["run_id"]), "report.json") == text


@pytest.mark.parametrize(
    "scenario", ["complete", "short-benchmark", "transport-error", "massive-error", "unassessable"]
)
def test_routed_prices_freeze_one_source_and_reopen_reports(
    database, tmp_path, monkeypatch, scenario
):
    import pandas as pd

    from stock_analyser import live
    from stock_analyser.calendars import ExchangeCalendarAdapter
    from stock_analyser.config import load_runtime
    from stock_analyser.markets import US_MARKET
    from stock_analyser.reports import ReportService
    from stock_analyser.routing import RoutingSettings, VerifiedCoverage
    from stock_analyser.yfinance import YFinancePriceProvider

    requested = datetime(2026, 10, 17, tzinfo=UTC)
    calls = []

    class Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **options):
            calls.append(self.symbol)
            if scenario == "transport-error":
                raise OSError("private transport details")
            sessions = ExchangeCalendarAdapter().schedule(
                US_MARKET,
                "XNAS",
                date.fromisoformat(options["start"]),
                date.fromisoformat(options["end"]) - timedelta(days=1),
            )
            if scenario == "short-benchmark" and self.symbol == "SPY":
                sessions = sessions[-10:]
            return pd.DataFrame(
                {
                    "Open": 10,
                    "High": 12,
                    "Low": 9,
                    "Close": 11,
                    "Volume": 1.5,
                    "Dividends": 0.0,
                    "Stock Splits": 0.0,
                    "Adj Close": 10.5,
                },
                index=pd.DatetimeIndex([s.session for s in sessions]).tz_localize(
                    "America/New_York"
                ),
            )

        def get_history_metadata(self):
            return {"currency": "USD", "exchangeTimezoneName": "America/New_York"}

    import stock_analyser.yfinance as yf_adapter

    monkeypatch.setattr(
        yf_adapter,
        "YFinancePriceProvider",
        lambda plans: YFinancePriceProvider(
            plans,
            ticker_factory=Ticker,
            clock=lambda: requested,
        ),
    )

    class MassiveFailure:
        def __init__(self, client):
            pass

        def fetch(self, *args):
            calls.append("massive")
            raise OSError("private transport details")

    monkeypatch.setattr(live, "MassiveEvidenceProvider", MassiveFailure)
    if scenario == "unassessable":
        market = US_MARKET.model_copy(
            update={
                "configuration_version": "no-long-history-fixture",
                "capabilities": tuple(
                    c.model_copy(update={"weekly_prices": False}) for c in US_MARKET.capabilities
                ),
            }
        )
        monkeypatch.setattr(live, "US_MARKET", market)
    database.initialize()
    runtime = load_runtime(data_dir=tmp_path)
    runtime.price_routing = RoutingSettings(
        massive_coverage=VerifiedCoverage(
            start=date(2024, 10, 17), end=date(2026, 10, 16), reference="controlled account fixture"
        )
    )
    results = live.collect_smoke(
        database,
        runtime,
        object(),
        requested=requested,
        routed=True,
        weekly_bars=2 if scenario == "massive-error" else 260,
    )
    expected_provider = "massive" if scenario == "massive-error" else "yfinance"
    assert calls == (
        []
        if scenario == "unassessable"
        else ["massive", "massive"]
        if scenario == "massive-error"
        else ["AAPL", "SPY"]
    )
    service = ReportService(database, runtime.reports_dir)
    for result in results:
        run_id = UUID(result["run_id"])
        saved = service.show(run_id, "report.json")
        data = json.loads(saved)
        decision = data["run"]["price_routing"]
        if scenario == "unassessable":
            assert decision["route"] is None
            assert data["report"]["reason"].startswith("UNASSESSABLE")
        else:
            assert decision["route"]["provider"] == expected_provider
        assert "routing policy history-routing-1" in service.show(run_id)
        assert data["report"]["stages"]["verdict"] == "NOT_RUN"
        assert data["report"]["availability"] != "AVAILABLE"  # Publication remains unverified.
        assert "private transport details" not in saved
        for packet in data["packets"]:
            assert packet["metadata"]["sources"][0]["provider"] == expected_provider
        if scenario == "short-benchmark" and result["symbol"] == "SPY":
            weekly = next(p for p in data["packets"] if p["metadata"]["timeframe"] == "weekly")
            assert weekly["metadata"]["requested_bars"] == 260
            assert "INSUFFICIENT_HISTORY" in weekly["metadata"]["quality_flags"]
            assert not result["history_complete"]
        if scenario == "complete":
            assert result["history_complete"]
            assert len(data["packets"]) == 2
        # Reading/regenerating uses frozen evidence and never fetches again.
        before = list(calls)
        service.generate(run_id)
        assert service.show(run_id, "report.json") == saved
        assert calls == before
    if scenario == "complete":
        with database.read() as repo:
            original = {
                s.metadata.snapshot_id
                for r in results
                for s in repo.list_snapshots(UUID(r["run_id"]))
            }
        again = live.collect_smoke(
            database, runtime, requested=requested, routed=True, weekly_bars=260
        )
        with database.read() as repo:
            new = {
                s.metadata.snapshot_id
                for r in again
                for s in repo.list_snapshots(UUID(r["run_id"]))
            }
        assert len(original) == len(new) == 4
        assert not original.intersection(new)
        assert calls == ["AAPL", "SPY", "AAPL", "SPY"]
