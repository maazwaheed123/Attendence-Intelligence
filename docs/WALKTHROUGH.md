# Walkthrough (5-8 minute demo script)

Before recording: `docker compose up -d`, Ollama running on the host with the models from
the README, corpus ingested (`python -m scripts.demo --full` once). Answers that call a
model take ~30-90 s on CPU, so either pre-warm (ask Q1 once) or cut the waits.

| # | Time | Show | Say |
|---|---|---|---|
| 1 | 0:00 | `docs/architecture.png` | Layers as in the reference figure; the red box is the isolation boundary: Postgres RLS on every table, every read through `scoped_session`. Local only: Ollama qwen2.5 7b -> 3b -> template. |
| 2 | 0:45 | UI (http://localhost:8501) -> Health | All components; models down = degraded, not down. |
| 3 | 1:00 | Upload page as `a_hr_admin`: upload `tenant_a_sep_v2.csv`, `tenant_a_sales_sep.xlsx`, `tenant_a_week2.docx`, `scan_printed.png`, `handwritten_sheet.png`, `corrupt.xlsx` | Four+ formats into one canonical model; re-upload = duplicate (idempotent); v2 of the CSV supersedes v1; corrupt file fails with a reason; handwriting rows flagged for review. |
| 4 | 2:15 | Ask as `a_eng_manager`: "Who was present on 1 September 2026?" | Structured answer, 4 citations (file + row), confidence + explanation, provider path. |
| 5 | 3:00 | Ask as `a_hr_admin`: "Show evidence that Alice was present on 3 Sep" | Hybrid: CSV row + the manager remark from the DOCX. Switch to `a_eng_manager`: the confidential remark is not shown. |
| 6 | 3:45 | Ask: "What was Alice's attendance in December 2027?" and "What is the company revenue?" | Controlled unavailable answers, no model call. |
| 7 | 4:15 | As `a_eng_manager`: "What was John Carter's attendance?" (tenant B) vs "What was Jane Doe's attendance?" | Identical responses: a denial never reveals that the person exists. Add filter entity = hr -> 403. |
| 8 | 4:45 | "Summarise the injection memo" | The memo's instruction paragraph is flagged, never obeyed, never quoted; warning shown. |
| 9 | 5:15 | Export page: the Q1 request as JSON, XLSX, PDF | Same rows, same checksum; PDF footer shows classification + request id. |
| 10 | 6:00 | Feedback page as `a_reviewer` on the Q2 answer ("Engineering's average attendance % in September") with the ideal wording from `docs/api_examples.http` | Validated against live data, stored as a new version; ask Q2 as `a_eng_manager` -> the approved wording with recomputed numbers; as `b_manager` -> not applied. Roll back. |
| 11 | 7:15 | `docs/TEST_SUMMARY.md` | Test counts, coverage, the 14 mandatory scenarios and the live evaluation, all generated from real runs. |

Command-line alternative for the whole flow: `docker compose run --rm -T api python -m scripts.demo`.
