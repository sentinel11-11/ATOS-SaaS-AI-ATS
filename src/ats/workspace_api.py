"""Compose workspace resources; domain logic lives in services and repositories."""

from fastapi import APIRouter

from ats.api_calls import router as api_calls_router
from ats.api_campaigns import router as api_campaigns_router
from ats.api_contacts import router as api_contacts_router
from ats.api_identity import router as api_identity_router
from ats.api_operations import router as api_operations_router
from ats.api_resources import router as api_resources_router

router = APIRouter()
router.include_router(api_contacts_router)
router.include_router(api_campaigns_router)
router.include_router(api_calls_router)
router.include_router(api_resources_router)
router.include_router(api_identity_router)
router.include_router(api_operations_router)
