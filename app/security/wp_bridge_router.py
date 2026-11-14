"""Private WordPress-to-FastAPI HMAC connectivity check.

This endpoint does not invoke the prediction engine or modify stored data.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from app.security.wp_hmac import verify_wp_hmac

router = APIRouter(prefix="/v1/internal", tags=["internal"])

class HmacCheckRequest(BaseModel):
    fixture_id: int = Field(ge=1)

@router.post("/hmac-check", dependencies=[Depends(verify_wp_hmac)])
def hmac_check(payload: HmacCheckRequest):
    return {"status": "success", "authenticated": True, "fixture_id": payload.fixture_id}
