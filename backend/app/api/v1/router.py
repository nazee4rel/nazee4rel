"""Aggregate v1 router.

Later phases append their routers here:
  Phase 3  x_oauth
  Phase 6  agent
  Phase 7  dashboard aggregates (on the analytics router)
  Phase 8  alerts, reports
"""

from fastapi import APIRouter

from app.api.v1 import (
    accounts,
    agent,
    alerts,
    analytics,
    auth,
    collection,
    health,
    x_oauth,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(accounts.router)
api_router.include_router(x_oauth.router)
api_router.include_router(collection.router)
api_router.include_router(analytics.router)
api_router.include_router(agent.router)
api_router.include_router(alerts.router)
