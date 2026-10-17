"""Migrations share the application's locked transaction and never log connection URLs."""

from alembic import context

from stock_analyser.persistence.schema import metadata

connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=metadata)
with context.begin_transaction():
    context.run_migrations()
