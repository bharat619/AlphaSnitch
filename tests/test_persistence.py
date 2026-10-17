"""Real PostgreSQL checks use disposable databases, never application records."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy.engine import make_url
from typer.testing import CliRunner

from stock_analyser.cli import app
from stock_analyser.contracts import OperationalStatus, Security
from stock_analyser.persistence import Database
from stock_analyser.persistence import schema as s
from stock_analyser.persistence.database import WRITE_LOCK, postgres_url
from stock_analyser.persistence.demo import create_fixture_run
from stock_analyser.persistence.records import ArtifactRecord, RunRecord, SnapshotReference


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
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0001"
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(s.market_configurations)) == 1
        )
        assert compare_metadata(MigrationContext.configure(connection), s.metadata) == []
    cfg = Config()
    from pathlib import Path

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
