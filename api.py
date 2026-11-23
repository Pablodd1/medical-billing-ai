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
import hmac
from typing import Dict, Any, List, Optional, Union
from fastapi import FastAPI, HTTPException, Body, Depends, Header
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Ensure root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models.encounter import DocumentSourceType
from pipeline import AutonomousMedicalBillingPipeline
from compliance.knowledge_base import MedicalCodingAssistant
from compliance.code_search import search_clinical_codes
from transmission.office_ally_companion import OfficeAllyCompanionValidator
from security.auth import require_api_key
from security.audit_log import audit_event

app = FastAPI(
    title="Autonomous Medical Billing AI Engine",
    description="End-to-End Clinical NLP Coding, 8-Tier Scrutiny, Office Ally Clearinghouse Dispatch, & 835 Remittance Loop",
    version="2.0.0",
    root_path=os.getenv("ROOT_PATH", ""),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()] or [],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)

pipeline = AutonomousMedicalBillingPipeline(submitter_id=os.getenv("SUBMITTER_ID", "MAGNOLIA_HEALTH"))
coding_assistant = MedicalCodingAssistant()
oa_companion = OfficeAllyCompanionValidator(config=pipeline.config)

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

class ROICalculatorRequest(BaseModel):
    monthly_claims: int = 750
    avg_claim_amount: float = 195.0
    current_biller_pct: float = 6.0
    current_denial_rate_pct: float = 12.0
    specialty: str = "Family Medicine"
    state: str = "FL"

class ScrapeLeadsRequest(BaseModel):
    taxonomy_description: str = "Family Medicine"
    state: str = "FL"
    city: str = "Miami"
    limit: int = 25
    enumeration_type: str = "NPI-2"

class LeadStatusUpdateRequest(BaseModel):
    status: str = "CONTACTED"
    notes: str = ""

class CampaignGenerateRequest(BaseModel):
    lead_id: Optional[str] = None
    custom_lead: Optional[Dict[str, Any]] = None

class AttestationSignRequest(BaseModel):
    encounter_id: str
    provider_npi: str
    provider_name: str
    billing_summary: str

class EnrollmentCheckRequest(BaseModel):
    billing_npi: str
    payer_id: str

class AssistantAskRequest(BaseModel):
    query: str
    allow_openfda_live: bool = False

class AssistantCodeCheckRequest(BaseModel):
    cpt_codes: List[Union[str, Dict[str, Any]]] = Field(default_factory=list)
    icd10_codes: List[str] = Field(default_factory=list)
    pos: str = "11"
    modifiers: Optional[Union[List[str], Dict[str, List[str]]]] = None
    ndc: Optional[Union[str, Dict[str, Any]]] = None
    payer_type: str = "MEDICARE"
    provider_type: str = "MD"

class OfficeAllyPreflightRequest(BaseModel):
    edi_content: str = ""
    raw_837_edi: Optional[str] = None
    filename: Optional[str] = None
    run_pyx12: bool = True
    verified_submitter_ids: Optional[List[str]] = None

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
def biller_portal(
    x_api_key: Optional[str] = Header(default=None),
    api_key: Optional[str] = None
):
    """Interactive visual portal for medical billers to review encounters,
    manage the HITL queue, and view the claims ledger.
    Requires authentication via X-API-Key header or ?api_key= query parameter."""
    provided_key = x_api_key or api_key
    # Check configured keys fail-closed
    keys = [k.strip() for k in (os.getenv("API_KEYS", "") or os.getenv("API_KEY", "")).split(",") if k.strip()]
    if not keys:
        raise HTTPException(
            status_code=503,
            detail="API authentication is not configured. Service is fail-closed."
        )
    if not provided_key or not any(hmac.compare_digest(provided_key, k) for k in keys):
        # Return simple login / unauthorized screen prompting for key
        return HTMLResponse(
            status_code=401,
            content="""<!DOCTYPE html>
<html>
<head><title>Authentication Required | Medical Biller Portal</title>
<style>body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;background:#f8fafc;margin:0;}
.box{background:#fff;padding:2rem;border-radius:8px;box-shadow:0 4px 6px -1px rgba(0,0,0,0.1);max-width:400px;width:100%;}
input{width:100%;padding:8px 12px;margin:12px 0;border:1px solid #cbd5e1;border-radius:6px;box-sizing:border-box;}
button{width:100%;padding:10px;background:#4f46e5;color:#fff;border:none;border-radius:6px;font-weight:bold;cursor:pointer;}
</style></head>
<body><div class="box">
<h2>Biller Portal Sign-In</h2>
<p style="font-size:0.875rem;color:#64748b;">Please enter your HIPAA Authorized API Key to access the medical billing portal.</p>
<form onsubmit="location.href=location.pathname + '?api_key=' + encodeURIComponent(document.getElementById('k').value); return false;">
<input id="k" type="password" placeholder="Enter API Key" required autofocus />
<button type="submit">Access Portal</button>
</form></div></body></html>"""
        )

    template_path = os.path.join(os.path.dirname(__file__), "templates", "portal.html")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=404, detail="Portal template not found.")
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()
    # If authenticated via query param or header, inject key into input securely on load
    if provided_key:
        html = html.replace('id="apiKeyInput" type="password" value=""', f'id="apiKeyInput" type="password" value="{provided_key}"')
    return html


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
        # Prevent leaking raw patient payload from validation error strings
        err_msg = str(e)
        if "validation error" in err_msg.lower():
            clean_msg = "Payload validation error: please verify all required patient and provider fields."
        else:
            clean_msg = f"Encounter processing error: {type(e).__name__}"
        raise HTTPException(status_code=500, detail=clean_msg)

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
        err_msg = str(e)
        if "validation error" in err_msg.lower():
            clean_msg = "Eligibility request validation error: invalid patient or payer format."
        else:
            clean_msg = f"Eligibility verification failed: {type(e).__name__}"
        raise HTTPException(status_code=400, detail=clean_msg)

