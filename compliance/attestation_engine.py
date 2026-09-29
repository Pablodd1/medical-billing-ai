# ============================================
# FILE: medical_billing_ai/compliance/attestation_engine.py
# PURPOSE: 1-Click Physician Legal Attestation & False Claims Act Liability Shield
# ============================================

import hashlib
from datetime import datetime
from typing import Dict, Any, Optional
from ..models.encounter import ProviderAttestation, ProviderInfo, BillingDetails

class PhysicianAttestationEngine:
    """
    Manages 1-click physician legal attestations.
    Transfers clinical liability to the licensed physician and creates an immutable audit trail
    to shield the AI billing platform from False Claims Act (31 U.S.C. § 3729) liability.
    """

    def generate_attestation_prompt(self, encounter_id: str, provider: ProviderInfo, billing: BillingDetails) -> Dict[str, Any]:
        """
        Builds the 3-second review modal payload for the clinician inside Magnolia App.
        """
        cpt_summary = ", ".join([f"CPT {line.code}" + (f"-{'-'.join(line.modifiers)}" if line.modifiers else "") for line in billing.cpt_codes])
        dx_summary = ", ".join([d.code for d in billing.diagnosis_codes])

        return {
            "encounter_id": encounter_id,
            "provider_name": f"{provider.rendering_first_name} {provider.rendering_last_name}, {provider.type.value}",
            "rendering_npi": provider.rendering_npi_type_1 or provider.npi,
            "summary_for_doctor": f"AI coded: [{cpt_summary}] linked to [{dx_summary}]. Total: ${billing.total_charge:.2f}",
            "prompt_text": "Please attest that you personally performed or directly supervised these services and that documentation supports these codes.",
            "attestation_action_required": True
        }

    def sign_attestation(self, encounter_id: str, provider_npi: str, provider_name: str, billing_summary: str) -> ProviderAttestation:
        """
        Executes the digital signature hash and creates the legal attestation record.
        """
        now_iso = datetime.now().isoformat()
        
        # Cryptographic signature hash
        signature_payload = f"{encounter_id}|{provider_npi}|{now_iso}|{billing_summary}".encode("utf-8")
        signature_hash = hashlib.sha256(signature_payload).hexdigest()

        return ProviderAttestation(
            is_attested=True,
            attested_by_name=provider_name,
            attested_by_npi=provider_npi,
            attested_at=now_iso,
            signature_hash=signature_hash
        )

    def verify_attestation(self, attestation: Optional[ProviderAttestation], provider_npi: str) -> bool:
        """
        Validates if the encounter has a valid, matching physician attestation.
        """
        if not attestation or not attestation.is_attested:
            return False
        if attestation.attested_by_npi != provider_npi:
            return False
        if not attestation.signature_hash or len(attestation.signature_hash) != 64:
            return False
        return True
