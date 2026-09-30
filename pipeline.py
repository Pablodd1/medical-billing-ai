# ============================================
# FILE: medical_billing_ai/pipeline.py
# PURPOSE: Master End-to-End Medical Billing & Autonomous Coding Pipeline
# ============================================

import sys
import os
from typing import Dict, Any, Union

# Ensure current directory is in sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

try:
    from models.encounter import EncounterPayload, DocumentSourceType, PatientInfo, ProviderInfo, PayerInfo, ClinicalNoteParsed, BillingDetails
    from ingestion.multimodal_ingestor import MultimodalDocumentIngestor
    from eligibility.eligibility_engine import RealTimeEligibilityEngine
    from coding.clinical_coder import ClinicalCodingEngine
    from scrutiny.hcfa_auditor import HCFA1500ExtractorAndAuditor
    from scrutiny.scrutinizer import MedicalBillingScrutinizer
    from transmission.x12_837_generator import X12_837P_Generator
    from transmission.office_ally_dispatcher import OfficeAllyDispatcher
    from remittance.era_835_parser import ERA_835_Parser
    from remittance.denial_appeals_engine import DenialAppealsEngine
    from config import OfficeAllyConfig
except ImportError:
    from .models.encounter import EncounterPayload, DocumentSourceType, PatientInfo, ProviderInfo, PayerInfo, ClinicalNoteParsed, BillingDetails
    from .ingestion.multimodal_ingestor import MultimodalDocumentIngestor
    from .eligibility.eligibility_engine import RealTimeEligibilityEngine
    from .coding.clinical_coder import ClinicalCodingEngine
    from .scrutiny.hcfa_auditor import HCFA1500ExtractorAndAuditor
    from .scrutiny.scrutinizer import MedicalBillingScrutinizer
    from .transmission.x12_837_generator import X12_837P_Generator
    from .transmission.office_ally_dispatcher import OfficeAllyDispatcher
    from .remittance.era_835_parser import ERA_835_Parser
    from .remittance.denial_appeals_engine import DenialAppealsEngine
    from .config import OfficeAllyConfig

