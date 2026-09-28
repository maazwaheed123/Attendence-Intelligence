import json
import os
import re
from pathlib import Path

# --- Point every test at the dedicated test database BEFORE app settings load. ---
for _var in ("DATABASE_URL_OWNER", "DATABASE_URL_APP", "DATABASE_URL_READER"):
    if os.environ.get(_var):
        os.environ[_var] = re.sub(r"/attendance$", "/attendance_test", os.environ[_var])
os.environ["APP_ENV"] = "test"
os.environ["INGEST_SYNC"] = "true"
os.environ["LLM_CHAIN"] = "mock,template"  # the gate never calls a real model
os.environ["VISION_CHAIN"] = "mock"
os.environ["UPLOAD_DIR"] = "/tmp/attendance_test_uploads"
# Query suites send many requests per persona; test_rate_limit sets its own limit.
os.environ["RATE_LIMIT_PER_MIN"] = "100000"
os.environ["REDIS_URL"] = re.sub(
    r"/\d+$", "/15", os.environ.get("REDIS_URL", "redis://redis:6379/0")
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()
assert get_settings().database_url_owner.get_secret_value().endswith("/attendance_test")

from app.main import create_app  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.fixture(scope="session")
def migrated_db():
    """Fresh schema for the session: downgrade to base, then upgrade to head."""
    from tests.support.db import migrate_fresh

    migrate_fresh()
    yield
    from app.db.session import dispose_engines

    dispose_engines()


@pytest.fixture
def corpus_db(migrated_db):
    """Seeded roster + every sample file's rows loaded as canonical records.

    Loaded straight from the generator manifests (no ingestion code involved), so
    DB-layer tests (RLS, view metrics) are independent of the ingestion pipeline.
    Reloaded lazily only if an ingestion test emptied the database.
    """
    from tests.support.db import STATE, reset_corpus

    if not STATE["corpus_loaded"]:
        reset_corpus()


@pytest.fixture
def clean_db(migrated_db):
    """Empty database with reference data (tenants, entities, roster) only."""
    from tests.support.db import reset_empty

    reset_empty()


@pytest.fixture
def ingest_api(clean_db, client):
    return client


@pytest.fixture
def token_for():
    """token_for("a_eng_manager", role="auditor") -> signed JWT for that persona (+ overrides)."""
    from tests.support.tokens import token_for as _token_for

    return _token_for


@pytest.fixture
def auth(token_for):
    """auth("a_eng_manager") -> Authorization header dict."""
    return lambda persona, **kw: {"Authorization": f"Bearer {token_for(persona, **kw)}"}


@pytest.fixture
def api(corpus_db, client):
    """TestClient against a seeded database (entities/tenants exist for gateway checks)."""
    return client


@pytest.fixture
def scope_for():
    from tests.support.db import persona_scope

    return persona_scope


# --------------------------------------------------------------- sample-data fixtures
DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.fixture(scope="session")
def data_dir() -> Path:
    return DATA


@pytest.fixture(scope="session")
def manifests() -> dict:
    return json.loads((DATA / "ground_truth" / "manifests.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def truth() -> dict:
    return json.loads((DATA / "ground_truth" / "ground_truth.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def expected() -> dict:
    return json.loads((DATA / "ground_truth" / "expected_results.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def regenerated(tmp_path_factory) -> Path:
    """Run the generator into a temp root once per session."""
    from scripts.generate_data import generate

    root = tmp_path_factory.mktemp("regen")
    generate(root)
    return root
