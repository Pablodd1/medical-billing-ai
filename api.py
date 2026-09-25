# ============================================
# FILE: medical_billing_ai/api.py
# PURPOSE: Production FastAPI REST API Server for Medical Billing & Autonomous Coding
# ============================================

import sys
import os
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Ensure root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models.encounter import DocumentSourceType
from pipeline import AutonomousMedicalBillingPipeline

app = FastAPI(
    title="Autonomous Medical Billing AI Engine",
    description="End-to-End Clinical NLP Coding, 5-Tier Scrutiny, Office Ally Clearinghouse Dispatch, & 835 Remittance Loop",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
        "version": "1.0.0",
        "clearinghouse": "Office Ally (SFTP / CAQH CORE API)",
        "docs_url": "/docs"
    }

@app.get("/health")
def health_check():
    return {"status": "HEALTHY"}

@app.post("/api/v1/encounter/process")
def process_encounter_endpoint(req: EncounterRequest):
    """
    Ingests encounter note, runs 270/271 eligibility, clinical coding,
    5-tier scrutiny, HCFA-1500 audit, generates 837P, and transmits to Office Ally.
    """
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
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/eligibility/check")
def check_eligibility_endpoint(req: EligibilityRequest):
    """
    Performs real-time 270/271 eligibility verification.
    """
    from models.encounter import PatientInfo, PayerInfo
    try:
        pat = PatientInfo(**req.patient)
        pyr = PayerInfo(**req.payer)
        return pipeline.eligibility_engine.check_eligibility(pat, pyr)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/v1/remittance/ingest-835")
def ingest_remittance_endpoint(req: RemittanceRequest):
    """
    Ingests 835 Remittance Advice (EOBs), reconciles payment,
    classifies CARC/RARC denials, and generates automated appeal letters.
    """
    try:
        parsed_835 = pipeline.era_parser.parse_835(req.raw_835_edi)
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

        return {
            "parsed_remittance": parsed_835,
            "appeals_generated": appeals
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
