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
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
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

class ApprovalRequest(BaseModel):
    resolution_notes: str = "Biller review approved; supervisory protocol/attestation verified"
    resolved_by: str = "BILLER"

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

@app.get("/portal", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def biller_portal():
    """Interactive visual portal for medical billers to review encounters,
    manage the HITL queue, and view the claims ledger."""
    template_path = os.path.join(os.path.dirname(__file__), "templates", "portal.html")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Portal template not found.")
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt():
    """Search engine discovery and access rules."""
    return "User-agent: *\nAllow: /\nAllow: /portal\nAllow: /docs\nAllow: /categories\nAllow: /products\nAllow: /contact\nDisallow: /api/\nSitemap: /sitemap.xml\n"

@app.get("/sitemap.xml")
def sitemap_xml():
    """XML sitemap index for search crawlers."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '  <url><loc>/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>\n'
        '  <url><loc>/portal</loc><changefreq>daily</changefreq><priority>0.9</priority></url>\n'
        '  <url><loc>/categories</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>\n'
        '  <url><loc>/products</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>\n'
        '  <url><loc>/contact</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>\n'
        '</urlset>'
    )
    return Response(content=xml, media_type="application/xml")

@app.get("/categories")
def categories():
    """Clinical specialty & provider classification catalog."""
    return {
        "status": "OPERATIONAL",
        "categories": [
            {"id": "pt-rehab", "name": "Physical Therapy & Rehabilitation", "specialty": "Physical Medicine"},
            {"id": "aprn-autonomous", "name": "Autonomous APRN Practice", "specialty": "Family & Primary Care"},
            {"id": "orthopedics", "name": "Orthopedics & Sports Medicine", "specialty": "Surgical & Conservative"}
        ]
    }

@app.get("/products")
def products():
    """Billing engine core capabilities and product modules."""
    return {
        "status": "OPERATIONAL",
        "products": [
            {"id": "clinical-coding-nlp", "name": "Clinical NLP Coding Engine", "version": "2.0"},
            {"id": "scrutiny-8tier", "name": "8-Tier Clinical Scrutiny & CDI", "version": "2.0"},
            {"id": "oa-transmission", "name": "Office Ally SFTP Clearinghouse Pipeline", "version": "2.0"},
            {"id": "remittance-835", "name": "835 ERA Automated Ledger Reconciliation", "version": "2.0"}
        ]
    }

@app.get("/contact")
def contact():
    """Medical billing engine support and HIPAA compliance contact."""
    return {
        "organization": "Autonomous Medical Billing AI Engine",
        "compliance_officer": "compliance@medicalbilling.local",
        "support_email": "support@medicalbilling.local",
        "status": "OPERATIONAL"
    }

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

# ---------------- Operational & HITL Review Endpoints ----------------

@app.get("/api/v1/claims/aging/worklist")
def aging_worklist(actor: str = Depends(require_api_key)):
    """Daily follow-up worklist: claims stuck in non-final states."""
    audit_event(actor, "AGING_READ", "/api/v1/claims/aging/worklist", "OK")
    return pipeline.dispatcher.ledger.aging_report()

@app.get("/api/v1/claims/review-queue")
def get_review_queue_endpoint(status: str = "PENDING_REVIEW", actor: str = Depends(require_api_key)):
    """
    HITL Review Queue: Lists all claims flagged by 8-Tier Scrutiny or HCFA Auditor
    awaiting medical biller inspection and resolution.
    """
    items = pipeline.dispatcher.ledger.get_review_queue(status)
    audit_event(actor, "REVIEW_QUEUE_READ", "/api/v1/claims/review-queue", "OK", detail={"count": len(items)})
    return {"status": "OK", "count": len(items), "queue": items}

@app.get("/api/v1/claims/review-queue/{patient_control_number}")
def get_review_item_endpoint(patient_control_number: str, actor: str = Depends(require_api_key)):
    """
    HITL Review Detail: Returns full encounter payload, scrutiny issues, and CDI queries for a flagged claim.
    """
    item = pipeline.dispatcher.ledger.get_review_item(patient_control_number)
    audit_event(actor, "REVIEW_ITEM_READ", "/api/v1/claims/review-queue/{pcn}",
                "FOUND" if item else "NOT_FOUND", patient_ref=patient_control_number)
    if not item:
        raise HTTPException(status_code=404, detail="Claim not found in review queue.")
    return item

@app.get("/api/v1/claims/{patient_control_number}")
def get_claim_status(patient_control_number: str, actor: str = Depends(require_api_key)):
    """Provable lifecycle status of one claim from the ledger."""
    claim = pipeline.dispatcher.ledger.get_claim(patient_control_number)
    audit_event(actor, "CLAIM_STATUS_READ", "/api/v1/claims/{pcn}",
                "FOUND" if claim else "NOT_FOUND", patient_ref=patient_control_number)
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found in ledger.")
    return claim

@app.post("/api/v1/claims/{patient_control_number}/approve-and-transmit")
def approve_and_transmit_endpoint(
    patient_control_number: str,
    req: ApprovalRequest = Body(default_factory=ApprovalRequest),
    actor: str = Depends(require_api_key)
):
    """
    HITL 1-Click Approval: Medical biller reviews flagged claim, provides resolution notes,
    and dispatches the approved 837P EDI to Office Ally.
    """
    audit_event(actor, "CLAIM_APPROVE_TRANSMIT", "/api/v1/claims/{pcn}/approve-and-transmit",
                "ATTEMPT", patient_ref=patient_control_number)
    try:
        res = pipeline.approve_and_transmit_claim(
            patient_control_number=patient_control_number,
            resolution_notes=req.resolution_notes,
            resolved_by=f"{actor}:{req.resolved_by}"
        )
        audit_event(actor, "CLAIM_APPROVE_TRANSMIT", "/api/v1/claims/{pcn}/approve-and-transmit",
                    "SUCCESS", patient_ref=patient_control_number)
        return res
    except ValueError as e:
        audit_event(actor, "CLAIM_APPROVE_TRANSMIT", "/api/v1/claims/{pcn}/approve-and-transmit",
                    "REJECTED", patient_ref=patient_control_number)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        audit_event(actor, "CLAIM_APPROVE_TRANSMIT", "/api/v1/claims/{pcn}/approve-and-transmit",
                    "ERROR", patient_ref=patient_control_number)
        raise HTTPException(status_code=500, detail=str(e))

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
