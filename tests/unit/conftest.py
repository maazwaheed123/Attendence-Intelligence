import json
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[2] / "data"


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
