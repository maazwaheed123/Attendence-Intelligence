"""Write the OpenAPI specification to docs/openapi.json (from the live app factory).

Usage (inside the api container):  python -m scripts.export_openapi
The dev-only /v1/auth routes are included because APP_ENV=dev mounts them; they
are marked as such in their descriptions and are never mounted in production.
tests/unit/test_docs.py fails if the committed file drifts from the code.
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def spec() -> dict:
    from app.main import create_app

    return create_app().openapi()


def render() -> str:
    return json.dumps(spec(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUT} ({len(spec()['paths'])} paths)")


if __name__ == "__main__":
    main()
