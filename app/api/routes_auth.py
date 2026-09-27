"""DEV/TEST ONLY: mint a token for a named synthetic persona.

Not registered at all when APP_ENV=prod (the route returns 404). In production a
real identity provider (OIDC) issues tokens; the gateway only verifies them.
"""

from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import APIRouter
from pydantic import BaseModel

from app.api.errors import AppError
from app.config import get_settings
from app.security.jwt import issue_token

router = APIRouter(prefix="/v1/auth", tags=["auth (dev only)"])


class DevTokenRequest(BaseModel):
    persona: str


class DevTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth token type, not a secret
    expires_in_min: int
    persona: str


@lru_cache
def load_personas() -> dict:
    spec = yaml.safe_load(Path(get_settings().dev_personas_file).read_text(encoding="utf-8"))
    return spec["personas"]


def persona_claims(name: str) -> dict:
    p = load_personas()[name]
    claims = {
        "sub": f"user:{name}",
        "product_id": p["product"],
        "tenant_id": p["tenant"],
        "module": "attendance",
        "role": p["role"],
        "entities": p["entities"],
        "clearance": p["clearance"],
    }
    if p.get("employee_id"):
        claims["employee_id"] = p["employee_id"]
    return claims


@router.get("/personas", summary="List synthetic test personas")
def list_personas() -> dict:
    return {name: {k: v for k, v in p.items()} for name, p in sorted(load_personas().items())}


@router.post("/dev-token", response_model=DevTokenResponse, summary="Issue a dev token")
def dev_token(body: DevTokenRequest) -> DevTokenResponse:
    if body.persona not in load_personas():
        raise AppError(404, "NOT_FOUND", "Unknown persona.")
    ttl = get_settings().jwt_ttl_min
    return DevTokenResponse(
        access_token=issue_token(persona_claims(body.persona), ttl),
        expires_in_min=ttl,
        persona=body.persona,
    )
