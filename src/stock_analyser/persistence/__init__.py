"""PostgreSQL application state; no connection is opened on import."""

from stock_analyser.persistence.database import Database

__all__ = ["Database"]
