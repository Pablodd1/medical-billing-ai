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

class AutonomousMedicalBillingPipeline:
    """
    Unified End-to-End Orchestrator for Medical Billing AI.
    Connects Multimodal Ingestion -> Eligibility (270/271) -> Clinical Coding ->
    5-Tier Scrutiny -> CDI Queries / HITL -> 837P Transmission -> OA SFTP -> 835 Remittance Loop.
    """

    def __init__(self, submitter_id: str = "MAGNOLIA_HEALTH"):
        self.ingestor = MultimodalDocumentIngestor()
        self.eligibility_engine = RealTimeEligibilityEngine()
        self.coding_engine = ClinicalCodingEngine()
        self.hcfa_auditor = HCFA1500ExtractorAndAuditor()
        self.scrutinizer = MedicalBillingScrutinizer()
        self.x12_generator = X12_837P_Generator(submitter_id=submitter_id)
        self.dispatcher = OfficeAllyDispatcher(submitter_id=submitter_id)
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

        # STAGE 1: Multimodal Ingestion
        parsed_note = self.ingestor.ingest_document(raw_doc_or_content, doc_type)
        pipeline_log.append(f"[Stage 1] Ingested document type '{doc_type.value}' ({len(parsed_note.raw_text)} chars)")

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
        pipeline_log.append(f"[Stage 3] Coded {len(billing.diagnosis_codes)} ICD-10 dx and {len(billing.cpt_codes)} CPT lines. Total: ${billing.total_charge}")

        encounter_payload = EncounterPayload(
            encounter_id=encounter_id,
            date_of_service=date_of_service,
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

        # STAGE 5: 5-Tier Scrutiny
        scrutiny_res = self.scrutinizer.scrutinize_encounter(encounter_dict)
        pipeline_log.append(f"[Stage 5] Scrutiny Result: Passed={scrutiny_res.passed}, Confidence={scrutiny_res.confidence_score * 100:.1f}%")

        if not scrutiny_res.passed or not hcfa_res["is_clean_claim"]:
            return {
                "status": "FLAGGED_FOR_HUMAN_REVIEW",
                "confidence_score": scrutiny_res.confidence_score,
                "scrutiny_issues": scrutiny_res.issues,
                "hcfa_missing_fields": hcfa_res["missing_fields"],
                "physician_queries": hcfa_res["physician_queries"],
                "pipeline_log": pipeline_log
            }

        # STAGE 6: 837P Generation & Dispatch
        x12_837_content = self.x12_generator.generate_837p(encounter_dict)
        dispatch_res = self.dispatcher.dispatch_batch(x12_837_content, batch_id=f"BAT_{encounter_id}")
        pipeline_log.append(f"[Stage 6] 837P Transmitted to Office Ally SFTP. 999 Ack={dispatch_res['ack_999_functional']['status']}, 277CA={dispatch_res['ack_277ca_claim_level']['status']}")

        return {
            "status": "TRANSMITTED_CLEAN_CLAIM",
            "confidence_score": scrutiny_res.confidence_score,
            "encounter_id": encounter_id,
            "total_billed": billing.total_charge,
            "x12_837_snippet": x12_837_content.split("\n")[:10],
            "clearinghouse_dispatch": dispatch_res,
            "pipeline_log": pipeline_log
        }
