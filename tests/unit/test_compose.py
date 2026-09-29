"""docker-compose wiring that the code relies on."""

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

COMPOSE = yaml.safe_load((Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text())


def test_worker_runs_rq_scheduler():
    assert "--with-scheduler" in COMPOSE["services"]["worker"]["command"]


def test_ui_talks_to_api_with_long_timeout():
    env = COMPOSE["services"]["ui"]["environment"]
    assert env["API_URL"] == "http://api:8000" and int(env["UI_HTTP_TIMEOUT_S"]) >= 300
