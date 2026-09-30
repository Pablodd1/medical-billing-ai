# ============================================
# FILE: medical_billing_ai/security/audit_log.py
# PURPOSE: HIPAA audit trail (45 CFR 164.312(b)) — append-only JSONL log of
#          every access to or action on PHI-bearing endpoints.
#
# Rules:
#   - Never log PHI itself (no patient names, DOB, member IDs, notes).
#     Identifiers are logged only as one-way hashes.
#   - Never log API keys; only their fingerprints.
#   - The log is append-only by contract; retention/rotation is an ops duty.
# ============================================

import json
import os
from datetime import datetime, timezone
from typing import Optional

_LOG_PATH = os.getenv("AUDIT_LOG_PATH", os.path.join(os.path.dirname(__file__), "hipaa_audit.jsonl"))


def audit_event(
    actor_fingerprint: str,
    action: str,
    endpoint: str,
    outcome: str,
    patient_ref: Optional[str] = None,
    detail: Optional[dict] = None,
) -> None:
    """Append one tamper-evident-style audit record.

    actor_fingerprint: from security.auth.key_fingerprint (never the key)
    patient_ref: pass the RAW identifier; it is hashed before writing.
    """
    from security.phi_crypto import hash_identifier

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor_fingerprint,
        "action": action,
        "endpoint": endpoint,
        "outcome": outcome,
        "patient_ref_hash": hash_identifier(patient_ref) if patient_ref else None,
        "detail": detail or {},
    }
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
