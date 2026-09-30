# ============================================
# FILE: medical_billing_ai/security/auth.py
# PURPOSE: API authentication for the FastAPI service (HIPAA Access Control)
#
# Every endpoint (except /health) requires an X-API-Key header. Keys come
# from the API_KEYS env var (comma-separated). Comparison is constant-time.
# If no keys are configured the API refuses all authenticated requests
# (fail-closed), because an unauthenticated PHI endpoint is a HIPAA breach.
# ============================================

import hmac
import os
from typing import Optional

from fastapi import Header, HTTPException


def _configured_keys() -> list:
    raw = os.getenv("API_KEYS", "") or os.getenv("API_KEY", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def key_fingerprint(api_key: str) -> str:
    """Non-reversible identifier for audit logs (never log the key itself)."""
    import hashlib
    return hashlib.sha256(api_key.encode()).hexdigest()[:12]


async def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> str:
    keys = _configured_keys()
    if not keys:
        raise HTTPException(
            status_code=503,
            detail="API authentication is not configured (set API_KEYS). "
                   "Service is fail-closed per HIPAA access-control requirements.",
        )
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header.")
    if not any(hmac.compare_digest(x_api_key, k) for k in keys):
        raise HTTPException(status_code=403, detail="Invalid API key.")
    return key_fingerprint(x_api_key)
