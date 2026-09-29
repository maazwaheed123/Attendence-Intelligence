"""Alembic environment. Migrations run as the schema owner, never as a runtime role."""

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings


def _url() -> str:
    return (
        context.config.attributes.get("url") or get_settings().database_url_owner.get_secret_value()
    )


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=None, transaction_per_migration=True
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
