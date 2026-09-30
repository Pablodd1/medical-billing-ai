# ============================================
# FILE: medical_billing_ai/api.py
# PURPOSE: Production FastAPI REST API Server for Medical Billing & Autonomous Coding
#
# SECURITY (Phase 4):
#   - All endpoints except /health require X-API-Key (HIPAA access control,
#     fail-closed when API_KEYS is unset).
#   - Every PHI-bearing request is recorded in the HIPAA audit log
#     (security/audit_log.py) — identifiers hashed, never raw.
#   - CORS origins come from ALLOWED_ORIGINS env var (no '*' in production).
# ============================================

import sys
import os
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Body, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Ensure root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models.encounter import DocumentSourceType
from pipeline import AutonomousMedicalBillingPipeline
from security.auth import require_api_key
from security.audit_log import audit_event

app = FastAPI(
    title="Autonomous Medical Billing AI Engine",
    description="End-to-End Clinical NLP Coding, 8-Tier Scrutiny, Office Ally Clearinghouse Dispatch, & 835 Remittance Loop",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()] or [],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)

pipeline = AutonomousMedicalBillingPipeline(submitter_id=os.getenv("SUBMITTER_ID", "MAGNOLIA_HEALTH"))

class EncounterRequest(BaseModel):
    encounter_id: str
    date_of_service: str = "2026-09-25"
    document_content: str
    document_type: DocumentSourceType = DocumentSourceType.STRUCTURED_JSON
    is_telehealth: bool = False
    patient: Dict[str, Any]
    provider: Dict[str, Any]
    payer: Dict[str, Any]

class EligibilityRequest(BaseModel):
    patient: Dict[str, Any]
    payer: Dict[str, Any]

class RemittanceRequest(BaseModel):
    raw_835_edi: str
    clinical_note_summary: Optional[Dict[str, Any]] = None

@app.get("/")
def root():
    return {
        "service": "Autonomous Medical Billing AI Engine",
        "status": "OPERATIONAL",
        "version": "2.0.0",
        "clearinghouse": "Office Ally (SFTP, ftp10.officeally.com:22)",
        "docs_url": "/docs"
    }

@app.get("/health")
def health_check():
    return {"status": "HEALTHY"}

@app.post("/api/v1/encounter/process")
def process_encounter_endpoint(req: EncounterRequest, actor: str = Depends(require_api_key)):
    """
    Ingests encounter note, runs 270/271 eligibility, clinical coding,
    8-tier scrutiny, HCFA-1500 audit, generates 837P, and transmits to Office Ally.
    """
    audit_event(actor, "ENCOUNTER_PROCESS", "/api/v1/encounter/process",
                "ATTEMPT", patient_ref=req.encounter_id)
    try:
        result = pipeline.process_encounter(
            raw_doc_or_content=req.document_content,
            doc_type=req.document_type,
            patient_data=req.patient,
            provider_data=req.provider,
            payer_data=req.payer,
            encounter_id=req.encounter_id,
            date_of_service=req.date_of_service,
            is_telehealth=req.is_telehealth
        )
        audit_event(actor, "ENCOUNTER_PROCESS", "/api/v1/encounter/process",
                    result.get("status", "UNKNOWN"), patient_ref=req.encounter_id)
        return result
    except Exception as e:
        audit_event(actor, "ENCOUNTER_PROCESS", "/api/v1/encounter/process",
                    "ERROR", patient_ref=req.encounter_id)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/eligibility/check")
def check_eligibility_endpoint(req: EligibilityRequest, actor: str = Depends(require_api_key)):
    """Performs 270/271 eligibility verification (real X12 via OA when
    SFTP credentials are configured; clearly-labelled simulation otherwise)."""
    from models.encounter import PatientInfo, PayerInfo
    try:
        pat = PatientInfo(**req.patient)
        pyr = PayerInfo(**req.payer)
        result = pipeline.eligibility_engine.check_eligibility(pat, pyr)
        audit_event(actor, "ELIGIBILITY_CHECK", "/api/v1/eligibility/check",
                    "SIMULATION" if result.get("simulation") else "SUBMITTED",
                    patient_ref=pat.member_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/v1/remittance/ingest-835")
def ingest_remittance_endpoint(req: RemittanceRequest, actor: str = Depends(require_api_key)):
    """
    Ingests a real 835 Remittance Advice, reconciles against the claim ledger,
    classifies CARC/RARC denials, and generates automated appeal letters.
    """
    audit_event(actor, "ERA_INGEST", "/api/v1/remittance/ingest-835", "ATTEMPT")
    try:
        parsed_835 = pipeline.era_parser.parse_835(req.raw_835_edi)
        reconciled = pipeline.dispatcher.ledger.reconcile_835(parsed_835, "api_upload")
        appeals = []
        for claim in parsed_835.get("claims", []):
            for adj in claim.get("adjustments", []):
                if adj["carc_code"] in ["97", "50", "4", "16", "197"]:
                    appeal = pipeline.appeals_engine.generate_appeal_letter(
                        claim_data={"payer_name": parsed_835.get("payer_name", "Payer"), "claim_id": claim.get("patient_control_number")},
                        adjustment=adj,
                        clinical_note=req.clinical_note_summary or {"assessment_and_plan": "Documentation on file"}
                    )
                    appeals.append(appeal)
        audit_event(actor, "ERA_INGEST", "/api/v1/remittance/ingest-835",
                    "OK", detail={"claims_reconciled": reconciled})
        return {
            "parsed_remittance": parsed_835,
            "claims_reconciled": reconciled,
            "appeals_generated": appeals
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ---------------- Phase 2/4 operational endpoints ----------------

@app.get("/api/v1/claims/{patient_control_number}")
def get_claim_status(patient_control_number: str, actor: str = Depends(require_api_key)):
    """Provable lifecycle status of one claim from the ledger."""
    claim = pipeline.dispatcher.ledger.get_claim(patient_control_number)
    audit_event(actor, "CLAIM_STATUS_READ", "/api/v1/claims/{pcn}",
                "FOUND" if claim else "NOT_FOUND", patient_ref=patient_control_number)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found in ledger.")
    return claim

@app.get("/api/v1/claims/aging/worklist")
def aging_worklist(actor: str = Depends(require_api_key)):
    """Daily follow-up worklist: claims stuck in non-final states."""
    audit_event(actor, "AGING_READ", "/api/v1/claims/aging/worklist", "OK")
    return pipeline.dispatcher.ledger.aging_report()

@app.post("/api/v1/transmission/poll-acknowledgments")
def poll_acks(actor: str = Depends(require_api_key)):
    """Polls OA /outbound, parses real 999/277CA/835 files, reconciles ledger."""
    try:
        summary = pipeline.dispatcher.poll_acknowledgments()
        audit_event(actor, "POLL_ACKS", "/api/v1/transmission/poll-acknowledgments",
                    "OK", detail={k: v for k, v in summary.items() if k != "unknown_files"})
        return summary
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

@app.get("/api/v1/compliance/reference-data/status")
def reference_data_status(actor: str = Depends(require_api_key)):
    """Shows which CMS/FCSO reference datasets are loaded and their provenance."""
    from compliance.reference_data import ReferenceDataLibrary
    return ReferenceDataLibrary().status_summary()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
