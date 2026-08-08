"""Model registry.

Alembic autogenerate only sees tables whose modules have been imported, so every
model module must be re-exported here. A model missing from this list produces a
migration that silently drops nothing and creates nothing — a failure mode worth
avoiding by convention.
"""

from app.db.base import Base
from app.models.audit import AuditLog, SystemLog
from app.models.collection import CollectionKind, CollectionRun, CollectionStatus
from app.models.content import (
    AccountMetricSnapshot,
    Post,
    PostMetricSnapshot,
    PostType,
)
from app.models.enums import (
    AuditAction,
    CapabilityStatus,
    Provenance,
    UserRole,
    XCapability,
)
from app.models.usage import ApiUsageLedger, OAuthState
from app.models.user import Session, User
from app.models.x_account import AccountCapability, OAuthToken, XAccount

__all__ = [
    "AccountCapability",
    "AccountMetricSnapshot",
    "ApiUsageLedger",
    "AuditAction",
    "AuditLog",
    "Base",
    "CapabilityStatus",
    "CollectionKind",
    "CollectionRun",
    "CollectionStatus",
    "OAuthState",
    "OAuthToken",
    "Post",
    "PostMetricSnapshot",
    "PostType",
    "Provenance",
    "Session",
    "SystemLog",
    "User",
    "UserRole",
    "XAccount",
    "XCapability",
]