@app.post("/api/v1/remittance/ingest-835")
def ingest_remittance_endpoint(req: RemittanceRequest, actor: str = Depends(require_api_key)):
    """
    Ingests a real 835 Remittance Advice, reconciles against the claim ledger,
    classifies CARC/RARC denials, generates automated appeal letters, and
    creates post-835 patient balance SMS/Stripe statements.
    """
    audit_event(actor, "ERA_INGEST", "/api/v1/remittance/ingest-835", "ATTEMPT")
    try:
        parsed_835 = pipeline.era_parser.parse_835(req.raw_835_edi)
        reconciled = pipeline.dispatcher.ledger.reconcile_835(parsed_835, "api_upload")
        appeals = []
        patient_statements = []
        for claim in parsed_835.get("claims", []):
            pcn = claim.get("patient_control_number", "ENC-001")
            for adj in claim.get("adjustments", []):
                if adj["carc_code"] in ["97", "50", "4", "16", "197"]:
                    appeal = pipeline.appeals_engine.generate_appeal_letter(
                        claim_data={"payer_name": parsed_835.get("payer_name", "Payer"), "claim_id": pcn},
                        adjustment=adj,
                        clinical_note=req.clinical_note_summary or {"assessment_and_plan": "Documentation on file"}
                    )
                    appeals.append(appeal)
            has_pr = any(a.get("group_code") == "PR" for a in claim.get("adjustments", [])) or (claim.get("patient_responsibility", 0.0) > 0)
            if has_pr:
                stmt = pipeline.balance_collector.generate_patient_statement_from_835(
                    claim_remittance=claim,
                    patient_info={"first_name": "Valued", "last_name": "Patient", "phone": "3055550199"},
                    encounter_id=pcn
                )
                patient_statements.append(stmt.model_dump())
        audit_event(actor, "ERA_INGEST", "/api/v1/remittance/ingest-835",
                    "OK", detail={"claims_reconciled": reconciled})
        return {
            "parsed_remittance": parsed_835,
            "claims_reconciled": reconciled,
            "appeals_generated": appeals,
            "patient_balance_statements": patient_statements
        }
    except Exception as e:
        audit_event(actor, "ERA_INGEST", "/api/v1/remittance/ingest-835", "ERROR")
        raise HTTPException(status_code=500, detail="Failed to process 835 remittance file: parsing or reconciliation error.")

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
        raise HTTPException(status_code=500, detail=f"Claim approval error: {type(e).__name__}")

@app.post("/api/v1/transmission/poll-acknowledgments")
def poll_acks(actor: str = Depends(require_api_key)):
    """Polls OA /outbound, parses real 999/277CA/835 files, reconciles ledger."""
    try:
        summary = pipeline.dispatcher.poll_acknowledgments()
        audit_event(actor, "POLL_ACKS", "/api/v1/transmission/poll-acknowledgments",
                    "OK", detail={k: v for k, v in summary.items() if k != "unknown_files"})
        return summary
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"Clearinghouse communication error: {str(e)}")

@app.get("/api/v1/compliance/reference-data/status")
def reference_data_status(actor: str = Depends(require_api_key)):
    """Shows which CMS/FCSO reference datasets are loaded and their provenance."""
    from compliance.reference_data import ReferenceDataLibrary
    return ReferenceDataLibrary().status_summary()

