"""Request/response schemas for authentication.

Note what is absent: no schema in this project serialises a password hash, a
session token, or an OAuth token. That is enforced by these types being the only
things endpoints return.
"""

from __future__ import annotations

import uuid
import zoneinfo
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import UserRole

# Long enough to resist offline attack, capped because Argon2 hashing cost grows
# with input and an unbounded password is a cheap DoS.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    display_name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(default="UTC", max_length=64)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            zoneinfo.ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Unknown IANA timezone: {v!r}") from exc
        return v

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        # Length is the dominant factor; a small variety check catches the worst
        # cases without pushing users toward predictable substitutions.
        if v.isalpha() or v.isdigit():
            raise ValueError("Password must combine letters with numbers or symbols.")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str
    role: UserRole
    timezone: str
    totp_enabled: bool
    last_login_at: datetime | None
    created_at: datetime


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    ip_address: str | None
    user_agent: str | None
    is_current: bool = False


class MessageOut(BaseModel):
    message: str


class SetupStatusOut(BaseModel):
    """Answer to "does this instance have an owner yet?".

    Deliberately one boolean. The login page needs to pick a form, not learn
    anything else about who is registered.
    """

    owner_exists: bool
