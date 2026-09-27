"""Role -> permission matrix and role clearance ceilings (PLAN.txt section D.2)."""

from enum import StrEnum

from app.security.scope import CLEARANCE_RANK


class Permission(StrEnum):
    INGEST = "ingest"
    QUERY = "query"
    EXPORT = "export"
    FEEDBACK_SUBMIT = "feedback_submit"
    FEEDBACK_MANAGE = "feedback_manage"  # deactivate / rollback
    AUDIT_READ = "audit_read"


ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    "hr_admin": frozenset(Permission),
    "manager": frozenset({Permission.INGEST, Permission.QUERY, Permission.EXPORT}),
    "reviewer": frozenset(
        {Permission.QUERY, Permission.FEEDBACK_SUBMIT, Permission.FEEDBACK_MANAGE}
    ),
    "employee": frozenset({Permission.QUERY}),
    "auditor": frozenset({Permission.AUDIT_READ}),
}

# A token may never claim a clearance above its role's ceiling.
ROLE_MAX_CLEARANCE = {
    "hr_admin": "restricted",
    "manager": "confidential",
    "reviewer": "internal",
    "employee": "internal",
    "auditor": "internal",
}

ROLES = frozenset(ROLE_PERMISSIONS)


def has_permission(role: str, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def clearance_allowed(role: str, clearance: str) -> bool:
    ceiling = ROLE_MAX_CLEARANCE.get(role)
    return (
        ceiling is not None
        and clearance in CLEARANCE_RANK
        and CLEARANCE_RANK[clearance] <= CLEARANCE_RANK[ceiling]
    )
