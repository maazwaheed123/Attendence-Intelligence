# Setup guide: from `git clone` to a running system

Follow the steps in order. Everything runs locally in Docker, plus Ollama for the models.
No API keys or cloud accounts are needed.

- **Time:** about 20-40 minutes the first time, mostly downloads (Docker images ~3 GB,
  models ~9 GB).
- **Commands:** each step shows **bash** (macOS, Linux, Git Bash on Windows) and, where they
  differ, **PowerShell** (Windows).

---

## 1. Prerequisites

| Need | Version / note |
|---|---|
| Docker Desktop (or Docker Engine + Compose v2) | running; ~4 GB RAM for Docker is enough (models run outside Docker) |
| [Ollama](https://ollama.com/download) | installed and running on the host machine |
| Git | any recent version |
| RAM / disk | 16 GB RAM recommended; ~20 GB free disk |
| Free ports | **8000** (API), **8501** (UI), **5433** (Postgres), **6380** (Redis), **11434** (Ollama) |

Check them:

```bash
docker --version
docker compose version
ollama --version
```

**Linux only:** Ollama listens on `127.0.0.1` by default, so containers cannot reach it.
Make it listen on all interfaces:

```bash
sudo systemctl edit ollama
# add:   [Service]
#        Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl restart ollama
```

Also on Linux, the containers run as uid 1000 and write to `data/uploads` and
`data/exports` inside the clone. If your user is not uid 1000, run
`mkdir -p data/uploads data/exports && chmod 777 data/uploads data/exports`.

---

## 2. Pull the models

```bash
ollama pull qwen2.5:7b-instruct      # main answer model (~4.7 GB)
ollama pull qwen2.5:3b-instruct      # fallback model (~1.9 GB)
ollama pull nomic-embed-text         # embeddings (~0.3 GB)
ollama pull qwen2.5vl:3b             # handwriting OCR (~3.2 GB), optional
```

Without `qwen2.5vl:3b` everything still works, but handwritten sheets are read by
Tesseract only and flagged for review.

Check: `ollama list` shows the models.

---

## 3. Clone the repository

```bash
git clone https://github.com/maazwaheed123/Attendence-Intelligence.git attendance-intelligence
cd attendance-intelligence
```

All later commands run from this folder.

---

## 4. Create the `.env` file

```bash
cp .env.example .env
```

PowerShell:

```powershell
Copy-Item .env.example .env
```

The defaults in `.env.example` work for a local run. Only **one** value is required:
`PII_ENCRYPTION_KEY` (used to encrypt employee phone numbers, e-mails and IDs). Seeding
fails without it.

### 4a. Build the images

Building first lets you use the image's Python to generate the key. The first build takes
about 5-15 minutes.

```bash
docker compose build
```

### 4b. Generate the key

```bash
docker compose run --rm --no-deps api python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy the printed value (it ends with `=`) into `.env`:

```
PII_ENCRYPTION_KEY=<the printed key>
```

### 4c. Recommended: change the other secrets

These are optional for a local run.

- `JWT_SECRET`: any long random string.
- `POSTGRES_PASSWORD`: if you change it, change the password in `DATABASE_URL_OWNER` to the
  same value.
- The `app_rw` / `rag_reader` passwords in `DATABASE_URL_APP` / `DATABASE_URL_READER` can be
  anything. The migration creates those database roles with exactly these passwords.

Keep `APP_ENV=dev`. It enables the demo login endpoint (`/v1/auth/dev-token`), which the UI
and the demo script use; it is never mounted with `APP_ENV=prod`.

> Changed `.env` later? Apply it with `docker compose up -d --force-recreate api worker`.

---

## 5. Start the services

```bash
docker compose up -d
docker compose ps
```

Wait until `postgres` and `redis` show `healthy` and `api` shows `running` or `healthy`
(about 30 s). On the very first start Postgres also creates the test database
(`attendance_test`) and the `vector` / `pg_trgm` extensions.

Check:

```bash
curl -s http://localhost:8000/v1/health
```

PowerShell:

```powershell
Invoke-RestMethod http://localhost:8000/v1/health
```

Expected: `{"status":"ok", ...}`.

---

## 6. Create the database schema and reference data

```bash
docker compose run --rm -T api alembic upgrade head
docker compose run --rm -T api python -m scripts.seed
```

- `alembic upgrade head` creates the tables, least-privilege roles, Row-Level Security
  policies and the `v_attendance` view.
- `scripts.seed` loads the two demo tenants, their entities (departments) and the employee
  roster, with PII encrypted.

---

## 7. Ingest the sample data and run the end-to-end demo

```bash
docker compose run --rm -T api python -m scripts.demo --full
```

This uploads all 15 sample files in `data/generated/` through the API, the same way a
user would. The worker parses, OCRs, normalizes and embeds them. Two files (`corrupt.xlsx`,
`empty.csv`) are **meant** to be rejected at upload, with a reason.

The script then checks the whole flow:

- an answer with citations and confidence
- controlled "unavailable" answers
- blocked cross-tenant and cross-department requests
- JSON / XLSX / PDF exports with the same checksum
- the feedback / training loop with a repeat query
- rollback

It ends with `N/N checks passed` and exit code 0.

Expect **5-10 minutes**: embedding the sample data takes a couple of minutes, and each
answer that calls the local model takes ~30-90 s on CPU (the first one, while the model
loads, up to ~3 min). Refusals and denials are instant.

Exported files are written to `data/exports/`.

Run it again at any time without `--full`; re-uploads would only be reported as duplicates.

---

## 8. Use the application

| What | Where |
|---|---|
| Web UI (Streamlit) | http://localhost:8501 |
| API + Swagger UI | http://localhost:8000/docs |
| Deep health check | http://localhost:8000/v1/health/deep |
| Request examples | `docs/api_examples.http` |

**In the UI:**

1. Pick a persona in the sidebar. The sidebar shows its tenant, role, departments and
   clearance.
2. Try each page:
   - **Ask:** e.g. "Who was present on 1 September 2026?"
   - **Upload:** hr_admin / manager only
   - **Feedback:** reviewer / hr_admin only
   - **Export:** hr_admin / manager only
   - **Health**

**From the command line (bash):**

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/v1/auth/dev-token -H "Content-Type: application/json" -d '{"persona":"a_eng_manager"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

curl -s -m 300 -X POST http://localhost:8000/v1/query -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"question":"Who was present on 1 September 2026?"}'
```

**PowerShell:**

```powershell
$t = (Invoke-RestMethod -Method Post http://localhost:8000/v1/auth/dev-token -ContentType "application/json" -Body '{"persona":"a_eng_manager"}').access_token

Invoke-RestMethod -Method Post http://localhost:8000/v1/query -TimeoutSec 300 -Headers @{Authorization="Bearer $t"} -ContentType "application/json" -Body '{"question":"Who was present on 1 September 2026?"}'
```

**Personas:** `a_hr_admin`, `a_eng_manager`, `a_hr_manager`, `a_employee_e001`,
`a_reviewer`, `a_auditor`, `b_manager`, `b_reviewer`, `x_other_product`. The README
explains what each one may see; `docs/WALKTHROUGH.md` has a guided tour with good
questions to ask.

---

## 9. Run the tests (optional)

```bash
bash scripts/gate.sh 80
```

PowerShell:

```powershell
.\scripts\gate.ps1 -Floor 80
```

- **What it runs:** lint, a format check and the full automated suite, with a coverage
  floor of 80%.
- **Time:** ~5 minutes.
- **Isolation:** the tests use the separate `attendance_test` database and mock models, so
  they do not touch your data and do not need Ollama.
- **Expected result:** `GATE PASSED`.

Live evaluation against the real models (~15 minutes) and regenerating the test summary:

```bash
docker compose run --rm -T api pytest -m live -s -q tests/live/test_eval_questions.py
docker compose run --rm -T api python -m scripts.eval_report
```

---

## 10. Stop, restart, reset

```bash
docker compose stop          # stop (data is kept)
docker compose start         # start again (no need to repeat steps 6-7)
docker compose down          # remove containers (data is kept in the pgdata volume)
docker compose down -v       # remove containers AND all data -> redo steps 5-7
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `port is already allocated` on `docker compose up` | Another program uses 8000/8501/5433/6380. Stop it, or change the left-hand port in `docker-compose.yml` (e.g. `"5434:5432"`). |
| `RuntimeError: PII_ENCRYPTION_KEY is not configured` during seed | Step 4b was skipped. Set the key in `.env`, then `docker compose up -d --force-recreate api worker` and seed again. |
| `password authentication failed` | `POSTGRES_PASSWORD` differs from the password in `DATABASE_URL_OWNER`, or you changed it after the first start (the volume keeps the old one). Fix `.env`, or reset with `docker compose down -v`. |
| `/v1/health/deep` shows `llm_providers` / `embeddings` **down**, status **degraded** | Ollama is not running, a model is not pulled, or (Linux) Ollama is not listening on `0.0.0.0` (see step 1). Answers still work through the deterministic template engine. |
| Answers are slow or time out | Normal on CPU: 30-90 s per model call, up to ~3 min for the first call after a model loads. Use `-m 300` with curl. Repeated questions are answered from the cache. |
| Jobs stay `index_pending` or answers find no documents | Ollama was unavailable during ingestion. Once it is running: `docker compose run --rm -T api python -m scripts.reindex`. |
| `corrupt.xlsx` / `empty.csv` rejected (`UNSUPPORTED_FILE` / `VALIDATION_ERROR`) | Expected: these are the deliberately broken sample files. |
| Handwriting rows show "awaiting review" | Expected without `qwen2.5vl:3b`; uncertain OCR values are never stored as facts. |
| Changed Python code in `app/ingestion` has no effect | Restart the worker: `docker compose restart worker` (the API reloads by itself). |
| `docker compose run` seems to hang after finishing (Windows) | Press Ctrl+C; the command has completed. Adding `-T` (as in the commands above) avoids most cases. |

For how the system is built and why, see `README.md`, `docs/architecture.md` and
`docs/DECISIONS.md`.
