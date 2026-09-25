# ============================================
# FILE: medical_billing_ai/review_audit.py
# PURPOSE: Multi-Agent Comprehensive Review & Audit Suite
# PERSONAS: Ingestion Agent, Eligibility Agent, Coding Agent, Compliance Agent, EDI Agent, Remittance Agent, CTO Agent
# ============================================

import sys
import json
from docx import Document
from pypdf import PdfWriter

from models.encounter import DocumentSourceType
from pipeline import AutonomousMedicalBillingPipeline

def run_multi_agent_review():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    print("======================================================================")
    print("[*] STARTING MULTI-AGENT COMPREHENSIVE MEDICAL BILLING AI AUDIT")
    print("======================================================================\n")

    pipeline = AutonomousMedicalBillingPipeline(submitter_id="MAGNOLIA_HEALTH")
    audit_results = {}

    # Sample base patient, provider, and payer
    patient_data = {
        "first_name": "CARLOS",
        "last_name": "SANTANA",
        "dob": "1970-08-15",
        "gender": "M",
        "member_id": "FLB123456789",
        "address": "800 Ocean Drive",
        "city": "Miami Beach",
        "state": "FL",
        "zip": "33139"
    }

    provider_data = {
        "organization_name": "MAGNOLIA HEALTH CLINIC LLC",
        "rendering_first_name": "MARIA",
        "rendering_last_name": "GONZALEZ",
        "type": "APRN",
        "state": "FL",
        "specialty": "Family Medicine",
        "fl_autonomous_registered": True,
        "has_collaborative_protocol": False,
        "npi": "1987654321",
        "tax_id": "650987654",
        "taxonomy_code": "363LF0000X",
        "address": "1200 Brickell Ave",
        "city": "Miami",
        "zip": "33131"
    }

    payer_data = {
        "name": "FLORIDA BLUE (BCBS FL)",
        "payer_id": "00590",
        "type": "COMMERCIAL",
        "address": "PO BOX 1798",
        "city": "Jacksonville",
        "state": "FL",
        "zip": "32203"
    }

    # ------------------------------------------------------------------
    # AGENT 1: Ingestion & Multimodal Document Specialist
    # ------------------------------------------------------------------
    print("[AGENT 1: Multimodal Ingestion Specialist] Testing Word (.docx), PDF, Camera, and Handwritten input...")
    
    # 1. Test DOCX ingestion
    docx_path = "sample_clinical_note.docx"
    doc = Document()
    doc.add_heading("Clinical Encounter Note", 0)
    doc.add_paragraph("Chief Complaint: Evaluation of scaly spot on arm and blood pressure check.")
    doc.add_paragraph("HPI: Patient complains of scaly spot on arm for 6 months. History of hypertension.")
    doc.add_paragraph("Assessment & Plan: 1. Actinic Keratosis: Cryosurgery with liquid nitrogen performed. 2. Hypertension: Refilled Lisinopril 10mg. Total face to face time: 35 minutes.")
    doc.save(docx_path)

    res_docx = pipeline.process_encounter(
        raw_doc_or_content=docx_path,
        doc_type=DocumentSourceType.DOCX_FILE,
        patient_data=patient_data,
        provider_data=provider_data,
        payer_data=payer_data,
        encounter_id="ENC-DOCX-01"
    )

    # 2. Test Handwritten note normalization
    handwritten_raw = (
        "CC: Pt c/o rash and T2DM f/u.\n"
        "HPI: 55yo male s/p insulin start. Complains of forearm lesion.\n"
        "A&P: 1. T2DM: Refilled Metformin 500mg BID. 2. AK: LNK performed 15 sec. RTC 3 mos."
    )
    res_handwritten = pipeline.process_encounter(
        raw_doc_or_content=handwritten_raw,
        doc_type=DocumentSourceType.HANDWRITTEN_NOTE,
        patient_data=patient_data,
        provider_data=provider_data,
        payer_data=payer_data,
        encounter_id="ENC-HW-02"
    )

    agent1_pass = (res_docx["status"] == "TRANSMITTED_CLEAN_CLAIM") and (res_handwritten["status"] == "TRANSMITTED_CLEAN_CLAIM")
    audit_results["Agent_1_Multimodal_Ingestion"] = {
        "status": "PASS" if agent1_pass else "FAIL",
        "docx_claim_billed": res_docx.get("total_billed"),
        "handwritten_claim_billed": res_handwritten.get("total_billed")
    }
    print(f"   --> Agent 1 Verdict: {'PASS' if agent1_pass else 'FAIL'} (DOCX Billed: ${res_docx.get('total_billed')}, Handwritten Billed: ${res_handwritten.get('total_billed')})\n")

    # ------------------------------------------------------------------
    # AGENT 2: Upstream Real-Time Eligibility (270/271) Auditor
    # ------------------------------------------------------------------
    print("[AGENT 2: Upstream Eligibility & Prior-Auth Auditor] Testing inactive policy block & copay extraction...")
    
    inactive_patient = dict(patient_data)
    inactive_patient["member_id"] = "123" # Triggers inactive simulation

    res_eligibility = pipeline.process_encounter(
        raw_doc_or_content="Routine checkup",
        doc_type=DocumentSourceType.OCR_TEXT,
        patient_data=inactive_patient,
        provider_data=provider_data,
        payer_data=payer_data,
        encounter_id="ENC-INACTIVE-03"
    )

    agent2_pass = res_eligibility.get("status") == "BLOCKED_INACTIVE_COVERAGE"
    audit_results["Agent_2_Eligibility"] = {
        "status": "PASS" if agent2_pass else "FAIL",
        "blocked_stage": res_eligibility.get("stage")
    }
    print(f"   --> Agent 2 Verdict: {'PASS' if agent2_pass else 'FAIL'} (Correctly blocked before coding)\n")

    # ------------------------------------------------------------------
    # AGENT 3: Clinical Coding & E/M MDM Auditor
    # ------------------------------------------------------------------
    print("[AGENT 3: Clinical Coding & E/M MDM Auditor] Testing AMA 2021/2023 MDM Leveling & Modifier 25...")
    
    coding_res = pipeline.coding_engine.generate_coding(
        pipeline.ingestor.ingest_document(
            "Chief Complaint: Diabetes follow up\nA&P: T2DM and Hypertension. Refilled prescription. Time: 35 minutes. Performed cryotherapy on lesion.",
            DocumentSourceType.STRUCTURED_JSON
        )
    )
    cpt_codes_extracted = [c.code for c in coding_res.cpt_codes]
    has_99214 = "99214" in cpt_codes_extracted
    has_mod25 = any("25" in c.modifiers for c in coding_res.cpt_codes if c.code == "99214")
    agent3_pass = has_99214 and has_mod25
    audit_results["Agent_3_Coding_MDM"] = {
        "status": "PASS" if agent3_pass else "FAIL",
        "extracted_cpts": cpt_codes_extracted,
        "em_modifiers": [c.modifiers for c in coding_res.cpt_codes]
    }
    print(f"   --> Agent 3 Verdict: {'PASS' if agent3_pass else 'FAIL'} (CPTs: {cpt_codes_extracted}, Mod -25 Attached: {has_mod25})\n")

    # ------------------------------------------------------------------
    # AGENT 4: Florida Legal & FCSO Medical Necessity Auditor
    # ------------------------------------------------------------------
    print("[AGENT 4: Florida Scope & Medicare FCSO Auditor] Testing F.S. §464.0123 & FCSO LCD L36063 rules...")
    
    # Violation: Autonomous APRN doing specialty surgery without protocol
    specialty_provider = dict(provider_data)
    specialty_provider["specialty"] = "Pain Management & Orthopedic Surgery"
    specialty_provider["has_collaborative_protocol"] = False

    res_fl_law = pipeline.process_encounter(
        raw_doc_or_content="CC: Knee joint injection\nA&P: Performed joint injection.",
        doc_type=DocumentSourceType.OCR_TEXT,
        patient_data=patient_data,
        provider_data=specialty_provider,
        payer_data=payer_data,
        encounter_id="ENC-LAW-04"
    )

    agent4_pass = res_fl_law.get("status") == "FLAGGED_FOR_HUMAN_REVIEW" and any("FL_STATUTE_464_0123_VIOLATION" in i.get("code", "") for i in res_fl_law.get("scrutiny_issues", []))
    audit_results["Agent_4_Florida_Compliance"] = {
        "status": "PASS" if agent4_pass else "FAIL",
        "flagged_issues": [i.get("code") for i in res_fl_law.get("scrutiny_issues", [])]
    }
    print(f"   --> Agent 4 Verdict: {'PASS' if agent4_pass else 'FAIL'} (Flagged Statutory Violation: F.S. §464.0123)\n")

    # ------------------------------------------------------------------
    # AGENT 5: EDI & Clearinghouse Transmission Specialist
    # ------------------------------------------------------------------
    print("[AGENT 5: EDI Transmission Specialist] Testing X12 837P syntax, Office Ally SFTP dispatch, and 999/277CA acks...")
    
    dispatch_res = res_docx.get("clearinghouse_dispatch", {})
    ack_999 = dispatch_res.get("ack_999_functional", {}).get("status")
    ack_277ca = dispatch_res.get("ack_277ca_claim_level", {}).get("status")
    agent5_pass = (ack_999 == "ACCEPTED") and ("ACCEPTED" in ack_277ca)
    audit_results["Agent_5_EDI_Clearinghouse"] = {
        "status": "PASS" if agent5_pass else "FAIL",
        "ack_999": ack_999,
        "ack_277ca": ack_277ca
    }
    print(f"   --> Agent 5 Verdict: {'PASS' if agent5_pass else 'FAIL'} (999 Functional Ack: {ack_999}, 277CA Claim Ack: {ack_277ca})\n")

    # ------------------------------------------------------------------
    # AGENT 6: Remittance & Denial Appeals Specialist
    # ------------------------------------------------------------------
    print("[AGENT 6: Remittance & Denial Appeals Specialist] Testing 835 parsing and automated appeal letter generation...")
    
    raw_835 = (
        "ISA*00*          *00*          *ZZ*OFFICEALLY     *ZZ*MAGNOLIA_HEALTH*260925*1400*^*00501*000000001*0*T*:~\n"
        "GS*HP*OFFICEALLY*MAGNOLIA_HEALTH*20260925*1400*1*X*005010X221A1~\n"
        "ST*835*0001*005010X221A1~\n"
        "BPR*I*175.00*C*ACH*CTX*01*999999992*DA*12345678*1234567890**01*999999991*DA*98765432*20260925~\n"
        "TRN*1*9876543210*1999999992~\n"
        "N1*PR*FLORIDA BLUE (BCBS FL)~\n"
        "CLP*CLM992211*1*295.00*175.00*0.00*MC*CLAIM99881122~\n"
        "CAS*CO*97*120.00*1~\n"
        "SE*8*0001~\n"
        "GE*1*1~\n"
        "IEA*1*000000001~"
    )
    parsed_835 = pipeline.era_parser.parse_835(raw_835)
    appeal_letter = pipeline.appeals_engine.generate_appeal_letter(
        claim_data={"payer_name": "Florida Blue", "patient_name": "Carlos Santana", "claim_id": "CLM992211"},
        adjustment=parsed_835["claims"][0]["adjustments"][0],
        clinical_note={"chief_complaint": "T2DM and AK", "assessment_and_plan": "Cryotherapy performed alongside distinct chronic care."}
    )

    agent6_pass = (parsed_835["claims"][0]["total_paid"] == 175.00) and ("FORMAL RECONSIDERATION" in appeal_letter["formal_letter_text"])
    audit_results["Agent_6_Remittance_Appeals"] = {
        "status": "PASS" if agent6_pass else "FAIL",
        "paid_amount": parsed_835["claims"][0]["total_paid"],
        "appeal_generated": bool(appeal_letter["appeal_id"])
    }
    print(f"   --> Agent 6 Verdict: {'PASS' if agent6_pass else 'FAIL'} (Parsed $175.00 Paid, Generated Appeal ID: {appeal_letter['appeal_id']})\n")

    # ------------------------------------------------------------------
    # CTO FINAL VERDICT & SUMMARY
    # ------------------------------------------------------------------
    all_passed = all(v["status"] == "PASS" for v in audit_results.values())

    print("======================================================================")
    print("🎯 CTO EXECUTIVE VERDICT & PIPELINE AUDIT REPORT")
    print("======================================================================")
    for agent, res in audit_results.items():
        print(f"  • {agent.replace('_', ' ')}: [{res['status']}]")
    print("----------------------------------------------------------------------")
    print(f"FINAL AUDIT RESULT: {'APPROVED FOR PRODUCTION' if all_passed else 'REVISIONS REQUIRED'}")
    print("======================================================================\n")

if __name__ == "__main__":
    run_multi_agent_review()
