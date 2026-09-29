# ============================================
# FILE: medical_billing_ai/enrollment/edi_enrollment_tracker.py
# PURPOSE: Payer EDI Enrollment & Credentialing Tracker (Pre-Transmission Gate)
# ============================================

from typing import Dict, Any, List, Optional
from datetime import datetime

class PayerEDIEnrollmentTracker:
    """
    Validates whether the Billing/Rendering NPI has completed official EDI enrollment
    with the target payer before transmission to Office Ally.
    Prevents instant 277CA clearinghouse rejections.
    """

    # Known payers requiring formal EDI enrollment prior to 837 claim submission
    PAYERS_REQUIRING_ENROLLMENT = {
        "00140": {"name": "Medicare Part B Florida (FCSO)", "avg_approval_days": 45, "requires_paper": False},
        "00590": {"name": "Florida Blue (BCBS FL)", "avg_approval_days": 21, "requires_paper": False},
        "77027": {"name": "Florida Medicaid (AHCA)", "avg_approval_days": 30, "requires_paper": True},
        "61101": {"name": "Humana", "avg_approval_days": 14, "requires_paper": False}
    }

    def __init__(self):
        # In-memory registry of approved NPI + Payer enrollment records
        # Key: (billing_npi, payer_id) -> record
        self._enrollment_registry: Dict[tuple, Dict[str, Any]] = {}
        self._seed_default_enrollments()

    def _seed_default_enrollments(self):
        # Default approved mock clinic NPIs for tests
        self.register_enrollment(
            billing_npi="1234567890",
            payer_id="00590",
            status="ACTIVE",
            effective_date="2026-01-01"
        )
        self.register_enrollment(
            billing_npi="1987654321",
            payer_id="00590",
            status="ACTIVE",
            effective_date="2026-01-01"
        )
        self.register_enrollment(
            billing_npi="1234567890",
            payer_id="00140",
            status="ACTIVE",
            effective_date="2026-01-01"
        )

    def register_enrollment(self, billing_npi: str, payer_id: str, status: str = "ACTIVE", effective_date: str = None) -> Dict[str, Any]:
        record = {
            "billing_npi": billing_npi,
            "payer_id": payer_id,
            "status": status,  # ACTIVE, PENDING_PAYER_APPROVAL, NOT_ENROLLED
            "effective_date": effective_date or datetime.now().strftime("%Y-%m-%d"),
            "last_verified": datetime.now().isoformat()
        }
        self._enrollment_registry[(billing_npi, payer_id)] = record
        return record

    def check_enrollment_status(self, billing_npi: str, payer_id: str) -> Dict[str, Any]:
        """
        Validates if claims can be transmitted to this payer under this billing NPI.
        """
        key = (billing_npi, payer_id)
        record = self._enrollment_registry.get(key)

        requires_enrollment = payer_id in self.PAYERS_REQUIRING_ENROLLMENT
        payer_info = self.PAYERS_REQUIRING_ENROLLMENT.get(payer_id, {"name": "Commercial Carrier", "avg_approval_days": 10})

        if not requires_enrollment:
            # Payers that do not require prior EDI enrollment (auto-accept standard claims)
            return {
                "can_transmit": True,
                "status": "NOT_REQUIRED",
                "message": f"Payer {payer_id} does not require pre-enrollment. Auto-cleared for transmission."
            }

        if not record or record["status"] != "ACTIVE":
            status = record["status"] if record else "NOT_ENROLLED"
            return {
                "can_transmit": False,
                "status": status,
                "payer_name": payer_info["name"],
                "message": (
                    f"EDI Enrollment required for Payer '{payer_info['name']}' (ID: {payer_id}) under Billing NPI {billing_npi}. "
                    f"Current Status: {status}. Submitting now will trigger instant clearinghouse 277CA rejection."
                ),
                "action_required": f"Submit Office Ally EDI Payer Enrollment Form for {payer_info['name']} (takes approx {payer_info['avg_approval_days']} days)."
            }

        return {
            "can_transmit": True,
            "status": "ACTIVE",
            "effective_date": record["effective_date"],
            "message": f"EDI Enrollment active and verified for Payer {payer_id} under NPI {billing_npi}."
        }
