"""Real PostgreSQL checks use disposable databases, never application records."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
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
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0007"
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


def test_foundation_release_fresh_process_checklist(database, tmp_path):
    """Configuration, migration, buy/trade/sell, interrupted resume and offline reopen."""
    import hashlib

    from stock_analyser.config import load_runtime
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.ledger import TradeConfirmation, portfolio_state
    from stock_analyser.sell import SellEvidence, collect_sell
    from stock_analyser.universe_demo import fixture_request

    config = tmp_path / "config.toml"
    config.write_text('market = "US"\nstock_concurrency = 1\n')
    runtime_dir = tmp_path / "runtime"
    # Run away from the checkout so optional .env files cannot configure the demo.
    env = {key: value for key, value in os.environ.items() if not key.startswith("STOCK_ANALYSER_")}
    env.update(
        STOCK_ANALYSER_DATABASE_URL=database.engine.url.render_as_string(hide_password=False),
        NO_COLOR="1",
        TERM="dumb",
        COLUMNS="240",
    )
    prefix = [
        sys.executable,
        "-m",
        "stock_analyser",
        "--config",
        str(config),
        "--data-dir",
        str(runtime_dir),
    ]

    def cli(*args, confirmation=None):
        result = subprocess.run(
            [*prefix, *map(str, args)],
            cwd=tmp_path,
            env=env,
            input=confirmation,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    doctor = json.loads(cli("doctor"))
    assert doctor["offline_ready"] is True
    cli("init")
    cli("init")
    cli("scan", "buy", "--sector", "technology", "--fixture", "shortfall")
    with database.read() as repo:
        buy = next(r for r in repo.list_runs(100) if "buy_collection" in r.configuration)
        instrument = repo.get_instrument(buy.request.instrument_ids[0])
    before = portfolio_state(database)
    trade = TradeConfirmation(
        instrument_id=instrument.instrument_id,
        venue=instrument.venue,
        side="BUY",
        quantity="3",
        price="10",
        currency="USD",
        executed_at=datetime(2026, 10, 1, tzinfo=UTC),
        idempotency_key="release-checklist",
    )
    trade_file = tmp_path / "trade.json"
    trade_file.write_text(trade.model_dump_json())
    assert "Cancelled" in cli("record-trade", "--input", trade_file, confirmation="n\n")
    assert portfolio_state(database) == before
    cli("record-trade", "--input", trade_file, confirmation="y\n")
    receipt = json.loads(cli("execution-receipt", trade.idempotency_key))
    assert receipt["position"]["quantity"] == "3"
    cli("record-trade", "--input", trade_file, confirmation="y\n")
    assert json.loads(cli("execution-receipt", trade.idempotency_key)) == receipt
    held = portfolio_state(database)
    cli("scan", "sell", "--fixture", "valid")
    assert portfolio_state(database) == held
    with database.read() as repo:
        sell = next(r for r in repo.list_runs(100) if "sell_collection" in r.configuration)

    # Persist an actual interruption. The next CLI process must resume its frozen
    # provider/calendar identity, not infer current config or recollect completed work.
    request, resolver = fixture_request(tmp_path / "resume-inputs")
    runtime = load_runtime(config, runtime_dir)

    def interrupt(message):
        if message.startswith("Collection 1/"):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        collect_sell(
            database,
            runtime,
            SellEvidence(
                market=request.market,
                requested_as_of=request.requested_as_of,
                history=request.policy.requirements(),
                cutoff_policy=request.cutoff_policy,
            ),
            RawFixtureProvider(),
            history_resolver=resolver,
            progress=interrupt,
        )
    with database.read() as repo:
        cancelled = next(r for r in repo.list_runs(100) if r.status == OperationalStatus.CANCELLED)
    cli("scan", "resume", cancelled.run_id)
    assert portfolio_state(database) == held

    evidence = []
    for run in (buy, sell, cancelled):
        first = json.loads(cli("report", "show", run.run_id, "--json"))
        assert first["report"]["stages"]["verdict"] == "NOT_RUN"
        assert json.loads(cli("report", "show", run.run_id, "--json")) == first
        with database.read() as repo:
            artifacts = repo.list_artifacts(run.run_id)
        hashes = {
            a.name: hashlib.sha256(Path(a.reference).read_bytes()).hexdigest() for a in artifacts
        }
        cli("scan", "resume", run.run_id)
        cli("report", "regenerate", run.run_id)
        assert {
            a.name: hashlib.sha256(Path(a.reference).read_bytes()).hexdigest() for a in artifacts
        } == hashes
        evidence.append(
            {
                "run_id": str(run.run_id),
                "mode": run.request.mode,
                "artifacts": hashes,
                "fresh_process_reopen": True,
            }
        )
    cli("portfolio")
    destination = os.environ.get("STOCK_ANALYSER_ACCEPTANCE_EVIDENCE")
    if destination:
        Path(destination).write_text(
            json.dumps(
                {
                    "scope": "synthetic fixtures; disposable database removed after test",
                    "configuration_migrations_trade_confirmation_resume": "PASS",
                    "reports": evidence,
                },
                indent=2,
            )
            + "\n"
        )


def test_universe_snapshot_restart_replay_and_immutable_inputs(database, tmp_path):
    from stock_analyser.universe import replay_universe, save_universe, select_universe
    from stock_analyser.universe_demo import fixture_request

    database.initialize()
    request, resolver = fixture_request(tmp_path / "inputs")
    snapshot = select_universe(request, resolver)
    artifact = save_universe(database, tmp_path, snapshot)
    saved_bytes = artifact.read_bytes()
    assert save_universe(database, tmp_path, snapshot) == artifact
    with database.read() as repo:
        restored = repo.get_universe(snapshot.snapshot_id)
    assert replay_universe(restored) == snapshot
    with pytest.raises(ValueError, match="immutable"), database.write() as repo:
        repo.put_universe(
            snapshot.model_copy(
                update={"result": snapshot.result.model_copy(update={"selected": ()})}
            )
        )
    env = {
        **os.environ,
        "STOCK_ANALYSER_DATABASE_URL": database.engine.url.render_as_string(hide_password=False),
    }
    replayed = subprocess.run(
        [
            sys.executable,
            "-m",
            "stock_analyser",
            "universe",
            "replay",
            str(snapshot.snapshot_id),
            "--json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(replayed.stdout) == snapshot.model_dump(mode="json")
    assert artifact.read_bytes() == saved_bytes


def test_universe_file_publication_failure_can_retry_from_database(database, tmp_path, monkeypatch):
    from stock_analyser import collection
    from stock_analyser.universe import save_universe, select_universe
    from stock_analyser.universe_demo import fixture_request

    database.initialize()
    request, resolver = fixture_request(tmp_path / "inputs", eligible_count=7)
    snapshot = select_universe(request, resolver)
    original = collection.immutable_write

    def fail_publication(*args):
        raise OSError("Simulated publication interruption")

    monkeypatch.setattr(collection, "immutable_write", fail_publication)
    with pytest.raises(OSError, match="interruption"):
        save_universe(database, tmp_path, snapshot)
    with database.read() as repo:
        restored = repo.get_universe(snapshot.snapshot_id)
    assert restored == snapshot
    monkeypatch.setattr(collection, "immutable_write", original)
    assert save_universe(database, tmp_path, restored).exists()
    assert restored.result.shortfall == 3


def test_universe_cli_demo_and_replay(database, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "universe", "demo", "--shortfall"])
    assert result.exit_code == 0, result.output
    assert "PROTOTYPE" in result.output and "shortfall: 3" in result.output
    snapshot_id = next(
        line.split(": ", 1)[1]
        for line in result.output.splitlines()
        if line.startswith("Universe snapshot:")
    )
    replayed = runner.invoke(app, ["universe", "replay", snapshot_id])
    assert replayed.exit_code == 0, replayed.output
    assert "Replay verified" in replayed.output and "shortfall: 3" in replayed.output


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


def collect_report(database, tmp_path, monkeypatch, scenario="valid", research=None):
    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path), "collect-fixture", "--scenario", scenario]
        + (["--research-scenario", research] if research else []),
    )
    assert result.exit_code == (0 if scenario == "valid" else 1), result.output
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "scenario", ["relevant", "irrelevant", "empty", "failed", "conflicting-event"]
)
def test_research_saved_with_reports_and_immutable_recovery(
    database, tmp_path, monkeypatch, scenario
):
    from stock_analyser.reports import ReportService

    output = collect_report(database, tmp_path, monkeypatch, research=scenario)
    run_id = UUID(output["run_id"])
    reports = ReportService(database, tmp_path / "reports")
    frozen = reports.freeze(run_id)
    assert len(frozen.research) == 1
    record = frozen.research[0]
    with database.read() as repo:
        assert repo.list_research(run_id) == frozen.research
    manifest = json.loads(Path(output["manifest"]).read_bytes())
    assert manifest["research"] == [record.model_dump(mode="json")]
    markdown = Path(output["report"]).read_text()
    assert "News coverage and earnings uncertainty" in markdown
    assert f"Earnings {record.earnings.state}" in markdown
    assert "mandatory gate cleared: False" in markdown
    before = Path(output["report_json"]).read_bytes()
    Path(output["report_json"]).unlink()
    reports.generate(run_id)
    assert Path(output["report_json"]).read_bytes() == before
    with database.write() as repo:
        repo.put_research(record)
    changed = record.model_copy(
        update={"news": record.news.model_copy(update={"reason": "Changed"})}
    )
    with pytest.raises(ValueError, match="after report freeze"):
        with database.write() as repo:
            repo.put_research(changed)


def test_research_survives_report_write_failure(database, tmp_path, monkeypatch):
    from stock_analyser import reports

    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    with monkeypatch.context() as patch:

        def fail_write(*args):
            raise OSError("Synthetic destination failure")

        patch.setattr(reports, "atomic_write", fail_write)
        result = CliRunner().invoke(
            app,
            [
                "--data-dir",
                str(tmp_path),
                "collect-fixture",
                "--research-scenario",
                "conflicting-event",
            ],
        )
        assert result.exit_code == 1
    with database.read() as repo:
        run = repo.list_runs()[0]
        record = repo.list_research(run.run_id)[0]
        assert record.earnings.state == "CONFLICTING"
        assert repo.get_report_input(run.run_id).research == (record,)
    service = reports.ReportService(database, tmp_path / "reports")
    service.generate(run.run_id)
    assert "Earnings CONFLICTING" in service.show(run.run_id)


@pytest.mark.parametrize("cooldown", [False, True])
@pytest.mark.parametrize("price_failure", [False, True])
def test_news_uses_massive_after_yfinance_prices(
    database, tmp_path, monkeypatch, cooldown, price_failure
):
    from pydantic import SecretStr

    from stock_analyser.config import load_runtime
    from stock_analyser.contracts import DataReadinessReport
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.live import collect_smoke
    from stock_analyser.massive import (
        HttpResponse,
        MassiveClient,
        MassiveSettings,
        ProviderAllowance,
    )
    from stock_analyser.reports import ReportInput, ReportService

    class Prices:
        def __init__(self, plans):
            pass

        def fetch(self, instrument, market, resolution):
            if price_failure:
                raise OSError("Synthetic price failure")
            response = RawFixtureProvider().fetch(instrument, market, resolution)
            return response.model_copy(
                update={
                    "provider": "yfinance",
                    "feed": market.route("long_history_prices").feed,
                    "provider_symbol": instrument.provider_symbol(
                        "yfinance", resolution.session_date
                    ),
                }
            )

    monkeypatch.setattr("stock_analyser.yfinance.YFinancePriceProvider", Prices)
    database.initialize()
    runtime = load_runtime(data_dir=tmp_path)
    requested = datetime(2026, 10, 17, tzinfo=UTC)
    now, calls, factory_calls = [1000.0], [], []

    def transport(target, key, timeout):
        calls.append(target)
        assert target.startswith("/v2/reference/news?")
        # Deterministically interleave a separate report reader while news is fetching,
        # after price collection has already finished (including publication-unverified
        # history and provider failures, both of which can mark the price run FAILED).
        with database.read() as repo:
            pending = repo.list_runs()[0]
            instrument = repo.get_instrument(pending.request.instrument_ids[0])
            assert pending.status in (OperationalStatus.SUCCEEDED, OperationalStatus.FAILED)
            assert not repo.list_research(pending.run_id)
        reports = ReportService(database, runtime.reports_dir)
        with pytest.raises(ValueError, match="saved research"):
            reports.generate(pending.run_id)
        # The serialized write boundary must also reject callers bypassing ReportService.
        premature = ReportInput(
            run_id=pending.run_id,
            run=pending,
            report=DataReadinessReport(
                run_id=pending.run_id, request=pending.request, status=pending.status
            ),
            instruments=(instrument,),
            collection_stages=(),
            snapshots=(),
            packets=(),
        )
        with pytest.raises(ValueError, match="saved research"):
            with database.write() as repo:
                repo.put_report_input(premature)
        with database.read() as repo:
            with pytest.raises(LookupError):
                repo.get_report_input(pending.run_id)
            assert all(a.name == "manifest.json" for a in repo.list_artifacts(pending.run_id))
        if cooldown:
            return HttpResponse(429, {"Retry-After": "300"}, b"{}")
        return HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "results": [
                        {
                            "id": "one",
                            "article_url": "https://example.com/news/one",
                            "published_utc": "2026-10-16T12:00:00Z",
                            "tickers": ["AAPL", "SPY"],
                        }
                    ]
                }
            ).encode(),
        )

    with ProviderAllowance(
        tmp_path / "news.lock",
        5,
        clock=lambda: now[0],
        sleep=lambda delay: now.__setitem__(0, now[0] + delay),
    ) as allowance:
        client = MassiveClient(SecretStr("private-key"), allowance, MassiveSettings(), transport)

        def factory():
            factory_calls.append(True)
            return client

        results = collect_smoke(
            database,
            runtime,
            requested=requested,
            routed=True,
            weekly_bars=2,
            news=True,
            massive_client_factory=factory,
        )
    assert len(factory_calls) == 1
    assert len(results) == len(calls) == (1 if cooldown else 2)
    for result in results:
        assert result["history_complete"] == (not price_failure)
        data = json.loads(Path(result["report_json"]).read_bytes())
        assert data["run"]["price_routing"]["route"]["provider"] == "yfinance"
        assert data["research"][0]["news"]["provider"] == "massive"
        assert data["research"][0]["news"]["status"] == ("FAILED" if cooldown else "COMPLETE")
        assert result["collection_stopped"] == cooldown
        service = ReportService(database, runtime.reports_dir)
        run_id = UUID(result["run_id"])
        frozen = service.freeze(run_id)
        assert len(frozen.research) == 1
        if not cooldown:
            assert frozen.research[0].news.items[0].provider_id == "one"
        saved = Path(result["report_json"]).read_bytes()
        service.generate(run_id)
        assert Path(result["report_json"]).read_bytes() == saved


def test_cache_replay_correction_and_unchanged_report_inputs(database, tmp_path, monkeypatch):
    from stock_analyser.cache import CachePolicy, EvidenceCache, load_snapshot
    from stock_analyser.collection import CollectionService, encode
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.reports import ReportService

    output = collect_report(database, tmp_path, monkeypatch)
    original_id = UUID(output["run_id"])
    reports = ReportService(database, tmp_path / "reports")
    reports.generate(original_id)
    frozen = reports.freeze(original_id)
    with database.read() as repo:
        original = repo.get_run(original_id)
        instrument = repo.get_instrument(original.request.instrument_ids[0])
        old_reference = repo.list_snapshots(original_id)[0]
        artifacts = repo.list_artifacts(original_id)
    saved_files = {a.reference: Path(a.reference).read_bytes() for a in artifacts}
    packet = load_snapshot(old_reference)
    cache = EvidenceCache(tmp_path / "cache", CachePolicy(overlap_periods=1))
    identity = cache.identity(
        original, instrument, packet.resolution, original.market.route("prices")
    )
    cache.commit(identity, old_reference, packet)

    def new_run():
        run = RunRecord(
            run_id=uuid4(),
            request=original.request,
            market=original.market,
            configuration=original.configuration,
            source_version=original.source_version,
            component_versions=original.component_versions,
            history_resolutions=original.history_resolutions,
        )
        with database.write() as repo:
            repo.create_run(run)
        return run.run_id

    class Offline:
        def fetch(self, *args):
            pytest.fail("Offline replay attempted a provider request")

    replay_id = new_run()
    service = CollectionService(database, tmp_path / "snapshots", cache=cache)
    assert service.collect(replay_id, instrument, Offline(), cache_mode="replay") == (packet,)
    with database.read() as repo:
        assert repo.list_snapshots(replay_id) == (old_reference,)
        assert repo.get_run(replay_id).status == OperationalStatus.SUCCEEDED

    class Corrected(RawFixtureProvider):
        def fetch(self, *args):
            response = super().fetch(*args)
            payload = json.loads(response.payload)
            payload["bars"][-1]["close"] = "11.5"
            return response.model_copy(update={"payload": encode(payload)})

    refresh_id = new_run()
    corrected = service.collect(refresh_id, instrument, Corrected(), cache_mode="refresh")[0]
    assert corrected.metadata.snapshot_id != packet.metadata.snapshot_id
    assert corrected.cache_refresh["corrected_sessions"] == [str(packet.bars[-1].session)]
    assert corrected.bars[-1].close != packet.bars[-1].close
    reports.generate(refresh_id)
    assert "corrected sessions:" in reports.show(refresh_id)
    assert reports.freeze(original_id) == frozen
    reports.generate(original_id)
    assert {p: Path(p).read_bytes() for p in saved_files} == saved_files
    assert load_snapshot(old_reference) == packet
    with database.read() as repo:
        assert repo.list_snapshots(original_id) == (old_reference,)
        assert repo.list_artifacts(original_id) == artifacts


def test_cache_replay_miss_fails_without_provider(database, tmp_path, monkeypatch):
    from stock_analyser.cache import CacheMiss, EvidenceCache
    from stock_analyser.collection import CollectionService

    output = collect_report(database, tmp_path, monkeypatch)
    with database.read() as repo:
        original = repo.get_run(UUID(output["run_id"]))
        instrument = repo.get_instrument(original.request.instrument_ids[0])
    run = original.model_copy(update={"run_id": uuid4(), "status": OperationalStatus.NOT_RUN})
    with database.write() as repo:
        repo.create_run(run)
    with pytest.raises(CacheMiss, match="replay never calls a provider"):
        CollectionService(
            database, tmp_path / "snapshots", cache=EvidenceCache(tmp_path / "empty-cache")
        ).collect(run.run_id, instrument, None, cache_mode="replay")
    with database.read() as repo:
        assert repo.get_run(run.run_id).status == OperationalStatus.FAILED
        assert not repo.list_snapshots(run.run_id)


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
            lambda *args, **kwargs: collect_smoke(
                *args, requested=datetime(2026, 10, 17, tzinfo=UTC), **kwargs
            ),
        )
        monkeypatch.setenv("MASSIVE_API_KEY", "private-live-key")
        monkeypatch.setenv(
            "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
        )
        command = CliRunner().invoke(
            app, ["--data-dir", str(tmp_path), "collect-massive", "--news"]
        )
        assert command.exit_code == (0 if scenario == "complete" else 1), command.output
        results = json.loads(command.stdout)["reports"]
        if scenario == "complete":
            before = list(calls)
            monkeypatch.setattr(
                "stock_analyser.config.load_massive_key",
                lambda: pytest.fail("Replay loaded provider credentials"),
            )
            replayed = CliRunner().invoke(
                app,
                [
                    "--data-dir",
                    str(tmp_path),
                    "collect-massive",
                    "--cache-mode",
                    "replay",
                    "--news",
                ],
            )
            assert replayed.exit_code == 0, replayed.output
            assert calls == before
            replay_report = json.loads(
                Path(json.loads(replayed.stdout)["reports"][0]["report_json"]).read_bytes()
            )
            assert replay_report["research"][0]["news"]["status"] == "NOT_REQUESTED"
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
        assert data["research"][0]["news"]["status"] == (
            "FAILED" if scenario in ("http-error", "long-cooldown") else "EMPTY"
        )
        assert data["research"][0]["earnings"]["state"] == "UNKNOWN"
        assert data["research"][0]["earnings"]["mandatory_gate_cleared"] is False
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
    "scenario",
    [
        "complete",
        "400-weeks",
        "1000-weeks",
        "short-benchmark",
        "transport-error",
        "massive-error",
        "unassessable",
    ],
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
        weekly_bars={"massive-error": 2, "400-weeks": 400, "1000-weeks": 1000}.get(scenario, 260),
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
        if scenario in ("complete", "400-weeks", "1000-weeks"):
            assert result["history_complete"]
            assert len(data["packets"]) == 2
            weekly = next(p for p in data["packets"] if p["metadata"]["timeframe"] == "weekly")
            assert weekly["metadata"]["returned_bars"] == {
                "400-weeks": 400,
                "1000-weeks": 1000,
            }.get(scenario, 260)
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
        replayed = live.collect_smoke(
            database,
            runtime,
            requested=requested,
            routed=True,
            weekly_bars=260,
            cache_mode="replay",
        )
        assert calls == ["AAPL", "SPY"]
        assert all(r["history_complete"] for r in replayed)
        with database.read() as repo:
            assert {
                s.metadata.snapshot_id
                for r in replayed
                for s in repo.list_snapshots(UUID(r["run_id"]))
            } == original
        again = live.collect_smoke(
            database,
            runtime,
            requested=requested,
            routed=True,
            weekly_bars=260,
            cache_mode="off",
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


@pytest.mark.parametrize(
    ("scenario", "count", "exit_code", "outcome"),
    [
        ("valid", 10, 0, "TEN_SELECTED"),
        ("zero", 0, 0, "NO_ELIGIBLE_STOCKS"),
        ("shortfall", 3, 0, "FEWER_THAN_TEN"),
        ("one-stock-failure", 10, 1, "TEN_SELECTED"),
        ("unassessable", 10, 1, "TEN_SELECTED"),
    ],
)
def test_buy_cli_saved_sector_outcomes(
    database, tmp_path, monkeypatch, scenario, count, exit_code, outcome
):
    import threading
    import time
    from collections import Counter

    from stock_analyser.collection import CollectionService
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.reports import ReportService

    lock = threading.Lock()
    active = peak = 0
    provider_active = provider_peak = 0
    calls = Counter()
    original_collect = CollectionService.collect
    original_fetch = RawFixtureProvider.fetch

    def observed_collect(self, run_id, instrument, provider, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.01)
            return original_collect(self, run_id, instrument, provider, **kwargs)
        finally:
            with lock:
                active -= 1

    def observed_fetch(self, instrument, market, resolution):
        nonlocal provider_active, provider_peak
        with lock:
            calls[instrument.symbol] += 1
            provider_active += 1
            provider_peak = max(provider_peak, provider_active)
        try:
            time.sleep(0.001)
            return original_fetch(self, instrument, market, resolution)
        finally:
            with lock:
                provider_active -= 1

    monkeypatch.setattr(CollectionService, "collect", observed_collect)
    monkeypatch.setattr(RawFixtureProvider, "fetch", observed_fetch)
    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    config = tmp_path / "config.toml"
    config.write_text('market = "US"\nstock_concurrency = 3\n')
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config),
            "--data-dir",
            str(tmp_path),
            "scan",
            "buy",
            "--sector",
            "technology",
            "--fixture",
            scenario,
        ],
    )
    assert result.exit_code == exit_code, result.output
    with database.read() as repo:
        runs = repo.list_runs(100)
        run = next(r for r in runs if "buy_collection" in r.configuration)
        stages = repo.list_stages(run.run_id)
        stocks = [repo.get_instrument(i) for i in run.request.instrument_ids]
    service = ReportService(database, tmp_path / "reports")
    report = json.loads(service.show(run.run_id, "report.json"))
    markdown = service.show(run.run_id)
    scope = report["run"]["configuration"]["buy_collection"]
    assert scope["selection_outcome"] == outcome
    assert scope["stock_concurrency"] == 3
    assert scope["full_sector_accepted"] is False
    assert len(report["instruments"]) == count
    assert len(stages) == count
    assert peak <= 3
    if count:
        assert peak > 1
        assert calls["INDEX"] == 1
        assert provider_peak == 1
    assert len(scope["shared_context"]) == (1 if count else 0)
    if count:
        assert scope["shared_context"][0]["status"] == "SUCCEEDED"
        assert len(scope["shared_context"][0]["snapshots"]) == 1
    assert "full-sector top-ten scan not completed" in markdown
    assert "Holdings unchanged" in markdown
    assert "policy: us-technology-selection-1" in result.output
    assert "currency: USD" in result.output
    assert "cutoff: 2026-10-16" in result.output
    for stock in stocks:
        assert stock.symbol in markdown
    failed = [s for s in stages if s.status == OperationalStatus.FAILED]
    assert len(failed) == (1 if exit_code else 0)
    assert report["report"]["stages"]["verdict"] == "NOT_RUN"
    before = service.show(run.run_id)
    service.generate(run.run_id)
    assert service.show(run.run_id) == before


def test_buy_cli_frozen_input_discloses_missing_benchmark(database, tmp_path, monkeypatch):
    from stock_analyser.reports import ReportService
    from stock_analyser.universe_demo import fixture_request

    request, resolver = fixture_request(tmp_path / "frozen", eligible_count=3)
    input_path = tmp_path / "selection.json"
    input_path.write_text(request.model_dump_json())
    monkeypatch.setattr(
        "stock_analyser.calendars.ExchangeCalendarAdapter", lambda: resolver.calendar
    )
    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "scan",
            "buy",
            "--sector",
            "technology",
            "--input",
            str(input_path),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "report saved" in result.output
    with database.read() as repo:
        run = next(r for r in repo.list_runs(100) if "buy_collection" in r.configuration)
    report = json.loads(
        ReportService(database, tmp_path / "reports").show(run.run_id, "report.json")
    )
    assert len(report["instruments"]) == 3
    assert all(s["status"] == "SUCCEEDED" for s in report["collection_stages"])
    assert run.status == OperationalStatus.PARTIAL
    assert run.configuration["buy_collection"]["shared_context"][0]["status"] == "FAILED"
    assert "shared context failures" in run.reason


@pytest.mark.parametrize(("retained_bars", "benchmark_venue"), [(300, "XNAS"), (260, "ARCX")])
def test_buy_cli_frozen_superset_and_independent_benchmark(
    database, tmp_path, monkeypatch, retained_bars, benchmark_venue
):
    from stock_analyser.cache import load_snapshot
    from stock_analyser.collection import CollectionService
    from stock_analyser.contracts import AnalysisRequest, CalendarReference, HistoryRequirement
    from stock_analyser.fixtures import RawFixtureProvider, fixture_market
    from stock_analyser.reports import ReportService
    from stock_analyser.universe import SelectionPolicy, select_universe
    from stock_analyser.universe_demo import fixture_request

    market = fixture_market("US", "USD", "XNAS", "America/New_York")
    if benchmark_venue != "XNAS":
        venues = ("XNAS", benchmark_venue)
        market = market.model_copy(
            update={
                "calendars": market.calendars
                + (
                    CalendarReference(
                        venue=benchmark_venue, reference="fixture-us", version="synthetic-1"
                    ),
                ),
                "routes": tuple(
                    r.model_copy(update={"feed": r.feed.model_copy(update={"venues": venues})})
                    for r in market.routes
                ),
                "benchmarks": tuple(
                    b.model_copy(update={"venue": benchmark_venue}) for b in market.benchmarks
                ),
            }
        )
    monkeypatch.setattr("stock_analyser.universe_demo.fixture_market", lambda *args: market)
    request, resolver = fixture_request(
        tmp_path / "frozen",
        eligible_count=3,
        policy=SelectionPolicy(minimum_daily_bars=retained_bars),
    )
    # Frozen evidence can be longer than the current selection/collection policy.
    request = request.model_copy(update={"policy": SelectionPolicy()})
    requirement = HistoryRequirement(
        timeframe="daily", minimum_bars=retained_bars, adjustment="raw"
    )
    benchmark = request.histories[0].metadata.instrument.model_copy(
        update={
            "instrument_id": uuid4(),
            "security_id": uuid4(),
            "symbol": "INDEX",
            "venue": benchmark_venue,
            "provider_symbols": (
                SymbolMapping(
                    provider="fixture-us", symbol="INDEX", effective_from=date(2020, 1, 1)
                ),
            ),
        }
    )
    benchmark_plan = resolver.resolve(market, benchmark_venue, requirement, request.requested_as_of)
    benchmark_run = RunRecord(
        run_id=uuid4(),
        request=AnalysisRequest(
            request_id=uuid4(),
            mode="buy",
            market_id="US",
            instrument_ids=(benchmark.instrument_id,),
            requested_as_of=request.requested_as_of,
            strategy_version="synthetic-1",
            history=(requirement,),
        ),
        market=market,
        configuration={},
        source_version={},
        component_versions={},
        history_resolutions=(benchmark_plan,),
    )
    database.initialize()
    with database.write() as repo:
        repo.put_security(Security(security_id=benchmark.security_id, name="Synthetic benchmark"))
        repo.put_instrument(benchmark)
        repo.create_run(benchmark_run)
    CollectionService(database, tmp_path / "original").collect(
        benchmark_run.run_id, benchmark, RawFixtureProvider()
    )
    with database.read() as repo:
        benchmark_refs = repo.list_snapshots(benchmark_run.run_id)
    request = request.model_copy(update={"histories": request.histories + benchmark_refs})
    before = select_universe(request, resolver)
    assert {p.venue for p in before.resolutions} == {"XNAS"}
    original_hashes = {r.reference: r.sha256 for r in request.histories}
    input_path = tmp_path / "selection.json"
    input_path.write_text(request.model_dump_json())
    monkeypatch.setattr(
        "stock_analyser.calendars.ExchangeCalendarAdapter", lambda: resolver.calendar
    )
    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "scan",
            "buy",
            "--sector",
            "technology",
            "--input",
            str(input_path),
        ],
    )
    assert result.exit_code == 0, result.output
    with database.read() as repo:
        parent = next(r for r in repo.list_runs(100) if "buy_collection" in r.configuration)
        snapshots = repo.list_snapshots(parent.run_id)
        scope = parent.configuration["buy_collection"]
        context = scope["shared_context"][0]
        collected_benchmark = repo.get_run(UUID(context["run_id"]))
        frozen = repo.get_universe(UUID(scope["universe_snapshot_id"]))
    assert parent.status == OperationalStatus.SUCCEEDED
    assert len(snapshots) == 3
    assert all(r.metadata.returned_bars == 260 for r in snapshots)
    assert all(r.metadata.availability.value == "AVAILABLE" for r in snapshots)
    assert context["status"] == "SUCCEEDED"
    assert collected_benchmark.history_resolutions[0].venue == benchmark_venue
    assert collected_benchmark.history_resolutions[0].requested_as_of == request.requested_as_of
    assert collected_benchmark.history_resolutions[0].policy == request.cutoff_policy
    assert frozen == before  # Benchmark resolution never mutates sector selection inputs/plans.
    for reference in request.histories:
        load_snapshot(reference)
        assert reference.sha256 == original_hashes[reference.reference]
    if retained_bars > 260:
        for reference in snapshots:
            packet = load_snapshot(reference)
            projection = json.loads(Path(packet.raw_reference).read_bytes())["frozen_projection"]
            assert projection["manifest_sha256"] == original_hashes[projection["manifest"]]
            assert any(s.reference == projection["manifest"] for s in packet.metadata.sources)
            assert not packet.findings
    report = json.loads(
        ReportService(database, tmp_path / "reports").show(parent.run_id, "report.json")
    )
    assert all(s["status"] == "SUCCEEDED" for s in report["collection_stages"])
    assert report["report"]["stages"]["verdict"] == "NOT_RUN"


def ledger_instrument(database):
    from stock_analyser.contracts import Instrument

    database.initialize()
    security = Security(security_id=uuid4(), name="Ledger fixture")
    instrument = Instrument(
        instrument_id=uuid4(),
        security_id=security.security_id,
        market_id="US",
        venue="XNAS",
        symbol="LEDGER",
        quote_currency="USD",
    )
    with database.write() as repo:
        repo.put_security(security)
        repo.put_instrument(instrument)
    return instrument


def ledger_trade(instrument, side="BUY", quantity="10", price="100", **kwargs):
    from stock_analyser.ledger import TradeConfirmation

    return TradeConfirmation(
        instrument_id=instrument.instrument_id,
        venue=instrument.venue,
        side=side,
        quantity=quantity,
        price=price,
        currency="USD",
        executed_at=datetime(2025, 1, 1, tzinfo=UTC),
        idempotency_key=str(uuid4()),
        **kwargs,
    )


def test_ledger_restart_idempotency_and_conflict(database):
    from stock_analyser.ledger import execution_receipt, portfolio, record_execution

    instrument = ledger_instrument(database)
    purchase = ledger_trade(instrument)
    bought = record_execution(database, purchase)
    sale = ledger_trade(instrument, "SELL", "4", "110")
    sold = record_execution(database, sale)
    database.close()  # Force a new database connection and decode the persisted Decimal strings.
    assert record_execution(database, purchase) == bought
    assert record_execution(database, sale) == sold
    assert execution_receipt(database, sale.idempotency_key) == sold
    (position,) = portfolio(database)
    assert (position.quantity, position.cost_basis, position.realized_pnl) == (6, 600, 40)
    with pytest.raises(ValueError, match="Idempotency"):
        record_execution(database, sale.model_copy(update={"price": Decimal("111")}))
    with pytest.raises(ValueError, match="Insufficient"):
        record_execution(database, ledger_trade(instrument, "SELL", "7"))
    with database.read() as repo:
        assert repo.connection.scalar(sa.select(sa.func.count()).select_from(s.executions)) == 2
        payloads = repo.connection.scalars(sa.select(s.executions.c.payload)).all()
        assert all(isinstance(p["confirmation"]["price"], str) for p in payloads)


def test_competing_sales_and_duplicate_confirmation(database):
    from stock_analyser.ledger import portfolio, record_execution

    instrument = ledger_instrument(database)
    buy = ledger_trade(instrument)
    with ThreadPoolExecutor(max_workers=2) as workers:
        receipts = list(workers.map(lambda _: record_execution(database, buy), range(2)))
    assert receipts[0] == receipts[1]

    def sell(_):
        try:
            record_execution(database, ledger_trade(instrument, "SELL", "7"))
            return "recorded"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(sell, range(2))) == ["recorded", "rejected"]
    assert portfolio(database)[0].quantity == 3


def test_backdated_oversell_rejected_and_instrument_isolation(database):
    from stock_analyser.ledger import portfolio, record_execution

    first = ledger_instrument(database)
    second = ledger_instrument(database)
    record_execution(database, ledger_trade(first))
    with pytest.raises(ValueError, match="Insufficient"):
        record_execution(database, ledger_trade(second, "SELL", "1"))
    backdated = ledger_trade(first, "SELL", "1").model_copy(
        update={"executed_at": datetime(2024, 1, 1, tzinfo=UTC)}
    )
    with pytest.raises(ValueError, match="Insufficient"):
        record_execution(database, backdated)
    assert len(portfolio(database)) == 1


def test_trade_cli_confirmation_cancellation_and_receipt(database, tmp_path, monkeypatch):
    from stock_analyser.ledger import execution_receipt, portfolio

    instrument = ledger_instrument(database)
    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    runner = CliRunner()
    file = tmp_path / "trade.json"
    purchase = ledger_trade(instrument)
    file.write_text(purchase.model_dump_json())
    result = runner.invoke(app, ["record-trade", "--input", str(file)], input="n\n")
    assert result.exit_code == 0, result.output
    assert "Confirm actual offline execution" in result.output and "Cancelled" in result.output
    assert portfolio(database) == []
    for confirmation in (purchase, ledger_trade(instrument, "SELL", "4", "110")):
        file.write_text(confirmation.model_dump_json())
        result = runner.invoke(app, ["record-trade", "--input", str(file)], input="y\n")
        assert result.exit_code == 0, result.output
        receipt = execution_receipt(database, confirmation.idempotency_key)
        repeated = runner.invoke(app, ["record-trade", "--input", str(file)], input="y\n")
        assert repeated.exit_code == 0 and repeated.output == result.output
        assert execution_receipt(database, confirmation.idempotency_key) == receipt
    result = runner.invoke(app, ["portfolio"])
    assert result.exit_code == 0 and '"quantity": "6"' in result.output
    result = runner.invoke(app, ["execution-receipt", confirmation.idempotency_key])
    assert result.exit_code == 0 and str(receipt.execution_id) in result.output


def test_ledger_lossless_fee_storage_and_missing_fx(database):
    from stock_analyser.ledger import Fee, execution_receipt, portfolio, record_execution

    instrument = ledger_instrument(database)
    confirmation = ledger_trade(
        instrument,
        quantity="0.12345678",
        price="12345678.12345678",
        fees=(Fee(amount="0.00000001", currency="EUR"),),
    )
    receipt = record_execution(database, confirmation)
    database.close()
    restored = execution_receipt(database, confirmation.idempotency_key)
    assert restored == receipt
    assert str(restored.confirmation.quantity) == "0.12345678"
    assert str(restored.confirmation.price) == "12345678.12345678"
    assert restored.confirmation.fees[0].currency == "EUR"
    assert str(restored.confirmation.fees[0].amount) == "1E-8"
    (position,) = portfolio(database)
    assert position.quantity == confirmation.quantity
    assert position.cost_basis is None and not position.accounting_complete


def test_legacy_instrument_and_snapshot_repositories_remain_immutable_compatible(database):
    """Seed byte-equivalent pre-US-014 payloads, then read/re-save with new contracts."""
    from stock_analyser.contracts import Instrument
    from stock_analyser.persistence.repositories import instrument_revision

    legacy = json.loads(
        (Path(__file__).parent / "fixtures" / "us014-legacy-ledger-inputs.json").read_text()
    )
    payload = legacy["instrument"]
    instrument = Instrument.model_validate(payload)
    database.initialize()
    with database.write() as repo:
        repo.put_security(Security(security_id=instrument.security_id, name="Legacy fixture"))
        repo.connection.execute(
            sa.insert(s.instruments).values(
                instrument_id=instrument.instrument_id,
                security_id=instrument.security_id,
                market_id=instrument.market_id,
                venue=instrument.venue,
                symbol=instrument.symbol,
                currency=instrument.quote_currency,
                payload=payload,
            )
        )
        repo.connection.execute(
            sa.insert(s.instrument_versions).values(
                instrument_id=instrument.instrument_id,
                revision=legacy["instrument_revision"],
                payload=payload,
            )
        )
        repo.connection.execute(
            sa.insert(s.universe_snapshots).values(
                snapshot_id=UUID(legacy["snapshot"]["snapshot_id"]),
                payload=legacy["snapshot"],
            )
        )
    with database.write() as repo:
        restored = repo.get_instrument(instrument.instrument_id)
        assert instrument_revision(restored) == legacy["instrument_revision"]
        assert repo.put_instrument(restored) == legacy["instrument_revision"]
        snapshot = repo.get_universe(UUID(legacy["snapshot"]["snapshot_id"]))
        repo.put_universe(snapshot)
        assert (
            repo.connection.scalar(sa.select(sa.func.count()).select_from(s.instrument_versions))
            == 1
        )


def test_ledger_actions_demo_atomic_correction_and_immutable_receipts(database):
    from stock_analyser.ledger import (
        CorporateAction,
        Correction,
        execution_receipt,
        ledger_history,
        portfolio,
        portfolio_revision,
        record_execution,
        record_ledger_event,
    )

    instrument = ledger_instrument(database)
    original = ledger_trade(instrument)
    bought = record_execution(database, original)
    old_revision = portfolio_revision(database)
    split = CorporateAction(
        instrument_id=instrument.instrument_id,
        kind="SPLIT",
        ratio="2",
        effective_at=datetime(2025, 1, 2, tzinfo=UTC),
        reason="Synthetic 2:1 split",
        idempotency_key="split-2025",
    )
    first = record_ledger_event(database, split)
    assert record_ledger_event(database, split) == first
    assert first.portfolio_revision != old_revision
    assert first.position.quantity == 20 and first.position.cost_basis == 1000
    assert first.position.cost_basis / first.position.quantity == 50
    sale = ledger_trade(instrument, "SELL", "8", "60").model_copy(
        update={"executed_at": datetime(2025, 1, 3, tzinfo=UTC)}
    )
    sold = record_execution(database, sale)
    correction = Correction(
        target_execution_id=bought.execution_id,
        reason="Broker fill was 12 shares at 90",
        replacement=original.model_copy(update={"quantity": Decimal("12"), "price": Decimal("90")}),
        idempotency_key="correct-buy",
    )
    corrected = record_ledger_event(database, correction)
    assert record_ledger_event(database, correction) == corrected
    assert corrected.position.quantity == 16
    assert corrected.position.cost_basis == 720
    assert corrected.position.realized_pnl == 120
    assert corrected.position.lots[0].execution_id == bought.execution_id
    assert portfolio_revision(database) == corrected.portfolio_revision
    revision = portfolio_revision(database)
    with pytest.raises(ValueError, match="Insufficient"):
        record_ledger_event(
            database,
            Correction(
                target_execution_id=bought.execution_id,
                reason="Invalid reversal",
                idempotency_key="invalid-reversal",
            ),
        )
    assert portfolio_revision(database) == revision
    assert execution_receipt(database, original.idempotency_key) == bought
    assert execution_receipt(database, sale.idempotency_key) == sold
    assert len(ledger_history(database)) == 4
    # A second correction explicitly links to the prior correction and recomputes all lots.
    reversed_sale = record_ledger_event(
        database,
        Correction(
            target_execution_id=sold.execution_id,
            reason="Trade busted",
            idempotency_key="bust-sale",
        ),
    )
    assert reversed_sale.position.quantity == 24
    correction2 = correction.model_copy(update={"idempotency_key": "correct-buy-again"})
    corrected2 = record_ledger_event(database, correction2)
    assert corrected2.previous_correction_id == corrected.event_id
    assert portfolio(database)[0].quantity == 24


def test_ledger_complex_actions_conflicts_and_later_executions(database):
    from stock_analyser.ledger import (
        CorporateAction,
        Correction,
        portfolio,
        record_execution,
        record_ledger_event,
    )

    instrument = ledger_instrument(database)
    purchase = record_execution(database, ledger_trade(instrument))
    for kind in ("MERGER", "SPIN_OFF", "AMBIGUOUS"):
        receipt = record_ledger_event(
            database,
            CorporateAction(
                instrument_id=instrument.instrument_id,
                kind=kind,
                effective_at=datetime(2025, 1, 2, tzinfo=UTC),
                reason="Needs operator reconciliation",
                idempotency_key=kind,
            ),
        )
        assert receipt.status == "RECONCILIATION_REQUIRED"
    assert portfolio(database)[0].quantity == 10
    assert len(portfolio(database)[0].reconciliation_required) == 3
    correction = Correction(
        target_execution_id=purchase.execution_id, reason="Busted", idempotency_key="reverse"
    )
    receipt = record_ledger_event(database, correction)
    assert receipt.position.quantity == 0 and receipt.position.cost_basis == 0
    with pytest.raises(ValueError, match="Idempotency"):
        record_ledger_event(database, correction.model_copy(update={"reason": "Other"}))
    with pytest.raises(ValueError, match="Insufficient"):
        record_execution(database, ledger_trade(instrument, "SELL", "1"))
    assert portfolio(database)[0].quantity == 0
    restored = record_ledger_event(
        database,
        Correction(
            target_execution_id=purchase.execution_id,
            replacement=purchase.confirmation,
            reason="Restore original",
            idempotency_key="restore",
        ),
    )
    assert restored.previous_correction_id == receipt.event_id
    assert restored.position.quantity == 10


def test_ledger_event_cli_confirmation_and_history(database, monkeypatch, tmp_path):
    from stock_analyser.ledger import CorporateAction, ledger_history, record_execution

    instrument = ledger_instrument(database)
    record_execution(database, ledger_trade(instrument))
    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    action = CorporateAction(
        instrument_id=instrument.instrument_id,
        kind="SPLIT",
        ratio="2",
        effective_at=datetime(2025, 1, 2, tzinfo=UTC),
        reason="Test",
        idempotency_key="cli-split",
    )
    input_path = tmp_path / "action.json"
    input_path.write_text(action.model_dump_json())
    runner = CliRunner()
    result = runner.invoke(
        app, ["record-corporate-action", "--input", str(input_path)], input="n\n"
    )
    assert result.exit_code == 0 and len(ledger_history(database)) == 1
    result = runner.invoke(
        app, ["record-corporate-action", "--input", str(input_path)], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert len(ledger_history(database)) == 2
    result = runner.invoke(app, ["ledger-history"])
    assert result.exit_code == 0 and "cli-split" in result.output


def test_ledger_events_leave_historical_reports_immutable(database, tmp_path, monkeypatch):
    from stock_analyser.ledger import Correction, record_execution, record_ledger_event
    from stock_analyser.reports import ReportService

    output = collect_report(database, tmp_path, monkeypatch)
    run_id = UUID(output["run_id"])
    service = ReportService(database, tmp_path / "reports")
    frozen = service.freeze(run_id)
    files = {
        Path(output[key]): Path(output[key]).read_bytes()
        for key in ("report", "report_json", "manifest")
    }
    instrument = ledger_instrument(database)
    receipt = record_execution(database, ledger_trade(instrument))
    record_ledger_event(
        database,
        Correction(
            target_execution_id=receipt.execution_id, reason="Busted", idempotency_key="bust"
        ),
    )
    assert service.freeze(run_id) == frozen
    assert all(path.read_bytes() == content for path, content in files.items())
    Path(output["report_json"]).unlink()
    service.generate(run_id)
    assert all(path.read_bytes() == content for path, content in files.items())


def test_ledger_invalid_backdated_split_and_corrected_sale_are_atomic(database):
    from stock_analyser.ledger import (
        CorporateAction,
        Correction,
        ledger_history,
        portfolio_revision,
        record_execution,
        record_ledger_event,
    )

    instrument = ledger_instrument(database)
    record_execution(database, ledger_trade(instrument))
    sale = record_execution(
        database,
        ledger_trade(instrument, "SELL", "8").model_copy(
            update={"executed_at": datetime(2025, 1, 3, tzinfo=UTC)}
        ),
    )
    revision = portfolio_revision(database)
    with pytest.raises(ValueError, match="Insufficient"):
        record_ledger_event(
            database,
            CorporateAction(
                instrument_id=instrument.instrument_id,
                kind="SPLIT",
                ratio="0.5",
                effective_at=datetime(2025, 1, 2, tzinfo=UTC),
                reason="Reverse split",
                idempotency_key="invalid-split",
            ),
        )
    with pytest.raises(ValueError, match="Insufficient"):
        record_ledger_event(
            database,
            Correction(
                target_execution_id=sale.execution_id,
                reason="Oversold",
                replacement=sale.confirmation.model_copy(update={"quantity": Decimal("11")}),
                idempotency_key="invalid-sale",
            ),
        )
    assert portfolio_revision(database) == revision
    assert len(ledger_history(database)) == 2


@pytest.mark.parametrize("scenario,count", [("one-holding-failure", 12), ("valid", 0)])
def test_sell_cli_all_open_holdings(database, tmp_path, monkeypatch, scenario, count):
    from stock_analyser.ledger import TradeConfirmation, portfolio_state, record_execution
    from stock_analyser.reports import ReportService
    from stock_analyser.universe_demo import fixture_request

    database.initialize()
    request, _ = fixture_request(tmp_path / "inputs")
    instruments = {r.metadata.instrument.symbol: r.metadata.instrument for r in request.histories}
    selected = [instruments[f"TECH{i:02}"] for i in range(11)] + [instruments["ILLIQUID"]]
    for index, instrument in enumerate(selected[:count]):
        with database.write() as repo:
            repo.put_security(Security(security_id=instrument.security_id, name=instrument.symbol))
            repo.put_instrument(instrument)
        record_execution(
            database,
            TradeConfirmation(
                instrument_id=instrument.instrument_id,
                venue=instrument.venue,
                side="BUY",
                quantity="2",
                price="10",
                currency="USD",
                executed_at=datetime(2026, 10, 1, tzinfo=UTC),
                idempotency_key=f"sell-demo-{index}",
            ),
        )
    if count:
        # Closed and foreign-market positions do not enter the active market scan.
        closed = selected[0].model_copy(
            update={"instrument_id": uuid4(), "security_id": uuid4(), "symbol": "CLOSED"}
        )
        foreign = closed.model_copy(
            update={
                "instrument_id": uuid4(),
                "security_id": uuid4(),
                "market_id": "CA",
                "symbol": "FOREIGN",
                "quote_currency": "CAD",
            }
        )
        for instrument in (closed, foreign):
            with database.write() as repo:
                repo.put_security(
                    Security(security_id=instrument.security_id, name=instrument.symbol)
                )
                repo.put_instrument(instrument)
            trade = TradeConfirmation(
                instrument_id=instrument.instrument_id,
                venue=instrument.venue,
                side="BUY",
                quantity="1",
                price="10",
                currency=instrument.quote_currency,
                executed_at=datetime(2026, 10, 1, tzinfo=UTC),
                idempotency_key=instrument.symbol,
            )
            record_execution(database, trade)
            if instrument == closed:
                record_execution(
                    database,
                    trade.model_copy(
                        update={
                            "side": "SELL",
                            "executed_at": datetime(2026, 10, 2, tzinfo=UTC),
                            "idempotency_key": "close-ETF",
                        }
                    ),
                )
    before = portfolio_state(database)
    monkeypatch.setenv(
        "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
    )
    result = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "scan", "sell", "--fixture", scenario]
    )
    assert result.exit_code == (1 if count else 0), result.output
    assert portfolio_state(database) == before
    with database.read() as repo:
        parent = next(r for r in repo.list_runs(100) if "sell_collection" in r.configuration)
        stages = repo.list_stages(parent.run_id)
    service = ReportService(database, tmp_path / "reports")
    data = json.loads(service.show(parent.run_id, "report.json"))
    markdown = service.show(parent.run_id)
    scope = data["run"]["configuration"]["sell_collection"]
    assert len(data["instruments"]) == len(stages) == len(scope["holdings"]) == count
    assert parent.request.portfolio_revision == before[1]
    assert scope["portfolio_changed"] is False
    assert len([s for s in stages if s.status == OperationalStatus.FAILED]) == (1 if count else 0)
    assert data["report"]["stages"]["verdict"] == "NOT_RUN"
    for holding in scope["holdings"]:
        assert holding["position"]["quantity"] == "2"
        assert holding["position"]["lots"][0]["quantity"] == "2"
        assert holding["entries"][0]["entry_thesis"] is None
        assert holding["entries"][0]["invalidation"] is None
        assert holding["exposure"]["cost_basis"] == "20"
        assert holding["instrument"]["symbol"] in markdown
    assert ("ILLIQUID" in markdown) if count else ("EMPTY_PORTFOLIO" in markdown)
    assert "FOREIGN" not in markdown
    assert "CLOSED (" not in markdown
    service.generate(parent.run_id)
    assert service.show(parent.run_id) == markdown


def test_sell_scan_freezes_revision_and_shared_provider(database, tmp_path, monkeypatch):
    import threading
    import time

    from stock_analyser.config import load_runtime
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.ledger import (
        Correction,
        TradeConfirmation,
        freeze_open_holdings,
        portfolio_state,
        record_execution,
        record_ledger_event,
    )
    from stock_analyser.sell import SellEvidence, collect_sell
    from stock_analyser.universe_demo import fixture_request

    database.initialize()
    request, resolver = fixture_request(tmp_path / "inputs")
    selected = list(
        {
            r.metadata.instrument.instrument_id: r.metadata.instrument for r in request.histories
        }.values()
    )[:3]
    receipts = []
    for index, instrument in enumerate(selected):
        with database.write() as repo:
            repo.put_security(Security(security_id=instrument.security_id, name=instrument.symbol))
            repo.put_instrument(instrument)
        receipts.append(
            record_execution(
                database,
                TradeConfirmation(
                    instrument_id=instrument.instrument_id,
                    venue=instrument.venue,
                    side="BUY",
                    quantity="2",
                    price="10",
                    currency="USD",
                    executed_at=datetime(2026, 10, 1, tzinfo=UTC),
                    idempotency_key=f"freeze-{index}",
                    recommendation_id="entry-link",
                ),
            )
        )
    original = receipts[0]
    corrected = original.confirmation.model_copy(
        update={"price": Decimal("12"), "recommendation_id": "corrected-link"}
    )
    record_ledger_event(
        database,
        Correction(
            target_execution_id=original.execution_id,
            replacement=corrected,
            reason="Correct entry context",
            idempotency_key="context-correction",
        ),
    )
    before = portfolio_state(database)
    frozen, revision = freeze_open_holdings(database, "US")
    assert frozen[0]["entries"][0]["confirmation"]["recommendation_id"] == "corrected-link"
    active = peak = 0
    gate = threading.Lock()
    changed = False

    class ObservedProvider(RawFixtureProvider):
        def collection_identity(self):
            return {"version": "observed-fixture-1", "scenario": "valid"}

        def fetch(self, instrument, market, resolution):
            nonlocal active, peak, changed
            with gate:
                active += 1
                peak = max(peak, active)
            try:
                if not changed:
                    changed = True
                    record_execution(
                        database,
                        original.confirmation.model_copy(
                            update={"idempotency_key": "concurrent-buy", "quantity": Decimal("1")}
                        ),
                    )
                time.sleep(0.01)
                return super().fetch(instrument, market, resolution)
            finally:
                with gate:
                    active -= 1

    runtime = load_runtime(data_dir=tmp_path)
    evidence = SellEvidence(
        market=request.market,
        requested_as_of=request.requested_as_of,
        history=request.policy.requirements(),
        cutoff_policy=request.cutoff_policy,
    )
    run_id, status, _ = collect_sell(
        database, runtime, evidence, ObservedProvider(), history_resolver=resolver
    )
    assert status == OperationalStatus.SUCCEEDED
    assert peak == 1
    with database.read() as repo:
        run = repo.get_run(run_id)
    scope = run.configuration["sell_collection"]
    assert run.request.portfolio_revision == revision == before[1]
    assert scope["portfolio_changed"] is True
    assert scope["holdings"] == frozen
    assert "portfolio changed after selection" in run.reason
    assert portfolio_state(database)[0][0].quantity == Decimal("3")


def test_sell_rejects_unsupported_public_market(database, tmp_path):
    from stock_analyser.config import load_runtime
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.sell import SellEvidence, collect_sell
    from stock_analyser.universe_demo import fixture_request

    request, resolver = fixture_request(tmp_path / "inputs")
    evidence = SellEvidence(
        market=request.market.model_copy(update={"market_id": "CA"}),
        requested_as_of=request.requested_as_of,
        history=request.policy.requirements(),
    )
    with pytest.raises(ValueError, match="Unsupported market"):
        collect_sell(
            database,
            load_runtime(data_dir=tmp_path),
            evidence,
            RawFixtureProvider(),
            history_resolver=resolver,
        )


def test_sell_retains_holding_without_supported_collection_context(database, tmp_path):
    from stock_analyser.config import load_runtime
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.ledger import TradeConfirmation, portfolio_state, record_execution
    from stock_analyser.reports import ReportService
    from stock_analyser.sell import SellEvidence, collect_sell
    from stock_analyser.universe_demo import fixture_request

    database.initialize()
    request, resolver = fixture_request(tmp_path / "inputs")
    instrument = request.histories[0].metadata.instrument.model_copy(
        update={"venue": "OTC", "quote_currency": "CAD", "symbol": "UNSUPPORTED"}
    )
    with database.write() as repo:
        repo.put_security(Security(security_id=instrument.security_id, name=instrument.symbol))
        repo.put_instrument(instrument)
    record_execution(
        database,
        TradeConfirmation(
            instrument_id=instrument.instrument_id,
            venue="OTC",
            side="BUY",
            quantity="1",
            price="10",
            currency="CAD",
            executed_at=datetime(2026, 10, 1, tzinfo=UTC),
            idempotency_key="unsupported-holding",
        ),
    )
    before = portfolio_state(database)
    evidence = SellEvidence(
        market=request.market,
        requested_as_of=request.requested_as_of,
        history=request.policy.requirements(),
        cutoff_policy=request.cutoff_policy,
    )
    run_id, status, _ = collect_sell(
        database,
        load_runtime(data_dir=tmp_path),
        evidence,
        RawFixtureProvider(),
        history_resolver=resolver,
    )
    # No holding has usable collection: this is a total operational failure.
    assert status == OperationalStatus.FAILED
    report = json.loads(ReportService(database, tmp_path / "reports").show(run_id, "report.json"))
    assert report["instruments"][0]["quote_currency"] == "CAD"
    assert report["instruments"][0]["venue"] == "OTC"
    assert report["collection_stages"][0]["status"] == "FAILED"
    assert portfolio_state(database) == before


def test_resume_sell_after_two_instruments_preserves_original_inputs(
    database, tmp_path, monkeypatch
):
    """Restart after cancellation; only the two unfinished holdings make requests."""
    from collections import Counter

    from stock_analyser.config import load_runtime
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.ledger import portfolio_state, record_execution
    from stock_analyser.reports import ReportService
    from stock_analyser.resume import execute_plan
    from stock_analyser.sell import SellEvidence, collect_sell
    from stock_analyser.universe_demo import fixture_request

    database.initialize()
    request, resolver = fixture_request(tmp_path / "inputs", eligible_count=4)
    instruments = list(
        {
            r.metadata.instrument.instrument_id: r.metadata.instrument for r in request.histories
        }.values()
    )[:4]
    for instrument in instruments:
        with database.write() as repo:
            repo.put_security(Security(security_id=instrument.security_id, name=instrument.symbol))
            repo.put_instrument(instrument)
        record_execution(database, ledger_trade(instrument))
    runtime = load_runtime(data_dir=tmp_path)
    runtime = runtime.model_copy(update={"stock_concurrency": 1})
    evidence = SellEvidence(
        market=request.market,
        requested_as_of=request.requested_as_of,
        history=request.policy.requirements(),
        cutoff_policy=request.cutoff_policy,
    )
    original_portfolio = portfolio_state(database)

    def interrupt(message):
        if message.startswith("Collection 2/"):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        collect_sell(
            database,
            runtime,
            evidence,
            RawFixtureProvider(),
            history_resolver=resolver,
            progress=interrupt,
        )
    with database.read() as repo:
        run = next(r for r in repo.list_runs(100) if "resume_plan" in r.configuration)
        stages = repo.list_stages(run.run_id)
        preserved = repo.list_snapshots(run.run_id)
        run_count = len(repo.list_runs(100))
    assert run.status == OperationalStatus.CANCELLED
    assert sum(s.status == OperationalStatus.SUCCEEDED for s in stages) == 2
    assert len(preserved) == 2
    service = ReportService(database, runtime.reports_dir)
    cancelled = service.freeze(run.run_id)
    assert cancelled.report.stages.verdict == OperationalStatus.NOT_RUN
    assert "Unfinished collection: 2" in service.show(run.run_id)
    previous_reports = {
        a.reference: Path(a.reference).read_bytes() for a in service.generate(run.run_id)
    }
    original_bytes = {r.reference: Path(r.reference).read_bytes() for r in preserved}
    assert portfolio_state(database) == original_portfolio

    # The ledger and current instrument projection can change while the app is down.
    # Neither is a new input to this run.
    record_execution(database, ledger_trade(instruments[0], quantity="1"))
    with database.write() as repo:
        repo.put_instrument(instruments[-1].model_copy(update={"symbol": "RENAMED"}))
    after_external_trade = portfolio_state(database)
    calls = Counter()
    observations = []

    original_fetch = RawFixtureProvider.fetch

    def observed_fetch(self, instrument, market, resolution):
        calls[instrument.instrument_id] += 1
        observations.append((instrument, market, resolution))
        return original_fetch(self, instrument, market, resolution)

    monkeypatch.setattr(RawFixtureProvider, "fetch", observed_fetch)

    # A new Database object discards every in-memory collector/provider object.
    restarted = Database(database.engine.url.render_as_string(hide_password=False))
    try:
        result = execute_plan(restarted, runtime, run.run_id)
        assert result[0] == run.run_id and result[1] == OperationalStatus.SUCCEEDED
        with restarted.read() as repo:
            resumed = repo.get_run(run.run_id)
            snapshots = repo.list_snapshots(run.run_id)
            assert len(repo.list_runs(100)) == run_count
        assert len(calls) == 2 and sum(calls.values()) == 2
        assert not set(calls) & {r.metadata.instrument.instrument_id for r in preserved}
        assert len(snapshots) == 4
        assert resumed.request == run.request
        assert resumed.source_version == run.source_version
        assert resumed.component_versions == run.component_versions
        assert resumed.configuration["resume_plan"] == run.configuration["resume_plan"]
        assert (
            resumed.configuration["sell_collection"]["holdings"]
            == run.configuration["sell_collection"]["holdings"]
        )
        assert resumed.request.portfolio_revision == original_portfolio[1]
        assert resumed.configuration["sell_collection"]["portfolio_changed"] is True
        for instrument, market, resolution in observations:
            assert instrument in instruments and market == evidence.market
            assert resolution.requested_as_of == evidence.requested_as_of
            assert resolution.policy == evidence.cutoff_policy
        assert {r.reference: Path(r.reference).read_bytes() for r in preserved} == original_bytes
        assert {path: Path(path).read_bytes() for path in previous_reports} == previous_reports
        assert portfolio_state(restarted) == after_external_trade
        completed_bytes = service.show(run.run_id)
        completed_artifacts = service.generate(run.run_id)
        # Completed resume is regeneration: the CLI needs no provider.
        monkeypatch.setenv(
            "STOCK_ANALYSER_DATABASE_URL", database.engine.url.render_as_string(hide_password=False)
        )
        result = CliRunner().invoke(
            app, ["--data-dir", str(tmp_path), "scan", "resume", str(run.run_id)]
        )
        assert result.exit_code == 0, result.output
        assert service.show(run.run_id) == completed_bytes
        assert service.generate(run.run_id) == completed_artifacts
        assert portfolio_state(restarted) == after_external_trade
        with restarted.read() as repo:
            assert len(repo.list_runs(100)) == run_count
    finally:
        restarted.close()


def test_resume_incomplete_history_budget_and_policy_identity(database, tmp_path):
    from stock_analyser.collection import CollectionService
    from stock_analyser.contracts import AnalysisRequest, HistoryRequirement
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.universe_demo import fixture_request

    database.initialize()
    request, resolver = fixture_request(tmp_path / "inputs", eligible_count=1)
    instrument = request.histories[0].metadata.instrument
    requirements = (
        HistoryRequirement(timeframe="daily", minimum_bars=3),
        HistoryRequirement(timeframe="weekly", minimum_bars=2),
    )
    plans = tuple(
        resolver.resolve(request.market, instrument.venue, h, request.requested_as_of)
        for h in requirements
    )
    run = RunRecord(
        run_id=uuid4(),
        request=AnalysisRequest(
            request_id=uuid4(),
            mode="buy",
            market_id="US",
            instrument_ids=(instrument.instrument_id,),
            requested_as_of=request.requested_as_of,
            strategy_version="collection-only-1",
            history=requirements,
        ),
        market=request.market,
        configuration={},
        source_version={"original": "commit"},
        component_versions={"collection": "1"},
        history_resolutions=plans,
    )
    with database.write() as repo:
        repo.put_security(Security(security_id=instrument.security_id, name=instrument.symbol))
        repo.put_instrument(instrument)
        repo.create_run(run)
    calls = []

    class InterruptedProvider(RawFixtureProvider):
        def fetch(self, instrument, market, resolution):
            calls.append(resolution.requirement.timeframe)
            if resolution.requirement.timeframe == "weekly":
                raise KeyboardInterrupt
            return super().fetch(instrument, market, resolution)

    collector = CollectionService(database, tmp_path / "snapshots")
    with pytest.raises(KeyboardInterrupt):
        collector.collect(run.run_id, instrument, InterruptedProvider())
    with database.read() as repo:
        snapshot = repo.list_snapshots(run.run_id)[0]
        assert repo.get_run(run.run_id).status == OperationalStatus.CANCELLED
        assert repo.list_stages(run.run_id)[0].attempts == {"daily": 1, "weekly": 1}
    with pytest.raises(ValueError, match="new run"):
        collector.collect(
            run.run_id, instrument, RawFixtureProvider(), cache_mode="refresh", resume=True
        )
    with pytest.raises(ValueError, match="new run"):
        collector.collect(
            run.run_id,
            instrument.model_copy(update={"symbol": "CHANGED"}),
            RawFixtureProvider(),
            resume=True,
        )
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            collector.collect(run.run_id, instrument, InterruptedProvider(), resume=True)
    with pytest.raises(ValueError, match="retry budget"):
        collector.collect(run.run_id, instrument, InterruptedProvider(), resume=True)
    assert calls == ["daily", "weekly", "weekly", "weekly"]
    with database.read() as repo:
        assert repo.list_snapshots(run.run_id) == (snapshot,)
        assert repo.get_run(run.run_id).request == run.request
        assert repo.get_run(run.run_id).status == OperationalStatus.FAILED
        assert repo.list_stages(run.run_id)[0].attempts == {"daily": 1, "weekly": 3}


def test_resume_reuses_completed_timeframe_and_excludes_parallel_collectors(
    database, tmp_path, monkeypatch
):
    from stock_analyser.collection import CollectionService
    from stock_analyser.fixtures import RawFixtureProvider

    output = collect_report(database, tmp_path, monkeypatch)
    run_id = UUID(output["run_id"])
    with database.read() as repo:
        run = repo.get_run(run_id)
        stage = repo.list_stages(run_id)[0]
        snapshots = repo.list_snapshots(run_id)
    collector = CollectionService(database, tmp_path / "snapshots")

    class NeverFetch:
        def fetch(self, *args):
            raise AssertionError("Completed evidence must not be recollected")

    with database.collection_lock(run_id):
        with pytest.raises(ValueError, match="already active"):
            collector.collect(run_id, stage.frozen_instrument, RawFixtureProvider(), resume=True)
    collector.collect(run_id, stage.frozen_instrument, NeverFetch(), resume=True)
    with database.read() as repo:
        assert repo.get_run(run_id) == run
        assert repo.list_snapshots(run_id) == snapshots


def test_resume_buy_frozen_evidence_and_new_policy_create_new_runs(database, tmp_path):
    from stock_analyser.buy import FrozenPriceProvider, collect_buy
    from stock_analyser.config import load_runtime
    from stock_analyser.resume import execute_plan
    from stock_analyser.universe import SelectionPolicy, select_universe
    from stock_analyser.universe_demo import fixture_request

    database.initialize()
    request, resolver = fixture_request(tmp_path / "original", eligible_count=4)
    snapshot = select_universe(request, resolver)
    runtime = load_runtime(data_dir=tmp_path).model_copy(update={"stock_concurrency": 1})

    def interrupt(message):
        if message.startswith("Collection 2/"):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        collect_buy(
            database,
            runtime,
            snapshot,
            FrozenPriceProvider(request.histories),
            history_resolver=resolver,
            progress=interrupt,
        )
    with database.read() as repo:
        parent = next(r for r in repo.list_runs(100) if "resume_plan" in r.configuration)
        old_refs = repo.list_snapshots(parent.run_id)
        original_children = parent.configuration["buy_collection"]["child_run_ids"]
    # Replacement histories or a different adapter must be rejected before any
    # report retirement, attempt increment, evidence attachment, or status write.
    with database.read() as repo:
        before_runs = repo.list_runs(100)
        before_state = tuple(
            (
                r.run_id,
                repo.list_stages(r.run_id),
                repo.list_snapshots(r.run_id),
                repo.list_artifacts(r.run_id),
            )
            for r in before_runs
        )
        before_report = repo.get_report_input(parent.run_id)
    before_files = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    from stock_analyser.fixtures import RawFixtureProvider

    for replacement in (FrozenPriceProvider(request.histories[1:]), RawFixtureProvider()):
        with pytest.raises(ValueError, match="requires a new run"):
            execute_plan(database, runtime, parent.run_id, replacement)
    with database.read() as repo:
        assert repo.list_runs(100) == before_runs
        assert (
            tuple(
                (
                    r.run_id,
                    repo.list_stages(r.run_id),
                    repo.list_snapshots(r.run_id),
                    repo.list_artifacts(r.run_id),
                )
                for r in before_runs
            )
            == before_state
        )
        assert repo.get_report_input(parent.run_id) == before_report
    assert {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before_files
    # No input filename, universe re-selection, calendar re-resolution or provider
    # construction is needed after restart: only the persisted plan is read.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "stock_analyser",
            "--data-dir",
            str(tmp_path),
            "scan",
            "resume",
            str(parent.run_id),
        ],
        env={
            **os.environ,
            "STOCK_ANALYSER_DATABASE_URL": database.engine.url.render_as_string(
                hide_password=False
            ),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    # The synthetic benchmark has no frozen input and is honestly FAILED. All
    # selected stocks are complete and do not become strategy recommendations.
    assert result.returncode == 1, result.stdout + result.stderr
    with database.read() as repo:
        resumed = repo.get_run(parent.run_id)
        stages = repo.list_stages(parent.run_id)
        refs = repo.list_snapshots(parent.run_id)
    assert resumed.status == OperationalStatus.PARTIAL
    assert all(s.status == OperationalStatus.SUCCEEDED for s in stages)
    assert len(refs) == 4
    assert all(r in refs for r in old_refs)
    assert resumed.request == parent.request
    assert resumed.configuration["buy_collection"]["child_run_ids"] == original_children
    assert resumed.configuration["resume_plan"] == parent.configuration["resume_plan"]
    # A changed selection policy starts a separate scan rather than editing an
    # interrupted/completed snapshot in place.
    new_request = request.model_copy(update={"policy": SelectionPolicy(minimum_price_usd=100)})
    new_snapshot = select_universe(new_request, resolver)
    fresh_id, _, _ = collect_buy(
        database,
        runtime,
        new_snapshot,
        FrozenPriceProvider(new_request.histories),
        history_resolver=resolver,
    )
    assert fresh_id != parent.run_id
    with database.read() as repo:
        assert repo.get_run(parent.run_id) == resumed
        assert repo.list_snapshots(parent.run_id) == refs
        assert (
            repo.get_run(fresh_id).configuration["buy_collection"]["policy_version"]
            == new_request.policy.version
        )
    # An exhausted benchmark cannot be retried indefinitely across invocations.
    execute_plan(database, runtime, parent.run_id)
    execute_plan(database, runtime, parent.run_id)
    with database.read() as repo:
        context_id = UUID(parent.configuration["buy_collection"]["shared_context"][0]["run_id"])
        context_stage = repo.list_stages(context_id)[0]
        assert context_stage.attempts == {"daily": 3}
        assert "budget exhausted" in context_stage.reason


def test_resume_external_provider_requires_original_configuration(database, tmp_path):
    from stock_analyser.buy import collect_buy
    from stock_analyser.config import load_runtime
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.resume import execute_plan, provider_recipe
    from stock_analyser.universe import select_universe
    from stock_analyser.universe_demo import fixture_request

    class ExternalProvider(RawFixtureProvider):
        def __init__(self, revision):
            super().__init__()
            self.revision = revision

        def collection_identity(self):
            return {"adapter_version": "1", "data_revision": self.revision, "scenario": "valid"}

    with pytest.raises(ValueError, match="collection_identity"):
        provider_recipe(object())
    database.initialize()
    request, resolver = fixture_request(tmp_path / "inputs", eligible_count=1)
    runtime = load_runtime(data_dir=tmp_path).model_copy(update={"stock_concurrency": 1})

    def interrupt(message):
        if message.startswith("Collection 1/"):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        collect_buy(
            database,
            runtime,
            select_universe(request, resolver),
            ExternalProvider("v1"),
            history_resolver=resolver,
            progress=interrupt,
        )
    with database.read() as repo:
        run = next(r for r in repo.list_runs(100) if "resume_plan" in r.configuration)
        artifacts = repo.list_artifacts(run.run_id)
        stages = repo.list_stages(run.run_id)
        snapshots = repo.list_snapshots(run.run_id)
        report = repo.get_report_input(run.run_id)
    assert run.configuration["resume_plan"]["provider"]["configuration"]["data_revision"] == "v1"
    for provider in (ExternalProvider("v2"), None):
        with pytest.raises(ValueError, match="new run|original identified"):
            execute_plan(database, runtime, run.run_id, provider)
    with database.read() as repo:
        assert repo.get_run(run.run_id) == run
        assert repo.list_artifacts(run.run_id) == artifacts
        assert repo.list_stages(run.run_id) == stages
        assert repo.list_snapshots(run.run_id) == snapshots
        assert repo.get_report_input(run.run_id) == report
    _, status, _ = execute_plan(database, runtime, run.run_id, ExternalProvider("v1"))
    assert status == OperationalStatus.SUCCEEDED
