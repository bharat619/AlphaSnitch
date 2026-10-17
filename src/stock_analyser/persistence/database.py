"""Short transaction boundary and PostgreSQL application write coordinator."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# Shared by all Database instances/processes connected to this database.
WRITE_LOCK = 0x414C504841


def postgres_url(value: str):
    try:
        url = make_url(value)
        if url.drivername not in {"postgresql", "postgresql+psycopg"} or not url.database:
            raise ValueError
        return url.set(drivername="postgresql+psycopg")
    except Exception:
        raise ValueError("Database URL must identify a PostgreSQL database.") from None


class Database:
    def __init__(self, url: str):
        self.engine = create_engine(
            postgres_url(url),
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"connect_timeout": 5},
        )

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def write(self) -> Iterator:
        """Serialize writes across processes; keep provider and filesystem I/O outside."""
        from stock_analyser.persistence.repositories import Repositories

        with self.engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": WRITE_LOCK})
            yield Repositories(connection)

    @contextmanager
    def read(self) -> Iterator:
        from stock_analyser.persistence.repositories import Repositories

        with self.engine.connect() as connection:
            yield Repositories(connection)

    def initialize(self) -> None:
        from stock_analyser.markets import US_MARKET

        # Lock before Alembic inspects the version table, also covering initial seed data.
        with self.write() as repositories:
            cfg = Config()
            cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
            cfg.attributes["connection"] = repositories.connection
            command.upgrade(cfg, "head")
            repositories.put_market(US_MARKET)
