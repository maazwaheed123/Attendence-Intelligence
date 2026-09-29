"""Render docs/architecture.png (the reference layers + the isolation boundary).

Drawn with Pillow so it is reproducible without a browser or mermaid-cli.
docs/architecture.md holds the same picture as Mermaid source.

Usage (inside the api container):  python -m scripts.render_architecture
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parents[1] / "docs" / "architecture.png"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
W, H = 2000, 1330
INK, MUTED, RED = (30, 34, 40), (70, 76, 86), (196, 40, 40)
FILL = {
    "client": (236, 242, 250),
    "gateway": (226, 236, 250),
    "ingest": (232, 246, 236),
    "orch": (244, 238, 250),
    "gen": (252, 242, 228),
    "retr": (238, 244, 252),
    "post": (250, 236, 236),
    "store": (246, 246, 246),
}

BOXES = [
    ("client", "client", (60, 40, 460, 190), "Clients",
     ["Streamlit UI :8501", "curl / Swagger /docs", "scripts/demo.py"]),
    ("gateway", "gateway", (560, 40, 1940, 190), "API gateway  (FastAPI, app/api)",
     ["RequestIdMiddleware: request id + audit of every call",
      "get_security_context: JWT (HS256, pinned alg/aud/iss) -> claims -> tenant/product/entity "
      "registry -> rate limit",
      "require(Permission) = RBAC   |   enforce_request_context: body filters can never widen "
      "the token scope"]),
    ("ingest", "ingest", (60, 250, 640, 580), "Ingestion  (app/ingestion, RQ worker)",
     ["magic-byte detection, SHA-256 idempotency, logical-name versioning",
      "parsers: CSV, XLSX, DOCX, text PDF, scanned PDF, PNG/JPG",
      "OCR: OpenCV clean-up -> Tesseract + qwen2.5vl -> reconcile",
      "normalize: header synonyms, tenant date format, status vocabulary, roster match",
      "uncertain values -> review flag (never a fact)",
      "row cards + narrative chunks: PII masked, injection flagged, embedded (nomic)"]),
    ("orch", "orch", (700, 250, 1300, 580), "Query orchestrator  (app/orchestrator.py)",
     ["1  scoped directory: roster, entities, coverage",
      "2  classify: rules first, LLM JSON only if needed",
      "3  rewrite dates / weeks / names via the directory",
      "4  route: structured | document | hybrid | refuse",
      "5  Redis query cache (scope-keyed, versioned)",
      "6  approved feedback: values recomputed live",
      "7  confidence, needs_review, unavailable"]),
    ("gen", "gen", (1360, 250, 1940, 580), "Generation  (app/generation)",
     ["LLMRouter: qwen2.5 7b -> qwen2.5 3b -> deterministic template",
      "circuit breaker, JSON repair round, every call audited",
      "phrasing grounded in SQL rows (numbers, ids, names checked)",
      "document answers cite [Cn] tags only; unsupported sentences dropped",
      "uploaded content is framed as UNTRUSTED DATA"]),
    ("post", "post", (60, 640, 640, 900), "Post-processing  (app/governance, app/export)",
     ["postprocess.finalize() on EVERY answer:",
      "citations must be in the retrieved set; leakage guard (foreign ids, names, tenants);",
      "injection output check; role-aware PII masking; schema -> safe fallback",
      "persist (question redacted + hashed), audit; exports JSON / XLSX / PDF"]),
    ("retr", "retr", (700, 640, 1640, 900),
     "Retrieval layer  (app/retrieval)  - runs as rag_reader",
     ["structured: template SQL + LLM SQL -> sqlglot allow-list validator (one SELECT on "
      "v_attendance) -> executor -> lineage citations",
      "document: pgvector HNSW + Postgres FTS + trigram + named document -> RRF (k=60) -> "
      "dedupe -> LexicalReranker -> sufficiency",
      "hybrid: structured answer + supporting narrative quotes"]),
    ("pg", "store", (70, 1000, 1290, 1230),
     "PostgreSQL 16 + pgvector  (knowledge + metadata store)",
     ["attendance_records -> v_attendance (security_invoker; metric rules fixed in the view)",
      "document_chunks: text_masked, embedding vector(768) HNSW, tsvector FTS, pg_trgm",
      "source_documents, ingestion_jobs, query_responses, feedback_examples, audit_events "
      "(append-only)",
      "roles: rag_reader (SELECT, no PII columns), app_rw (no DELETE); owner = migrations only",
      "scoped_session(scope): transaction-local app.* settings -> app_scope_ok(product, tenant, "
      "module, entity, classification) + app_self_ok(employee)"]),
    ("redis", "store", (1360, 1000, 1640, 1290), "Redis",
     ["RQ queue 'ingestion'", "query cache qc:*", "data / feedback versions",
      "rate limit, breakers"]),
    ("ollama", "store", (1680, 1000, 1940, 1290), "Ollama",
     ["host :11434 (Windows)", "qwen2.5:7b-instruct", "qwen2.5:3b-instruct",
      "qwen2.5vl:3b (vision)", "nomic-embed-text"]),
]  # fmt: skip

ARROWS = [
    [(460, 115), (560, 115)],
    [(700, 190), (350, 250)],
    [(1000, 190), (1000, 250)],
    [(1300, 415), (1360, 415)],
    [(1000, 580), (1000, 640)],
    [(760, 580), (640, 700)],
    [(1000, 900), (1000, 1000)],
    [(350, 900), (350, 1000)],
    [(60, 415), (30, 415), (30, 1140), (70, 1140)],
    [(1810, 580), (1810, 1000)],
    [(1580, 900), (1580, 1000)],
]


def _font(path, size):
    return ImageFont.truetype(path, size)


def _wrap(draw, text, font, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=font) <= width:
            cur = test
        else:
            lines.append(cur)
            cur = w
    return [*lines, cur] if cur else lines


def _arrow(draw, pts, color=INK):
    draw.line(pts, fill=color, width=3)
    (x0, y0), (x1, y1) = pts[-2], pts[-1]
    ang = math.atan2(y1 - y0, x1 - x0)
    size = 14
    left = (x1 - size * math.cos(ang - 0.4), y1 - size * math.sin(ang - 0.4))
    right = (x1 - size * math.cos(ang + 0.4), y1 - size * math.sin(ang + 0.4))
    draw.polygon([(x1, y1), left, right], fill=color)


def _dashed_rect(draw, box, color, dash=16, gap=10, width=4):
    x0, y0, x1, y1 = box
    for y in (y0, y1):
        x = x0
        while x < x1:
            draw.line([(x, y), (min(x + dash, x1), y)], fill=color, width=width)
            x += dash + gap
    for x in (x0, x1):
        y = y0
        while y < y1:
            draw.line([(x, y), (x, min(y + dash, y1))], fill=color, width=width)
            y += dash + gap


def render(path: Path = OUT) -> list[str]:
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    title, body, small = _font(BOLD, 21), _font(FONT, 16), _font(BOLD, 18)
    overflow = []
    _dashed_rect(d, (40, 950, 1320, 1315), RED)
    label = ("ISOLATION BOUNDARY - Postgres Row-Level Security (ENABLE + FORCE on every tenant "
             "table): rows outside the caller's scope never leave the database, so no retrieval "
             "mode, cache entry, export or model can see them.")  # fmt: skip
    for i, part in enumerate(_wrap(d, label, _font(BOLD, 15), 1240)):
        d.text((60, 1244 + i * 21), part, font=_font(BOLD, 15), fill=RED)
    for key, fill, (x0, y0, x1, y1), head, lines in BOXES:
        d.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=FILL[fill], outline=INK, width=2)
        d.text((x0 + 16, y0 + 12), head, font=title, fill=INK)
        y = y0 + 46
        for line in lines:
            for part in _wrap(d, line, body, x1 - x0 - 32):
                d.text((x0 + 16, y), part, font=body, fill=MUTED)
                y += 22
            y += 4
        if y > y1 - 6:
            overflow.append(key)
    for pts in ARROWS:
        _arrow(d, pts)
    d.text((1012, 910), "every read: scoped_session(ctx.to_scope())",
           font=small, fill=RED)  # fmt: skip
    path.parent.mkdir(exist_ok=True)
    img.save(path, optimize=True)
    return overflow


if __name__ == "__main__":
    bad = render()
    print(f"wrote {OUT}" + (f" (text overflows: {bad})" if bad else ""))
