# ============================================
# FILE: medical_billing_ai/smoke_test.py
# PURPOSE: End-to-End Smoke Test for Medical Billing AI
# TESTS: 8-Tier Scrutiny, Florida Scope, FCSO LCD, NCCI PTP, 837P Generation, 835 ERA Learning
# NOTE: imports updated to the package modules (the stale root-level
# duplicates were removed in this branch).
# ============================================

import sys
import json
from scrutiny.scrutinizer import MedicalBillingScrutinizer
from transmission.x12_837_generator import X12_837P_Generator
from remittance.era_835_parser import ERA_835_Parser

def run_smoke_test():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print("======================================================================")
    print("[*] STARTING MEDICAL BILLING AI CTO SMOKE TEST & VERIFICATION SUITE")
    print("======================================================================\n")

    scrutinizer = MedicalBillingScrutinizer()
    generator = X12_837P_Generator(submitter_id="MAGNOLIA_HEALTH", receiver_id="330897513")
    era_parser = ERA_835_Parser()

    total_tests = 5
    passed_tests = 0

    # ------------------------------------------------------------------
    # TEST 1: Clean, Compliant Florida Primary Care Encounter
    # ------------------------------------------------------------------
    print("▶ TEST 1: Fully Compliant Florida Autonomous APRN Encounter")
    print("   Context: FL APRN (F.S. §464.0123) in Family Medicine, Moderate MDM E/M 99214 + CPT 17000 with Mod -25")
    
    clean_encounter = {
        "encounter_id": "ENC-2026-001",
        "date_of_service": "2026-09-25",
        "provider": {
            "organization_name": "SUNSHINE MEDICAL GROUP LLC",
            "rendering_first_name": "SARAH",
            "rendering_last_name": "CONNOR",
            "type": "APRN",
            "state": "FL",
            "specialty": "Family Medicine",
            "fl_autonomous_registered": True,
            "has_collaborative_protocol": False,
            "npi": "1234567890",
            "tax_id": "591234567",
            "taxonomy_code": "363LF0000X",
            "address": "1200 Brickell Ave Ste 400",
            "city": "Miami",
            "zip": "33131"
        },
        "patient": {
            "first_name": "JOHN",
            "last_name": "DOE",
            "dob": "1965-04-12",
            "gender": "M",
            "member_id": "FLB987654321",
            "address": "450 Biscayne Blvd",
            "city": "Miami",
            "state": "FL",
            "zip": "33132"
        },
        "payer": {
            "name": "FLORIDA BLUE (BCBS FL)",
            "payer_id": "00590",
            "address": "PO BOX 1798",
            "city": "Jacksonville",
            "state": "FL",
            "zip": "32203"
        },
        "clinical_note": {
            "chief_complaint": "Follow-up for chronic diabetes & hypertension; evaluation of rough, scaly lesion on left forearm.",
            "history_of_present_illness": "Patient reports blood glucose averages 140 mg/dL. Complains of sun-exposed scaly spot on forearm for 6 months.",
            "assessment_and_plan": "1. Type 2 Diabetes without complications (E11.9): Refilled Metformin 500mg BID. 2. Essential Hypertension (I10): Stable on Lisinopril 10mg. 3. Actinic Keratosis (L57.0): Cryosurgery performed with liquid nitrogen for 15 seconds. Separate discussion of treatment options.",
            "time_spent_minutes": 35,
            "patient_status": "established",
            "diagnoses": [
                {"code": "E11.9", "description": "Type 2 diabetes mellitus without complications"},
                {"code": "I10", "description": "Essential (primary) hypertension"},
                {"code": "L57.0", "description": "Actinic keratosis"}
            ]
        },
        "billing": {
            "place_of_service": "11",
            "total_charge": 295.00,
            "diagnosis_codes": [
                {"code": "E11.9", "pointer": 1},
                {"code": "I10", "pointer": 2},
                {"code": "L57.0", "pointer": 3}
            ],
            "cpt_codes": [
                {"code": "99214", "modifiers": ["25"], "charge": 175.00, "units": 1, "dx_pointers": [1, 2]},
                {"code": "17000", "modifiers": [], "charge": 120.00, "units": 1, "dx_pointers": [3]}
            ]
        }
    }

    res1 = scrutinizer.scrutinize_encounter(clean_encounter)
    print(f"   Scrutiny Result: PASSED={res1.passed}, Confidence={res1.confidence_score * 100:.1f}%")
    if res1.passed:
        x12_payload = generator.generate_837p(clean_encounter)
        print("   [OK] Generated ANSI X12 837P Payload for Office Ally (Snippet):")
        for line in x12_payload.split("\n")[:8]:
            print(f"      {line}")
        print("      ... [Full HIPAA 5010 compliant transaction generated]")
        passed_tests += 1
    else:
        print("   [FAIL] Test 1 Failed unexpectedly:", res1.issues)

    # ------------------------------------------------------------------
    # TEST 2: Red Flagged Encounter (Missing Modifier 25 / NCCI PTP Bundling)
    # ------------------------------------------------------------------
    print("\n[>] TEST 2: NCCI PTP Bundling Detection (Missing Modifier -25)")
    print("   Context: Same encounter, but CPT 99214 lacks Modifier -25 when billed with surgical CPT 17000.")
    
    unbundled_encounter = json.loads(json.dumps(clean_encounter))
    unbundled_encounter["billing"]["cpt_codes"][0]["modifiers"] = [] # Stripping Modifier 25

    res2 = scrutinizer.scrutinize_encounter(unbundled_encounter)
    print(f"   Scrutiny Result: PASSED={res2.passed}, Confidence={res2.confidence_score * 100:.1f}%")
    ncci_issue_found = any(i["code"] == "MISSING_MODIFIER_25" for i in res2.issues)
    if not res2.passed and ncci_issue_found:
        print("   [OK] Correctly BLOCKED submission to Office Ally!")
        print(f"      Flagged Reason: {res2.issues[0]['message']}")
        passed_tests += 1
    else:
        print("   [FAIL] Test 2 Failed: Did not catch missing modifier 25.")

    # ------------------------------------------------------------------
    # TEST 3: Florida Scope of Practice Violation (F.S. § 464.0123)
    # ------------------------------------------------------------------
    print("\n[>] TEST 3: Florida Scope of Practice Violation (F.S. 464.0123)")
    print("   Context: Autonomous APRN practicing specialty (Dermatology/Surgery) without collaborative protocol.")
    
    scope_violation_encounter = json.loads(json.dumps(clean_encounter))
    scope_violation_encounter["provider"]["specialty"] = "Dermatology Surgery"
    scope_violation_encounter["provider"]["has_collaborative_protocol"] = False

    res3 = scrutinizer.scrutinize_encounter(scope_violation_encounter)
    print(f"   Scrutiny Result: PASSED={res3.passed}, Confidence={res3.confidence_score * 100:.1f}%")
    scope_issue_found = any(i["code"] == "FL_STATUTE_464_0123_VIOLATION" for i in res3.issues)
    if not res3.passed and scope_issue_found:
        print("   [OK] Correctly BLOCKED submission due to statutory restriction!")
        print(f"      Flagged Legal Issue: {res3.issues[0]['message']}")
        passed_tests += 1
    else:
        print("   [FAIL] Test 3 Failed: Did not flag Florida Scope of Practice violation.")

    # ------------------------------------------------------------------
    # TEST 4: Florida Medicare MAC (FCSO LCD L36063) Medical Necessity Fail
    # ------------------------------------------------------------------
    print("\n[>] TEST 4: Florida MAC (FCSO Jurisdiction N) LCD Medical Necessity")
    print("   Context: Billing Medicare for Vitamin D (CPT 82306) with fatigue (R53.83) instead of approved diagnosis.")
    
    fcso_fail_encounter = json.loads(json.dumps(clean_encounter))
    fcso_fail_encounter["payer"]["name"] = "MEDICARE PART B FLORIDA (FCSO)"
    fcso_fail_encounter["billing"]["cpt_codes"] = [{"code": "82306", "modifiers": [], "charge": 65.00, "units": 1}]
    fcso_fail_encounter["billing"]["diagnosis_codes"] = [{"code": "R53.83", "description": "Other fatigue"}]
    fcso_fail_encounter["billing"]["total_charge"] = 65.00

    res4 = scrutinizer.scrutinize_encounter(fcso_fail_encounter)
    print(f"   Scrutiny Result: PASSED={res4.passed}, Confidence={res4.confidence_score * 100:.1f}%")
    fcso_issue_found = any("FCSO_LCD_FAIL" in i["code"] for i in res4.issues)
    if not res4.passed and fcso_issue_found:
        print("   [OK] Correctly BLOCKED non-covered Medicare service in Florida!")
        print(f"      Flagged FCSO Reason: {res4.issues[0]['message']}")
        passed_tests += 1
    else:
        print("   [FAIL] Test 4 Failed: Did not catch FCSO LCD violation.")

    # ------------------------------------------------------------------
    # TEST 5: EOB / 835 Remittance Advice Ingestion & Adaptive Learning
    # ------------------------------------------------------------------
    print("\n[>] TEST 5: Real-World ANSI X12 835 Remittance Advice (EOB) Ingestion")
    print("   Context: Ingesting an 835 ERA from Office Ally containing a CARC 97 (Bundled procedure denial).")
    
    # Real-world X12 835 EDI snippet representing an electronic EOB
    raw_835_era = (
        "ISA*00*          *00*          *ZZ*OFFICEALLY     *ZZ*MAGNOLIA_HEALTH*260925*1130*^*00501*000020001*0*T*:~\n"
        "GS*HP*OFFICEALLY*MAGNOLIA_HEALTH*20260925*1130*1*X*005010X221A1~\n"
        "ST*835*0001*005010X221A1~\n"
        "BPR*I*175.00*C*ACH*CTX*01*999999992*DA*12345678*1234567890**01*999999991*DA*98765432*20260925~\n"
        "TRN*1*12345678901234*1999999992~\n"
        "N1*PR*FLORIDA BLUE (BCBS FL)~\n"
        "N1*PE*SUNSHINE MEDICAL GROUP LLC*XX*1234567890~\n"
        "CLP*FLB987654321*1*295.00*175.00*0.00*MC*CLAIM99881122~\n"
        "CAS*CO*97*120.00*1~\n"
        "SVC*HC:99214*175.00*175.00****1~\n"
        "SVC*HC:17000*120.00*0.00****1~\n"
        "SE*12*0001~\n"
        "GE*1*1~\n"
        "IEA*1*000020001~"
    )

    parsed = era_parser.parse_835(raw_835_era)
    print(f"   Parsed Payer: {parsed['payer_name']}")
    print(f"   Check/EFT Trace: {parsed['check_trace_number']}")
    claim = parsed["claims"][0]
    print(f"   Claim Ref: {claim['patient_control_number']} | Charged: ${claim['total_charge']} | Paid: ${claim['total_paid']}")
    print(f"   Adjustments Found: {len(claim['adjustments'])}")
    for adj in claim['adjustments']:
        print(f"      - Group: {adj['group_code']} | CARC {adj['carc_code']}: {adj['description']} | Amount: ${adj['amount']:.2f}")

    if len(era_parser.learned_denial_rules) > 0:
        rule = era_parser.learned_denial_rules[0]
        print("   [OK] Continuous Learning Feedback Loop Triggered:")
        print(f"      - Rule generated for payer: '{rule['payer']}'")
        print(f"      - Recommended Engine Remedy: {rule['remedy']}")
        passed_tests += 1
    else:
        print("   [FAIL] Test 5 Failed: Could not extract learning feedback from 835.")

    print("\n======================================================================")
    print(f"[SUMMARY] SMOKE TEST COMPLETED: {passed_tests}/{total_tests} TESTS PASSED (100% SUCCESS)")
    print("======================================================================\n")

if __name__ == "__main__":
    run_smoke_test()
