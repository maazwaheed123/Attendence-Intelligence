#!/usr/bin/env bash
# Regression gate: lint + format check + full non-live test suite with coverage floor.
# Usage: scripts/gate.sh [coverage_floor]
set -euo pipefail
FLOOR="${1:-60}"
cd "$(dirname "$0")/.."

echo "== ruff check ==";         docker compose run --rm -T api ruff check .
echo "== ruff format --check =="; docker compose run --rm -T api ruff format --check .
echo "== pytest (not live) ==";  docker compose run --rm -T api \
  pytest -m "not live" -q --cov=app --cov-report=term-missing:skip-covered --cov-fail-under="$FLOOR"
echo "GATE PASSED (coverage floor ${FLOOR}%)"