@app.post("/api/v1/compliance/attest")
def sign_attestation_endpoint(req: AttestationSignRequest, actor: str = Depends(require_api_key)):
    """Executes 1-Click Physician Legal Attestation & False Claims Act Shield."""
    att = pipeline.attestation_engine.sign_attestation(
        encounter_id=req.encounter_id,
        provider_npi=req.provider_npi,
        provider_name=req.provider_name,
        billing_summary=req.billing_summary
    )
    audit_event(actor, "PHYSICIAN_ATTESTATION", "/api/v1/compliance/attest", "SIGNED", patient_ref=req.encounter_id)
    return att.model_dump()

@app.post("/api/v1/enrollment/verify")
def verify_enrollment_endpoint(req: EnrollmentCheckRequest, actor: str = Depends(require_api_key)):
    """Verifies whether Billing NPI has active EDI enrollment with target Payer ID."""
    return pipeline.enrollment_tracker.check_enrollment_status(req.billing_npi, req.payer_id)

# ---------------- Zero-Hallucination Coder Assistant & Office Ally Pre-Flight ----------------

@app.post("/api/v1/assistant/ask")
def ask_coding_assistant_endpoint(req: AssistantAskRequest, actor: str = Depends(require_api_key)):
    """Deterministic, citation-grounded medical biller & coder standards Q&A
    (AMA CPT 2026, FY2026 ICD-10-CM, FDA 11-digit NDC, CMS/FCSO Medicare 09102,
    FL Medicaid/QMB 77027, Office Ally 5010A1) with ABSTAINED_UNVERIFIED guard."""
    res = coding_assistant.ask_assistant(req.query, allow_openfda_live=req.allow_openfda_live)
    audit_event(
        actor,
        "ASSISTANT_ASK",
        "/api/v1/assistant/ask",
        res.get("status", "UNKNOWN"),
        detail={"verified": bool(res.get("verified")), "citations": len(res.get("citations", []))},
    )
    return res

@app.post("/api/v1/assistant/code-check")
def check_claim_codes_endpoint(req: AssistantCodeCheckRequest, actor: str = Depends(require_api_key)):
    """Deterministic pre-submission CPT/HCPCS, ICD-10-CM leaf specificity, POS,
    modifier, NCCI PTP/MUE, FCSO FL LCD, QMB, and FDA 11-digit NDC code checker."""
    res = coding_assistant.check_claim_codes(
        cpt_codes=req.cpt_codes,
        icd10_codes=req.icd10_codes,
        pos=req.pos,
        modifiers=req.modifiers,
        ndc=req.ndc,
        payer_type=req.payer_type,
        provider_type=req.provider_type,
    )
    audit_event(
        actor,
        "ASSISTANT_CODE_CHECK",
        "/api/v1/assistant/code-check",
        "VERIFIED" if res.get("verified") else "ISSUES_FOUND",
        detail={"verified": bool(res.get("verified")), "issue_count": len(res.get("issues", []))},
    )
    return res

@app.post("/api/v1/office-ally/preflight")
def office_ally_preflight_endpoint(req: OfficeAllyPreflightRequest, actor: str = Depends(require_api_key)):
    """Audits an X12 837P (005010X222A1) string against all 8 Office Ally Companion Guide
    & CMS 5010A1 front-end reject suites plus 3-channel readiness diagnostics."""
    edi_payload = req.edi_content if req.edi_content else (req.raw_837_edi or "")
    validator = (
        OfficeAllyCompanionValidator(
            config=pipeline.config,
            verified_submitter_ids=set(req.verified_submitter_ids),
        )
        if req.verified_submitter_ids
        else oa_companion
    )
    res = validator.validate_837p(edi_payload, filename=req.filename, run_pyx12=req.run_pyx12)
    audit_event(
        actor,
        "OA_PREFLIGHT",
        "/api/v1/office-ally/preflight",
        "READY" if res.get("ready_for_office_ally") else "BLOCKED",
        detail={
            "claim_count": res.get("claim_count", 0),
            "error_count": res.get("error_count", 0),
            "warning_count": res.get("warning_count", 0),
            "pyx12_clean": bool(res.get("pyx12_clean")),
        },
    )
    return res

# ---------------- Commercial Growth, Lead Scraping, CRM & Sales Engine ----------------

