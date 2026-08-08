"""Aggregate v1 router.

Later phases append their routers here:
  Phase 3  x_oauth
  Phase 4  collection
  Phase 5  analytics
  Phase 6  agent, insights, recommendations
  Phase 7  dashboard aggregates
  Phase 8  alerts, reports
"""

from fastapi import APIRouter

from app.api.v1 import accounts, auth, health

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(accounts.router)
