"""Optional HMAC authentication for server-to-server WordPress calls.

Wire this dependency only to private endpoints. Never use it to protect the
existing public GET APIs without a coordinated WordPress migration.
"""
import hashlib
import hmac
import os
import time

from fastapi import Header, HTTPException, Request

MAX_SKEW_SECONDS = 300
MAX_BODY_BYTES = 64 * 1024


def signature_for(secret: str, method: str, path: str, timestamp: str, body: bytes) -> str:
    message = (method.upper() + "\n" + path + "\n" + timestamp + "\n").encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


async def verify_wp_hmac(
    request: Request,
    x_api_key: str = Header(..., alias="X-API-Key"),
    x_timestamp: str = Header(..., alias="X-Timestamp"),
    x_signature: str = Header(..., alias="X-Signature"),
) -> None:
    api_key = os.environ.get("SPORTIQ_WP_API_KEY", "")
    secret = os.environ.get("SPORTIQ_WP_HMAC_SECRET", "")
    if not api_key or not secret:
        raise HTTPException(503, "HMAC authentication is not configured")
    if not hmac.compare_digest(x_api_key, api_key):
        raise HTTPException(401, "Invalid credentials")
    if not x_timestamp.isascii() or not x_timestamp.isdecimal() or len(x_timestamp) > 12:
        raise HTTPException(400, "Invalid timestamp")
    if abs(int(time.time()) - int(x_timestamp)) > MAX_SKEW_SECONDS:
        raise HTTPException(401, "Expired request")
    if len(x_signature) != 64 or any(c not in "0123456789abcdef" for c in x_signature):
        raise HTTPException(403, "Invalid signature")
    if request.headers.get("content-length", "").isdigit() and int(request.headers["content-length"]) > MAX_BODY_BYTES:
        raise HTTPException(413, "Request too large")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(413, "Request too large")
    expected = signature_for(secret, request.method, request.url.path, x_timestamp, body)
    if not hmac.compare_digest(expected, x_signature):
        raise HTTPException(403, "Invalid signature")
