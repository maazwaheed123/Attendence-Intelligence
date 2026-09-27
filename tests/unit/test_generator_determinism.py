"""The generator must be byte-for-byte reproducible, and the committed data must be current."""

import pytest

from scripts.datagen.util import sha256_file
from scripts.generate_data import CORPUS_ORDER, generate

pytestmark = pytest.mark.unit


def _checksums(root):
    files = {f"generated/{n}": sha256_file(root / "generated" / n) for n in CORPUS_ORDER}
    for n in ("ground_truth.json", "manifests.json", "expected_results.json"):
        files[f"ground_truth/{n}"] = sha256_file(root / "ground_truth" / n)
    return files


def test_same_seed_identical_bytes(regenerated, tmp_path):
    generate(tmp_path)
    assert _checksums(regenerated) == _checksums(tmp_path)


def test_committed_data_matches_generator(regenerated, data_dir):
    """Fails if seed_spec/generator changed without re-running the generator."""
    fresh, committed = _checksums(regenerated), _checksums(data_dir)
    stale = sorted(k for k in fresh if fresh[k] != committed[k])
    assert not stale, f"regenerate data (python -m scripts.generate_data); stale: {stale}"


def test_manifest_checksums_match_files(manifests, data_dir):
    for name, m in manifests["files"].items():
        assert sha256_file(data_dir / "generated" / name) == m["sha256"], name