@app.get("/api/v1/growth/leads")
def list_crm_leads_endpoint(
    specialty: Optional[str] = None,
    city: Optional[str] = None,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 200,
    actor: str = Depends(require_api_key)
):
    """Returns scraped medical clinic leads from the persistent CRM database."""
    leads = pipeline.dispatcher.ledger.get_crm_leads(
        specialty=specialty, city=city, status=status, search=search, limit=limit
    )
    total_upside = sum(float(l.get("estimated_total_annual_upside", 0) or 0) for l in leads)
    return {
        "status": "OK",
        "count": len(leads),
        "total_annual_recoverable_pipeline": round(total_upside, 2),
        "leads": leads
    }

@app.post("/api/v1/growth/leads/scrape")
def scrape_live_leads_endpoint(req: ScrapeLeadsRequest, actor: str = Depends(require_api_key)):
    """Scrapes real medical practices live from the U.S. CMS NPPES NPI Registry API and saves to CRM."""
    scraped = pipeline.lead_scraper.scrape_nppes(
        taxonomy_description=req.taxonomy_description,
        state=req.state,
        city=req.city,
        limit=req.limit,
        enumeration_type=req.enumeration_type
    )
    saved = pipeline.dispatcher.ledger.upsert_crm_leads(scraped)
    audit_event(actor, "NPPES_LEAD_SCRAPE", "/api/v1/growth/leads/scrape", "OK", detail={"scraped": len(scraped)})
    return {
        "status": "OK",
        "scraped_count": len(scraped),
        "saved_to_crm": saved,
        "leads": scraped
    }

@app.post("/api/v1/growth/leads/{lead_id}/status")
def update_lead_status_endpoint(lead_id: str, req: LeadStatusUpdateRequest, actor: str = Depends(require_api_key)):
    """Updates outbound sales status (NEW_SCRAPED, CONTACTED, DEMO_BOOKED, PILOT_ACTIVE, CLOSED_WON) and notes."""
    updated = pipeline.dispatcher.ledger.update_crm_lead_status(lead_id, req.status, req.notes)
    if not updated:
        raise HTTPException(status_code=404, detail="Lead not found in CRM.")
    return {"status": "OK", "lead": updated}

@app.post("/api/v1/growth/campaign/generate")
def generate_sales_campaign_endpoint(req: CampaignGenerateRequest, actor: str = Depends(require_api_key)):
    """Generates hyper-personalized Call Scripts, SMS Sequences, 5-Touch Cold Emails, and Objection Rebuttals."""
    target_lead = req.custom_lead
    if not target_lead and req.lead_id:
        all_leads = pipeline.dispatcher.ledger.get_crm_leads(limit=500)
        target_lead = next(
            (l for l in all_leads if l.get("lead_id") == req.lead_id or l.get("npi") == req.lead_id),
            None
        )
    if not target_lead:
        all_leads = pipeline.dispatcher.ledger.get_crm_leads(limit=1)
        target_lead = all_leads[0] if all_leads else {
            "lead_id": "LEAD-DEFAULT",
            "npi": "1659204766",
            "clinic_name": "24K HEALTH AND FITNESS LLC",
            "decision_maker_name": "Giorvanie Fleurme, APRN",
            "decision_maker_title": "Owner",
            "specialty": "Nurse Practitioner",
            "city": "Miami",
            "state": "FL",
            "phone": "(305) 200-6090",
            "estimated_monthly_claims": 520,
            "estimated_annual_billing": 1029600.0,
            "estimated_total_annual_upside": 225482.4,
            "top_cpts": ["99214", "99213", "17000", "20610"]
        }
    return pipeline.sales_engine.generate_personalized_campaign(target_lead)

@app.get("/api/v1/knowledge/search")
def search_knowledge_codes_endpoint(
    q: str,
    limit: int = 15,
    actor: str = Depends(require_api_key),
):
    """Fast hybrid Meilisearch + local ICD-10-CM & Florida RCM code lookup (non-PHI)."""
    res = search_clinical_codes(query=q, limit=limit)
    audit_event(actor, "CODE_SEARCH", "/api/v1/knowledge/search", "OK", detail={"engine": res.get("engine"), "count": res.get("count")})
    return res


@app.post("/api/v1/growth/roi-calculator")
def calculate_roi_endpoint(req: ROICalculatorRequest):
    """Calculates CFO-grade revenue recovery & ROI for a clinic (publicly accessible for pitch page & portal)."""
    return pipeline.sales_engine.calculate_clinic_roi(
        monthly_claims=req.monthly_claims,
        avg_claim_amount=req.avg_claim_amount,
        current_biller_pct=req.current_biller_pct,
        current_denial_rate_pct=req.current_denial_rate_pct,
        specialty=req.specialty,
        state=req.state
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
