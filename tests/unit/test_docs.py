"""Submission docs stay in sync with the code."""

import json
import re
from pathlib import Path

import pytest

from scripts import export_openapi

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")


def test_committed_openapi_matches_the_app():
    committed = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    assert committed["paths"].keys() == export_openapi.spec()["paths"].keys(), (
        "run: python -m scripts.export_openapi"
    )


def test_readme_documents_every_env_var():
    keys = re.findall(r"^([A-Z_]+)=", (ROOT / ".env.example").read_text(), flags=re.M)
    missing = [k for k in keys if k not in README]
    assert not missing, missing


def test_readme_local_links_exist():
    links = re.findall(r"\]\(((?:docs|data)/[^)#]+)\)", README)
    assert links
    for link in links:
        assert (ROOT / link).exists(), link


@pytest.mark.parametrize(
    "name", ["architecture.md", "architecture.png", "DECISIONS.md", "WALKTHROUGH.md",
             "api_examples.http", "TEST_SUMMARY.md", "openapi.json"],
)  # fmt: skip
def test_docs_present(name):
    assert (ROOT / "docs" / name).stat().st_size > 500
