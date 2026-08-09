"""Test fixtures.

Tests run against in-memory SQLite so the suite needs no containers. That works
because Phase 2 uses no Postgres-specific SQL — JSONB columns are declared with
`.with_variant()` and fall back to JSON. From Phase 4, when snapshot tables get
native partitioning, a Postgres-backed suite will be added alongside this one.
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncGenerator

# Environment must be configured before app modules import Settings.
#
# The dotenv search is disabled outright: a developer running the suite with a
# real .env present would otherwise inherit their live Redis (making the rate
# limiter fire mid-suite) or their feature flags. Tests configure everything
# they need explicitly, below.
os.environ["XAGENT_ENV_FILE"] = "/nonexistent/xagent-tests.env"
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-long-enough-for-validation-x")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("DATABASE_URL_OVERRIDE", "sqlite+aiosqlite:///:memory:")
# Deliberately unreachable: the rate limiter falls back to process-local state,
# so tests never depend on a Redis being up or share counters between runs.
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:1/0")

# Dummy X credentials so the OAuth handshake can be exercised. No test makes a
# real call to X — every one runs against httpx.MockTransport.
os.environ.setdefault("X_CLIENT_ID", "test-client-id")
os.environ.setdefault("X_CLIENT_SECRET", "test-client-secret")  # noqa: S105

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402

TEST_EMAIL = "owner@example.com"
TEST_PASSWORD = "correct-horse-battery-staple-9"  # noqa: S105


@pytest.fixture
async def engine() -> AsyncGenerator[object, None]:
    # StaticPool keeps every connection pointed at the same in-memory database.
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db_session(engine) -> AsyncGenerator[AsyncSession, None]:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest.fixture
async def client(engine) -> AsyncGenerator[AsyncClient, None]:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def registered_client(client: AsyncClient) -> AsyncClient:
    """A client with the owner account registered and its session cookie set."""
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "display_name": "Test Owner",
            "timezone": "UTC",
        },
    )
    assert resp.status_code == 201, resp.text
    return client
