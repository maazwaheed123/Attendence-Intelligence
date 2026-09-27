from pathlib import Path

from sqlalchemy import text

from app.db.session import owner_session

GENERATED = Path(__file__).resolve().parents[2] / "data" / "generated"


def upload(api, headers, name, content=None, **form):
    content = content if content is not None else (GENERATED / name).read_bytes()
    return api.post("/v1/ingest", headers=headers, files={"file": (name, content)}, data=form)


def records(where="true", **params):
    with owner_session() as s:
        return [
            dict(r)
            for r in s.execute(
                text(f"SELECT * FROM attendance_records WHERE {where} ORDER BY source_locator"),
                params,
            ).mappings()
        ]
