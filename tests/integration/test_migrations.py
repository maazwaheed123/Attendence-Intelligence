import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app.db.models import Base
from app.db.session import get_engine, owner_session
from app.security.crypto import decrypt
from scripts.seed import seed
from tests.support.db import alembic_config, reset_corpus

pytestmark = pytest.mark.integration


def test_upgrade_downgrade_upgrade(migrated_db):
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    with owner_session() as s:
        assert s.execute(text("SELECT to_regclass('attendance_records')")).scalar() is None
    command.upgrade(cfg, "head")
    head = ScriptDirectory.from_config(cfg).get_current_head()
    with owner_session() as s:
        assert s.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == head
    reset_corpus()


def test_models_match_database(corpus_db):
    """ORM models must not drift from the migrations."""
    insp = inspect(get_engine("owner"))
    for table in Base.metadata.sorted_tables:
        db_cols = {c["name"] for c in insp.get_columns(table.name)}
        model_cols = {c.name for c in table.columns}
        assert db_cols == model_cols, (
            f"{table.name}: db-only={db_cols - model_cols} model-only={model_cols - db_cols}"
        )


def test_seed_is_idempotent_and_encrypts_pii(corpus_db):
    with owner_session() as s:
        seed(s)
        seed(s)
        n = s.execute(text("SELECT count(*) FROM employees")).scalar_one()
        phone_enc = s.execute(
            text(
                "SELECT phone_enc FROM employees WHERE employee_id='E001' AND product_id='attendance_ai'"
            )
        ).scalar_one()
    assert n == 18 + 2
    assert "555" not in phone_enc
    assert decrypt(phone_enc).startswith("+1-555-01")
