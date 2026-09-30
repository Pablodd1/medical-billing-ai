# ============================================
# FILE: medical_billing_ai/security/phi_crypto.py
# PURPOSE: PHI encryption at rest (HIPAA Security Rule 45 CFR 164.312(a)(2)(iv))
#
# Fernet (AES-128-CBC + HMAC) symmetric encryption keyed from the
# PHI_ENCRYPTION_KEY env var. Generate a key once with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#
# If cryptography is not installed or no key is configured, encryption is
# UNAVAILABLE and callers must refuse to persist PHI (fail-closed helper
# `encryption_available()`).
# ============================================

import hashlib
import os
from typing import Optional

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None
    InvalidToken = Exception


def encryption_available() -> bool:
    return Fernet is not None and bool(os.getenv("PHI_ENCRYPTION_KEY"))


def _fernet() -> "Fernet":
    if Fernet is None:
        raise RuntimeError("cryptography package required: pip install cryptography")
    key = os.getenv("PHI_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("PHI_ENCRYPTION_KEY env var is not set; refusing to handle PHI.")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_phi(plaintext: str) -> str:
    """Encrypts a PHI string for storage. Returns urlsafe-base64 token."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_phi(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError("PHI decryption failed: wrong key or corrupted data.") from e


def hash_identifier(value: str) -> str:
    """One-way hash of a patient identifier (PCN/member ID) for logs and
    audit trails where the raw identifier is not needed."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
