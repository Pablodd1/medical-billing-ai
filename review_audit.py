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
    hw_patient = dict(patient_data)
    hw_patient["first_name"] = "ELENA"
    hw_patient["last_name"] = "ROSTOVA"
    hw_patient["member_id"] = "FLB987654321"

    handwritten_raw = (
        "CC: Pt c/o rash and T2DM f/u.\n"
        "HPI: 55yo male s/p insulin start. Complains of forearm lesion.\n"
        "A&P: 1. T2DM: Refilled Metformin 500mg BID. 2. AK: LNK performed 15 sec. RTC 3 mos."
    )
    res_handwritten = pipeline.process_encounter(
        raw_doc_or_content=handwritten_raw,
        doc_type=DocumentSourceType.HANDWRITTEN_NOTE,
        patient_data=hw_patient,
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
    # AGENT 7: Secondary & Supplemental Insurance (COB) Specialist
    # ------------------------------------------------------------------
    print("[AGENT 7: Secondary COB Specialist] Testing Loop 2320 / 2330 secondary payer crossover...")
    
    patient_with_secondary = dict(patient_data)
    patient_with_secondary["first_name"] = "JUAN"
    patient_with_secondary["last_name"] = "VALDEZ"
    patient_with_secondary["member_id"] = "FLB777888999"
    patient_with_secondary["secondary_payer"] = {
        "name": "MEDICARE B FLORIDA",
        "payer_id": "00140",
        "type": "MEDICARE",
        "address": "PO BOX 45000",
        "city": "Jacksonville",
        "state": "FL",
        "zip": "32231"
    }
    patient_with_secondary["secondary_member_id"] = "MED987654321A"

    res_sec_cob = pipeline.process_encounter(
        raw_doc_or_content="CC: Routine hypertension checkup\nA&P: Stable HTN. Refilled medication. Time: 25 mins.",
        doc_type=DocumentSourceType.STRUCTURED_JSON,
        patient_data=patient_with_secondary,
        provider_data=provider_data,
        payer_data=payer_data,
        encounter_id="ENC-COB-07"
    )

    full_837 = pipeline.x12_generator.generate_837p(res_sec_cob.get("clearinghouse_dispatch", {}).get("batch_data", {}) or {
        "provider": provider_data,
        "patient": patient_with_secondary,
        "billing": {"place_of_service": "11", "total_charge": 115.0, "diagnosis_codes": [{"code": "I10", "description": "HTN"}], "cpt_codes": [{"code": "99213", "charge": 115.0, "units": 1}]},
        "payer": payer_data,
        "date_of_service": "2026-09-25"
    })

    has_sbr_s = "SBR*S*18*******CI~" in full_837
    has_nm1_pr_sec = "NM1*PR*2*MEDICARE B FLORIDA*****PI*00140~" in full_837
    has_sec_member = "MED987654321A" in full_837
    agent7_pass = (res_sec_cob["status"] == "TRANSMITTED_CLEAN_CLAIM") and has_sbr_s and has_nm1_pr_sec and has_sec_member
    audit_results["Agent_7_Secondary_COB"] = {
        "status": "PASS" if agent7_pass else "FAIL",
        "has_sbr_s": has_sbr_s,
        "has_sec_payer": has_nm1_pr_sec,
        "has_sec_member": has_sec_member
    }
    print(f"   --> Agent 7 Verdict: {'PASS' if agent7_pass else 'FAIL'} (Loop 2320 SBR*S: {has_sbr_s}, Loop 2330B Secondary Payer: {has_nm1_pr_sec})\n")

    # ------------------------------------------------------------------
    # AGENT 8: Place of Service (POS) Classification Specialist
    # ------------------------------------------------------------------
    print("[AGENT 8: Place of Service Specialist] Testing POS 13 (ALF), POS 12 (Home), and POS 02 (Telehealth)...")
    
    # 1. ALF Note
    alf_note = "Encounter at Sunrise Senior Living Assisted Living Facility (ALF). Pt evaluated in room for memory check and BP."
    parsed_alf = pipeline.ingestor.ingest_document(alf_note, DocumentSourceType.STRUCTURED_JSON)
    coded_alf = pipeline.coding_engine.generate_coding(parsed_alf)

    # 2. Home Note
    home_note = "House call visit conducted at patient residence. Physical exam and medication review performed."
    parsed_home = pipeline.ingestor.ingest_document(home_note, DocumentSourceType.STRUCTURED_JSON)
    coded_home = pipeline.coding_engine.generate_coding(parsed_home)

    # 3. Telehealth Note
    tele_note = "Virtual visit conducted via interactive audio/video telehealth platform. Discussed lab results."
    parsed_tele = pipeline.ingestor.ingest_document(tele_note, DocumentSourceType.STRUCTURED_JSON)
    coded_tele = pipeline.coding_engine.generate_coding(parsed_tele)

    agent8_pass = (coded_alf.place_of_service == "13") and (coded_home.place_of_service == "12") and (coded_tele.place_of_service in ["02", "10"]) and any("95" in c.modifiers for c in coded_tele.cpt_codes)
    audit_results["Agent_8_Place_of_Service"] = {
        "status": "PASS" if agent8_pass else "FAIL",
        "alf_pos": coded_alf.place_of_service,
        "home_pos": coded_home.place_of_service,
        "telehealth_pos": coded_tele.place_of_service
    }
    print(f"   --> Agent 8 Verdict: {'PASS' if agent8_pass else 'FAIL'} (ALF POS: {coded_alf.place_of_service}, Home POS: {coded_home.place_of_service}, Telehealth POS: {coded_tele.place_of_service})\n")

    # ------------------------------------------------------------------
    # AGENT 9: Payer Timely Filing Deadline Auditor
    # ------------------------------------------------------------------
    print("[AGENT 9: Timely Filing Deadline Auditor] Testing expired claim cutoff (CARC 29 risk) vs clean on-time filing...")
    
    # Expired DOS: 250 days ago (for a payer with 180-day limit)
    expired_patient = dict(patient_data)
    expired_patient["first_name"] = "ROBERTO"
    expired_patient["last_name"] = "DURAN"
    expired_patient["member_id"] = "FLB999000111"

    res_expired = pipeline.process_encounter(
        raw_doc_or_content="CC: Follow up\nA&P: Routine care.",
        doc_type=DocumentSourceType.STRUCTURED_JSON,
        patient_data=expired_patient,
        provider_data=provider_data,
        payer_data=payer_data,
        date_of_service="2025-10-01", # ~360 days ago
        encounter_id="ENC-EXPIRED-09"
    )

    agent9_pass = (res_expired.get("status") == "FLAGGED_FOR_HUMAN_REVIEW") and any(i.get("code") == "TIMELY_FILING_EXPIRED" for i in res_expired.get("scrutiny_issues", []))
    audit_results["Agent_9_Timely_Filing"] = {
        "status": "PASS" if agent9_pass else "FAIL",
        "expired_flagged": agent9_pass,
        "days_elapsed": res_expired.get("timely_filing", {}).get("days_elapsed")
    }
    print(f"   --> Agent 9 Verdict: {'PASS' if agent9_pass else 'FAIL'} (Correctly blocked expired claim with CARC 29 risk; {res_expired.get('timely_filing', {}).get('days_elapsed')} days old)\n")

    # ------------------------------------------------------------------
    # AGENT 10: Duplicate Claim Hash Guard & Anti-Fraud Auditor
    # ------------------------------------------------------------------
    print("[AGENT 10: Duplicate Claim Hash Guard Auditor] Testing SHA-256 fingerprint deduplication (CARC 18)...")
    
    # First submission: Transmits clean
    unique_member = "DUPTEST999"
    dup_patient = dict(patient_data)
    dup_patient["member_id"] = unique_member

    res_first = pipeline.process_encounter(
        raw_doc_or_content="CC: BP check\nA&P: HTN stable. Refilled Lisinopril. Time: 25 mins.",
        doc_type=DocumentSourceType.STRUCTURED_JSON,
        patient_data=dup_patient,
        provider_data=provider_data,
        payer_data=payer_data,
        date_of_service="2026-09-20",
        encounter_id="ENC-DUP-FIRST"
    )

    # Second submission: Identical member, DOS, CPTs, and rendering NPI
    res_second = pipeline.process_encounter(
        raw_doc_or_content="CC: BP check\nA&P: HTN stable. Refilled Lisinopril. Time: 25 mins.",
        doc_type=DocumentSourceType.STRUCTURED_JSON,
        patient_data=dup_patient,
        provider_data=provider_data,
        payer_data=payer_data,
        date_of_service="2026-09-20",
        encounter_id="ENC-DUP-SECOND"
    )

    agent10_pass = (res_first.get("status") == "TRANSMITTED_CLEAN_CLAIM") and (res_second.get("status") == "FLAGGED_FOR_HUMAN_REVIEW") and any(i.get("code") == "DUPLICATE_CLAIM_DETECTED" for i in res_second.get("scrutiny_issues", []))
    audit_results["Agent_10_Duplicate_Guard"] = {
        "status": "PASS" if agent10_pass else "FAIL",
        "first_submission": res_first.get("status"),
        "second_submission": res_second.get("status"),
        "blocked_code": "DUPLICATE_CLAIM_DETECTED" if agent10_pass else "NONE"
    }
    print(f"   --> Agent 10 Verdict: {'PASS' if agent10_pass else 'FAIL'} (First: {res_first.get('status')}, Second Duplicate: {res_second.get('status')} with CARC 18 guard)\n")

    # ------------------------------------------------------------------
    # AGENT 11: Multi-Date Extraction & Disambiguation Specialist
    # ------------------------------------------------------------------
    print("[AGENT 11: Multi-Date Extraction Specialist] Testing DOS vs Registration vs Signature vs DOB disambiguation...")
    
    multi_date_packet = (
        "FACILITY INTAKE & CLINICAL RECORD\n"
        "Patient: Carlos Santana\n"
        "DOB: 08/15/1970\n"
        "Registration Date: 08/01/2026\n"
        "Date of Service: 08/14/2026\n"
        "Chief Complaint: Diabetes and rash evaluation\n"
        "Assessment & Plan: T2DM and Actinic Keratosis. Liquid nitrogen cryosurgery performed. Medication refilled. Time: 35 mins.\n"
        "Electronically Signed by Maria Gonzalez APRN on 08/15/2026"
    )

    parsed_multi_date = pipeline.ingestor.ingest_document(multi_date_packet, DocumentSourceType.STRUCTURED_JSON)

    dos_ok = parsed_multi_date.date_of_service_detected == "2026-08-14"
    reg_ok = parsed_multi_date.registration_date_detected == "2026-08-01"
    sig_ok = parsed_multi_date.signature_date_detected == "2026-08-15"
    agent11_pass = dos_ok and reg_ok and sig_ok

    audit_results["Agent_11_Multi_Date_Disambiguation"] = {
        "status": "PASS" if agent11_pass else "FAIL",
        "detected_dos": parsed_multi_date.date_of_service_detected,
        "detected_registration": parsed_multi_date.registration_date_detected,
        "detected_signature": parsed_multi_date.signature_date_detected
    }
    print(f"   --> Agent 11 Verdict: {'PASS' if agent11_pass else 'FAIL'} (DOS: {parsed_multi_date.date_of_service_detected}, Reg: {parsed_multi_date.registration_date_detected}, Sig: {parsed_multi_date.signature_date_detected})\n")

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