class AutonomousMedicalBillingPipeline:
    """
    Unified End-to-End Orchestrator for Medical Billing AI.
    Connects Multimodal Ingestion -> Eligibility (270/271) -> Clinical Coding ->
    5-Tier Scrutiny -> CDI Queries / HITL -> 837P Transmission -> OA SFTP -> 835 Remittance Loop.
    """

    def __init__(self, submitter_id: str = "MAGNOLIA_HEALTH", config: OfficeAllyConfig = None):
        self.config = config or OfficeAllyConfig()
        if submitter_id:
            self.config.claims_submitter_id = submitter_id
        self.ingestor = MultimodalDocumentIngestor()
        self.eligibility_engine = RealTimeEligibilityEngine()
        self.coding_engine = ClinicalCodingEngine()
        self.hcfa_auditor = HCFA1500ExtractorAndAuditor()
        self.scrutinizer = MedicalBillingScrutinizer()
        self.x12_generator = X12_837P_Generator(config=self.config)
        # Real SFTP when credentials exist; otherwise a clearly-labelled
        # simulation so dev/test environments keep working.
        self.dispatcher = OfficeAllyDispatcher(
            config=self.config,
            allow_simulation=not self.config.has_sftp_credentials,
        )
        self.era_parser = ERA_835_Parser()
        self.appeals_engine = DenialAppealsEngine()

    def process_encounter(
        self,
        raw_doc_or_content: Union[str, bytes],
        doc_type: DocumentSourceType,
        patient_data: Dict[str, Any],
        provider_data: Dict[str, Any],
        payer_data: Dict[str, Any],
        encounter_id: str = "ENC-001",
        date_of_service: str = "2026-09-25",
        is_telehealth: bool = False
    ) -> Dict[str, Any]:
        pipeline_log = []

        # STAGE 1: Multimodal Ingestion & Multi-Date / POS Disambiguation
        parsed_note = self.ingestor.ingest_document(raw_doc_or_content, doc_type)
        
        # Disambiguate Date of Service (DOS)
        effective_dos = date_of_service
        if parsed_note.date_of_service_detected:
            effective_dos = parsed_note.date_of_service_detected
            pipeline_log.append(f"[Stage 1] Multi-Date Disambiguation: DOS detected as '{effective_dos}' (Reg Date='{parsed_note.registration_date_detected}', Sig Date='{parsed_note.signature_date_detected}')")
        else:
            pipeline_log.append(f"[Stage 1] Ingested document type '{doc_type.value}' ({len(parsed_note.raw_text)} chars). Using explicit DOS: '{effective_dos}'")

        pipeline_log.append(f"[Stage 1] POS Disambiguation: Detected POS '{parsed_note.place_of_service_detected}'")

        # STAGE 2: 270/271 Eligibility
        patient = PatientInfo(**patient_data)
        provider = ProviderInfo(**provider_data)
        payer = PayerInfo(**payer_data)

        eligibility_res = self.eligibility_engine.check_eligibility(patient, payer)
        pipeline_log.append(f"[Stage 2] 270/271 Eligibility Checked: Active={eligibility_res['is_active_coverage']}, Copay=${eligibility_res['copay_amount']}")

        if not eligibility_res["is_active_coverage"]:
            return {
                "status": "BLOCKED_INACTIVE_COVERAGE",
                "stage": "ELIGIBILITY_270_271",
                "message": "Patient insurance policy is inactive. Claim blocked before clinical coding.",
                "eligibility": eligibility_res,
                "pipeline_log": pipeline_log
            }

        # STAGE 3: Clinical NLP Coding & MDM
        billing = self.coding_engine.generate_coding(parsed_note, is_telehealth=is_telehealth)
        pipeline_log.append(f"[Stage 3] Coded {len(billing.diagnosis_codes)} ICD-10 dx and {len(billing.cpt_codes)} CPT lines (POS={billing.place_of_service}). Total: ${billing.total_charge}")

        encounter_payload = EncounterPayload(
            encounter_id=encounter_id,
            date_of_service=effective_dos,
            patient=patient,
            provider=provider,
            payer=payer,
            clinical_note=parsed_note,
            billing=billing
        )
        encounter_dict = encounter_payload.model_dump()

        # STAGE 4: HCFA-1500 Audit & Physician Micro-Queries
        hcfa_res = self.hcfa_auditor.audit_hcfa_requirements(encounter_dict)
        if hcfa_res["physician_queries"]:
            pipeline_log.append(f"[Stage 4] CDI Alert: {len(hcfa_res['physician_queries'])} real-time physician micro-queries generated")

        # STAGE 5: 8-Tier Scrutiny (FCSO, NCCI, Timely Filing, Duplicate Guard, Dual NPI)
        scrutiny_res = self.scrutinizer.scrutinize_encounter(encounter_dict)
        encounter_payload.claim_fingerprint_hash = scrutiny_res.claim_fingerprint
        if scrutiny_res.timely_filing:
            from models.encounter import TimelyFilingAssessment
            encounter_payload.timely_filing = TimelyFilingAssessment(**scrutiny_res.timely_filing)
        encounter_dict = encounter_payload.model_dump()

        pipeline_log.append(f"[Stage 5] Scrutiny Result: Passed={scrutiny_res.passed}, Confidence={scrutiny_res.confidence_score * 100:.1f}%, Fingerprint={scrutiny_res.claim_fingerprint[:10]}...")

        if not scrutiny_res.passed or not hcfa_res["is_clean_claim"]:
            return {
                "status": "FLAGGED_FOR_HUMAN_REVIEW",
                "confidence_score": scrutiny_res.confidence_score,
                "scrutiny_issues": scrutiny_res.issues,
                "hcfa_missing_fields": hcfa_res["missing_fields"],
                "physician_queries": hcfa_res["physician_queries"],
                "claim_fingerprint": scrutiny_res.claim_fingerprint,
                "timely_filing": scrutiny_res.timely_filing,
                "pipeline_log": pipeline_log
            }

        # STAGE 6: 837P Generation & Dispatch (with Secondary COB if present)
        encounter_dict["patient_control_number"] = encounter_id
        x12_837_content = self.x12_generator.generate_837p(encounter_dict)
        dispatch_res = self.dispatcher.dispatch_batch(x12_837_content, batch_id=f"BAT_{encounter_id}")
        # Record the claim in the ledger so real 999/277CA/835 files can be
        # reconciled against it by patient control number (CLM01).
        self.dispatcher.ledger.record_claims_in_batch(f"BAT_{encounter_id}", [{
            "patient_control_number": encounter_dict["patient_control_number"],
            "encounter_id": encounter_id,
            "patient_name": f"{patient.last_name}, {patient.first_name}" if hasattr(patient, "last_name") else "",
            "total_charge": billing.total_charge,
        }])
        if dispatch_res.get("simulation"):
            pipeline_log.append("[Stage 6] SIMULATION MODE: 837P generated but NOT transmitted (no OA SFTP credentials configured).")
        else:
            pipeline_log.append("[Stage 6] 837P uploaded to Office Ally /inbound via SFTP; awaiting real 999/277CA from /outbound.")

        return {
            "status": "TRANSMITTED_CLEAN_CLAIM",
            "confidence_score": scrutiny_res.confidence_score,
            "encounter_id": encounter_id,
            "date_of_service": effective_dos,
            "place_of_service": billing.place_of_service,
            "total_billed": billing.total_charge,
            "claim_fingerprint": scrutiny_res.claim_fingerprint,
            "timely_filing": scrutiny_res.timely_filing,
            "has_secondary_cob": bool(patient.secondary_payer),
            "x12_837_snippet": x12_837_content.split("\n")[:12],
            "clearinghouse_dispatch": dispatch_res,
            "pipeline_log": pipeline_log
        }
