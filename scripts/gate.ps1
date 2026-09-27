# Regression gate: lint + format check + full non-live test suite with coverage floor.
# Usage: .\scripts\gate.ps1 [-Floor 60]
param([int]$Floor = 60)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

function Step($name, [scriptblock]$cmd) {
    Write-Host "== $name =="
    & $cmd
    if ($LASTEXITCODE -ne 0) { Write-Host "GATE FAILED at: $name" -ForegroundColor Red; exit 1 }
}

Step "ruff check"          { docker compose run --rm -T api ruff check . }
Step "ruff format --check" { docker compose run --rm -T api ruff format --check . }
Step "pytest (not live)"   { docker compose run --rm -T api pytest -m "not live" -q --cov=app --cov-report=term-missing:skip-covered --cov-fail-under=$Floor }
Write-Host "GATE PASSED (coverage floor $Floor%)" -ForegroundColor Green
