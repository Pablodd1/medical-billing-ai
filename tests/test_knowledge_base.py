import io
import json
import os
import unittest
from unittest.mock import patch

from coding.clinical_coder import ClinicalCodingEngine
from compliance.claim_gate import (
    canonical_payer,
    coder_review_notes,
    hard_gate_issues,
    valid_mbi,
)
from compliance.knowledge_base import (
    MedicalCodingAssistant,
    valid_fl_medicaid_id,
)
from models.encounter import ClinicalNoteParsed
from scrutiny.hcfa_auditor import HCFA1500ExtractorAndAuditor
from scrutiny.scrutinizer import (
    MedicalBillingScrutinizer,
    normalize_fda_ndc_11,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOV26_DIR = os.path.join(REPO_ROOT, "data", "november_2026_0")


class TestMedicareMbiAndSouthFloridaPlans(unittest.TestCase):
    def test_valid_mbi_accepts_strict_cms_11_char_format(self):
        for good in ("1EG4TE5MK72", "8XC3F49DT93", "6A78JM8VE39", "3RC8KT2WA45", "9H04KP7MN12"):
            with self.subTest(mbi=good):
                self.assertTrue(valid_mbi(good))
                self.assertTrue(valid_mbi(good.lower()))

    def test_valid_mbi_rejects_pos1_zero_and_letters(self):
        # Pos 1 must be 1-9 (catches OCR 8->B in DICKISION and 6->S in YUEN)
        for bad in ("0EG4TE5MK72", "BXC3F49DT93", "SA78JM8VE39", "AEG4TE5MK72"):
            with self.subTest(mbi=bad):
                self.assertFalse(valid_mbi(bad))

    def test_valid_mbi_rejects_cms_banned_letters_s_l_o_i_b_z_in_all_alpha_slots(self):
        for banned in ("S", "L", "O", "I", "B", "Z"):
            # Pos 2, Pos 3, Pos 5, Pos 6, Pos 8, Pos 9
            candidates = [
                f"1{banned}G4TE5MK72",
                f"1E{banned}4TE5MK72",
                f"1EG4{banned}E5MK72",
                f"1EG4T{banned}5MK72",
                f"1EG4TE5{banned}K72",
                f"1EG4TE5M{banned}72",
            ]
            for bad in candidates:
                with self.subTest(mbi=bad):
                    self.assertFalse(valid_mbi(bad))

    def test_valid_mbi_rejects_wrong_slot_types_hyphens_and_lengths(self):
        bad_cases = [
            "12G4TE5MK72",      # Pos 2 digit (must be alpha)
            "1EGATE5MK72",      # Pos 4 alpha (must be digit)
            "1EG43E5MK72",      # Pos 5 digit (must be alpha)
            "1EG4TEAMK72",      # Pos 7 alpha (must be digit)
            "1EG4TE53K72",      # Pos 8 digit (must be alpha)
            "1EG4TE5M372",      # Pos 9 digit (must be alpha)
            "1EG4TE5MKA2",      # Pos 10 alpha (must be digit)
            "1EG4TE5MK7A",      # Pos 11 alpha (must be digit)
            "1EG4-TE5-MK72",    # Hyphens forbidden in X12 NM109
            "1EG4TE5MK7",       # 10 chars
            "1EG4TE5MK729",     # 12 chars
            "",
            None,
        ]
        for bad in bad_cases:
            with self.subTest(mbi=bad):
                self.assertFalse(valid_mbi(bad))

    def test_hard_gate_flags_invalid_mbi_format_on_ocr_confusions(self):
        base_demo = {
            "patient_name": "DOE, JANE",
            "last_name": "DOE",
            "first_name": "JANE",
            "gender": "F",
            "address": "100 MAIN ST",
            "city": "MIAMI",
            "state": "FL",
            "zip": "33101",
            "secondary_payer": None,
            "rendering_npi": "1598766495",
        }
        # Valid MBI passes
        ok_issues = hard_gate_issues({**base_demo, "member_id": "8XC3F49DT93"}, "DOE JANE.pdf", {})
        self.assertEqual(ok_issues, [])

        # OCR confusions (8->B, 0->O, 1->I, 5->S, 2->Z) are blocked
        for corrupted in ("BXC3F49DT93", "8XO3F49DT93", "8XC3I49DT93", "8XC3F49ST93", "8XC3F49DZ93"):
            issues = hard_gate_issues({**base_demo, "member_id": corrupted}, "DOE JANE.pdf", {})
            codes = {i["code"] for i in issues}
            self.assertIn("INVALID_MBI_FORMAT", codes)

    def test_south_florida_ma_and_mma_plans_detected_without_false_medicare_match(self):
        expected_matches = {
            "PREFERREDCAREPARTNERS Medicare Advantage Plan": "PREFERRED CARE PARTNERS",
            "Secondary: Preferred Care Partners HMO": "PREFERRED CARE PARTNERS",
            "CarePlus Health Plans HMO": "CAREPLUS HEALTH PLANS",
            "CARE PLUS MEDICARE": "CAREPLUS HEALTH PLANS",
            "AvMed Medicare": "AVMED",
            "Leon Health HMO": "LEON HEALTH",
            "Medica HealthCare Plans": "MEDICA HEALTHCARE",
            "Plan: MEDICA": "MEDICA HEALTHCARE",
            "Doctors HealthCare Plans": "DOCTORS HEALTHCARE PLANS",
            "Alignment Health Plan": "ALIGNMENT HEALTH PLAN",
            "Clover Health PPO": "CLOVER HEALTH",
            "Oscar Health": "OSCAR HEALTH",
            "Ambetter from Sunshine Health": "SUNSHINE HEALTH",
            "Ambetter Marketplace": "AMBETTER",
            "Simply Healthcare LTC": "SIMPLY HEALTHCARE",
            "Molina Healthcare of FL": "MOLINA HEALTHCARE",
            "Devoted Health": "DEVOTED HEALTH",
            "WellCare": "WELLCARE",
        }
        for raw, canonical in expected_matches.items():
            with self.subTest(raw=raw):
                self.assertEqual(canonical_payer(raw), canonical)

        # Must NEVER false-match Original Medicare, FL Medicaid FFS, or OCR 'Medicald'
        for non_ma in ("MEDICARE PART B FL", "FLORIDA MEDICAID", "Medicald LTC Pollcy", "Self Pay"):
            with self.subTest(non_ma=non_ma):
                self.assertIsNone(canonical_payer(non_ma))


class TestInitialVsSubsequentSnfCoding(unittest.TestCase):
    def setUp(self):
        self.coder = ClinicalCodingEngine()

    def _snf_note(self, cc: str = "", hpi: str = "", ap: str = "", raw: str = "", minutes: int = 0) -> ClinicalNoteParsed:
        return ClinicalNoteParsed(
            raw_text=raw or f"{cc} {hpi} {ap}",
            chief_complaint=cc,
            history_of_present_illness=hpi,
            review_of_systems="10-point ROS negative except musculoskeletal weakness.",
            physical_exam="Alert, frail elderly resident in SNF bed.",
            assessment_and_plan=ap,
            time_spent_minutes=minutes,
            place_of_service_detected="31",
        )

    def test_initial_snf_consultation_time_thresholds_99304_to_99306(self):
        base_kwargs = {
            "cc": "Rehab. Medicine Consultation Report",
            "hpi": "Patient was seen for rehab. medicine evaluation and treatment courtesy of Dr. Attending.",
            "ap": "1. Generalized muscle weakness. Continue PT/OT in SNF.",
        }
        r_25 = self.coder.generate_coding(self._snf_note(**base_kwargs, minutes=25))
        r_35 = self.coder.generate_coding(self._snf_note(**base_kwargs, minutes=35))
        r_45 = self.coder.generate_coding(self._snf_note(**base_kwargs, minutes=45))
        self.assertEqual(r_25.cpt_codes[0].code, "99304")
        self.assertEqual(r_35.cpt_codes[0].code, "99305")
        self.assertEqual(r_45.cpt_codes[0].code, "99306")

    def test_initial_snf_heuristic_caps_at_99305_without_time(self):
        # 4 chronic/acute diagnoses without time -> 99305 (never guesses 99306 without >=45m or explicit code)
        note_multi = self._snf_note(
            cc="Rehab. Medicine Consultation Report",
            ap="1. Hypertension. 2. Type 2 diabetes mellitus. 3. Muscle weakness. 4. Osteoarthritis of knee.",
        )
        res_multi = self.coder.generate_coding(note_multi)
        self.assertEqual(res_multi.cpt_codes[0].code, "99305")

        # 1 diagnosis without time -> 99304
        note_single = self._snf_note(
            cc="Initial SNF Evaluation",
            ap="1. Essential hypertension.",
        )
        res_single = self.coder.generate_coding(note_single)
        self.assertEqual(res_single.cpt_codes[0].code, "99304")

    def test_subsequent_snf_followup_uses_99307_to_99310(self):
        followup_30 = self._snf_note(
            cc="Subsequent Nursing Facility Visit",
            hpi="Routine follow-up SNF visit for ongoing rehab progress.",
            ap="1. Hypertension. 2. Muscle weakness improving.",
            minutes=30,
        )
        self.assertEqual(self.coder.generate_coding(followup_30).cpt_codes[0].code, "99309")

        followup_no_time = self._snf_note(
            cc="Routine follow-up visit",
            ap="1. Hypertension.",
            minutes=0,
        )
        self.assertEqual(self.coder.generate_coding(followup_no_time).cpt_codes[0].code, "99308")

    def test_real_november_2026_consult_ocr_files_score_as_initial_snf_99304_99305(self):
        if not os.path.isdir(NOV26_DIR):
            self.skipTest("november_2026_0 OCR fixtures not present")
        # Verify ALL 32 clinical consult charts in november_2026_0 (excluding MISC-DATE rounding card)
        ocr_files = [
            f for f in sorted(os.listdir(NOV26_DIR))
            if f.endswith(".pdf.ocr.txt") and not f.startswith("MISC-DATE")
        ]
        self.assertGreaterEqual(len(ocr_files), 30)
        for fname in ocr_files:
            fpath = os.path.join(NOV26_DIR, fname)
            with open(fpath, "r", encoding="utf-8") as fh:
                raw_ocr = fh.read()
            note = self._snf_note(raw=raw_ocr, ap=raw_ocr)
            coded = self.coder.generate_coding(note)
            with self.subTest(file=fname):
                self.assertIn(coded.cpt_codes[0].code, ("99304", "99305"))
                notes = coder_review_notes(raw_ocr, ["I10", "M62.81"], {"place_of_service": "31"})
                em_notes = [n for n in notes if n["code"] == "EM_LEVEL_HEURISTIC"]
                self.assertEqual(len(em_notes), 1)
                self.assertIn("99304-99306", em_notes[0]["message"])

    def test_explicit_snf_em_code_is_not_duplicated_as_procedure_line(self):
        note = self._snf_note(
            cc="Initial SNF Consultation",
            ap="1. Hypertension. 2. Muscle weakness. Billed CPT: 99305 performed today.",
        )
        coded = self.coder.generate_coding(note)
        self.assertEqual(coded.cpt_codes[0].code, "99305")
        self.assertEqual(len(coded.cpt_codes), 1)

    def test_osteoarthritis_anatomical_laterality_no_knee_hallucination(self):
        cases = [
            ("h/o Osteoarthritis of left hip", ["M16.12"]),
            ("Right hip osteoarthritis worsening", ["M16.11"]),
            ("Bilateral hip osteoarthritis", ["M16.0"]),
            ("History of hip osteoarthritis", ["M16.9"]),
            ("Right knee osteoarthritis", ["M17.11"]),
            ("Osteoarthritis of left knee", ["M17.12"]),
            ("Bilateral knee osteoarthritis", ["M17.0"]),
            ("Severe osteoarthritis knee. Plan: Osteoarthritis management.", ["M17.9"]),
            ("PMH: Osteoarthritis.", ["M19.90"]),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                codes = [d.code for d in self.coder._extract_diagnoses(text)]
                self.assertEqual(codes, expected)

        # Verify PROCEDURE_DX_AFFINITY["20610"] points to M16.*/M17.*/M19.90 over I10
        for oa_phrase, expected_dx in (
            ("Osteoarthritis of left hip", "M16.12"),
            ("Osteoarthritis", "M19.90"),
        ):
            note = self._snf_note(
                cc="Joint pain",
                hpi=f"Hypertension and {oa_phrase}.",
                ap="Performed joint injection today.",
            )
            billing = self.coder.generate_coding(note)
            dx_by_ptr = {d.pointer: d.code for d in billing.diagnosis_codes}
            inj_line = [ln for ln in billing.cpt_codes if ln.code == "20610"][0]
            self.assertEqual(dx_by_ptr[inj_line.dx_pointers[0]], expected_dx)

        # Live NOV26_0 charts: ISSERMAN CARLA I -> M16.12 (no M17.0/M19.90); LEDO CARMEN -> M19.90 (no M17.0)
        if os.path.isdir(NOV26_DIR):
            isserman_path = os.path.join(NOV26_DIR, "ISSERMAN CARLA I.pdf.ocr.txt")
            if os.path.exists(isserman_path):
                with open(isserman_path, "r", encoding="utf-8") as fh:
                    iss_codes = [d.code for d in self.coder._extract_diagnoses(fh.read())]
                self.assertIn("M16.12", iss_codes)
                self.assertNotIn("M17.0", iss_codes)
                self.assertNotIn("M19.90", iss_codes)

            ledo_path = os.path.join(NOV26_DIR, "LEDO CARMEN.pdf.ocr.txt")
            if os.path.exists(ledo_path):
                with open(ledo_path, "r", encoding="utf-8") as fh:
                    ledo_codes = [d.code for d in self.coder._extract_diagnoses(fh.read())]
                self.assertIn("M19.90", ledo_codes)
                self.assertNotIn("M17.0", ledo_codes)


class TestFdaNdcScrutinizerAndHcfaBoxes(unittest.TestCase):
    def test_normalize_fda_ndc_11_all_fda_segment_formats(self):
        self.assertEqual(normalize_fda_ndc_11("00002-7510-01"), "00002751001")  # 5-4-2
        self.assertEqual(normalize_fda_ndc_11("00002751001"), "00002751001")    # 11-digit raw
        self.assertEqual(normalize_fda_ndc_11("N400002751001"), "00002751001")  # Box 24 N4 prefix
        self.assertEqual(normalize_fda_ndc_11("1234-5678-90"), "01234567890")   # 4-4-2 -> pad labeler
        self.assertEqual(normalize_fda_ndc_11("12345-678-90"), "12345067890")   # 5-3-2 -> pad product
        self.assertEqual(normalize_fda_ndc_11("12345-6789-0"), "12345678900")   # 5-4-1 -> pad package

        # Ambiguous unhyphenated 10-digit, all-zeros, non-numeric, or bad segment counts rejected
        for invalid_ndc in ("1234567890", "00000000000", "0000-0000-00", "123-4567-89", "ABCDE-1234-56", "", None):
            with self.subTest(ndc=invalid_ndc):
                self.assertIsNone(normalize_fda_ndc_11(invalid_ndc))

    def _base_encounter(self) -> dict:
        return {
            "encounter_id": "ENC-NDC-001",
            "date_of_service": "2026-11-01",
            "provider": {
                "type": "MD",
                "state": "FL",
                "specialty": "PM&R",
                "billing_npi_type_2": "1235188676",
                "rendering_npi_type_1": "1598766495",
                "organization_name": "REHAB SPECIALISTS OF SOUTH FLORIDA",
            },
            "patient": {
                "member_id": "8XC3F49DT93",
                "first_name": "JANE",
                "last_name": "DOE",
                "dob": "1948-05-14",
                "gender": "F",
                "address": "100 MAIN ST",
                "city": "MIAMI",
                "state": "FL",
                "zip": "33101",
            },
            "payer": {
                "name": "MEDICARE PART B FL",
                "payer_id": "09102",
                "type": "MEDICARE",
            },
            "clinical_note": {
                "chief_complaint": "Right knee osteoarthritis pain",
                "assessment_and_plan": "Injected right knee with triamcinolone 40mg; prescribed home exercise.",
                "time_spent_minutes": 35,
            },
            "billing": {
                "place_of_service": "11",
                "diagnosis_codes": [{"code": "M17.11", "pointer": 1}],
                "cpt_codes": [
                    {"code": "20610", "charge": 115.0, "units": 1, "modifiers": [], "pointers": [1]},
                    {
                        "code": "J3301",
                        "charge": 25.0,
                        "units": 4,
                        "modifiers": ["JZ"],
                        "pointers": [1],
                        "ndc": "0009-3449-01",
                        "ndc_quantity": 1.0,
                        "ndc_unit": "ML",
                    },
                ],
            },
        }

    def test_scrutinizer_enforces_fda_ndc_on_j_q_s_drug_codes(self):
        scrut = MedicalBillingScrutinizer()

        # Valid 4-4-2 NDC normalizes to 11-digit and passes
        good_enc = self._base_encounter()
        res_ok = scrut.scrutinize_encounter(good_enc)
        self.assertTrue(res_ok.passed, f"Unexpected issues: {res_ok.issues}")
        self.assertEqual(good_enc["billing"]["cpt_codes"][1]["ndc"], "00009344901")

        # Missing NDC on J3301 raises CRITICAL MISSING_OR_INVALID_FDA_NDC
        bad_missing = self._base_encounter()
        bad_missing["patient"]["member_id"] = "1EG4TE5MK72"
        del bad_missing["billing"]["cpt_codes"][1]["ndc"]
        res_missing = scrut.scrutinize_encounter(bad_missing)
        self.assertFalse(res_missing.passed)
        self.assertIn("MISSING_OR_INVALID_FDA_NDC", {i["code"] for i in res_missing.issues})

        # Invalid unit ('MG' instead of X12 'ME') raises CRITICAL MISSING_OR_INVALID_FDA_NDC
        bad_unit = self._base_encounter()
        bad_unit["patient"]["member_id"] = "3RC8KT2WA45"
        bad_unit["billing"]["cpt_codes"][1]["ndc_unit"] = "MG"
        res_unit = scrut.scrutinize_encounter(bad_unit)
        self.assertFalse(res_unit.passed)
        self.assertIn("MISSING_OR_INVALID_FDA_NDC", {i["code"] for i in res_unit.issues})

    def test_scrutinizer_catches_g2211_snf_pos_mismatch_incident_to_and_qmb_balance_billing(self):
        scrut = MedicalBillingScrutinizer()
        enc = self._base_encounter()
        enc["patient"]["member_id"] = "6A78JM8VE39"
        enc["patient"]["qmb_eligible"] = True
        enc["billing"]["place_of_service"] = "31"
        enc["billing"]["incident_to"] = True
        enc["billing"]["patient_copay_collected"] = 33.00
        enc["billing"]["cpt_codes"] = [
            {"code": "99214", "charge": 175.0, "units": 1, "modifiers": [], "pointers": [1]},
            {"code": "G2211", "charge": 22.0, "units": 1, "modifiers": [], "pointers": [1]},
        ]
        # Replace G2211 base with SNF code in a second check
        res = scrut.scrutinize_encounter(enc)
        issue_codes = {i["code"] for i in res.issues}
        self.assertIn("POS_CPT_MISMATCH", issue_codes)
        self.assertIn("INCIDENT_TO_FACILITY_PROHIBITED", issue_codes)
        self.assertIn("QMB_BALANCE_BILLING_PROHIBITED", issue_codes)

        enc2 = self._base_encounter()
        enc2["patient"]["member_id"] = "9H04KP7MN12"
        enc2["billing"]["place_of_service"] = "31"
        enc2["billing"]["cpt_codes"] = [
            {"code": "99305", "charge": 165.0, "units": 1, "modifiers": [], "pointers": [1]},
            {"code": "G2211", "charge": 22.0, "units": 1, "modifiers": [], "pointers": [1]},
        ]
        res2 = scrut.scrutinize_encounter(enc2)
        self.assertIn("G2211_INELIGIBLE_BASE_OR_SETTING", {i["code"] for i in res2.issues})

    def test_hcfa_auditor_maps_box_18_box_24_ndc_and_box_32_facility(self):
        auditor = HCFA1500ExtractorAndAuditor()
        enc = self._base_encounter()
        enc["billing"]["place_of_service"] = "31"
        enc["billing"]["admission_date"] = "2026-10-28"
        enc["billing"]["service_facility"] = {
            "name": "GLADES WEST REHABILITATION AND NURSING CENTER",
            "npi": "1669882411",
            "address": "15955 BASS CREEK ROAD",
            "city": "MIRAMAR",
            "state": "FL",
            "zip": "330270301",
        }
        enc["billing"]["cpt_codes"] = [
            {
                "code": "J3301",
                "charge": 25.0,
                "units": 4,
                "modifiers": ["JZ"],
                "pointers": [1],
                "ndc": "00009344901",
                "ndc_quantity": 1.0,
                "ndc_unit": "ML",
            }
        ]
        report = auditor.audit_hcfa_requirements(enc)
        boxes = report["hcfa_boxes"]
        self.assertEqual(boxes["box_18_admission_date"], "2026-10-28")
        self.assertEqual(boxes["box_32_service_facility"]["32a_npi"], "1669882411")
        self.assertEqual(boxes["box_24_service_lines"][0]["24_shaded_ndc_n4"], "N400009344901 ML1.0")
        self.assertEqual(boxes["box_33_billing_provider"]["phone"], "")  # No fake 8005551212 fallback


class TestMedicalCodingAssistantCheckClaimCodes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = MedicalCodingAssistant()

    def test_clean_snf_initial_consult_claim_verifies_true(self):
        rep = self.kb.check_claim_codes(
            cpt_codes=["99305"],
            icd10_codes=["I10", "M62.81"],
            pos="31",
            payer_type="MEDICARE",
            provider_type="MD",
        )
        self.assertTrue(rep["verified"], f"Unexpected issues: {rep['issues']}")
        self.assertEqual([d["code"] for d in rep["valid_billable_icd10"]], ["I10", "M62.81"])
        self.assertTrue(rep["pos_cpt_compatibility"]["compatible"])
        self.assertGreaterEqual(len(rep["citations"]), 3)

    def test_truncated_non_billable_icd10_m54_5_suggests_specific_leaf_codes(self):
        rep = self.kb.check_claim_codes(
            cpt_codes=["99213"],
            icd10_codes=["M54.5", "Z99.999"],
            pos="11",
        )
        self.assertFalse(rep["verified"])
        self.assertIn("Z99.999", rep["invalid_icd10"])
        self.assertIn("M54.5", rep["non_billable_suggestions"])
        suggested_codes = {s["code"] for s in rep["non_billable_suggestions"]["M54.5"]}
        self.assertTrue({"M54.50", "M54.51", "M54.59"}.issubset(suggested_codes))

    def test_symptom_suppression_flags_knee_pain_with_osteoarthritis(self):
        rep = self.kb.check_claim_codes(
            cpt_codes=["99214"],
            icd10_codes=["M17.0", "M25.561"],
            pos="11",
        )
        self.assertEqual(len(rep["symptom_suppression_alerts"]), 1)
        self.assertEqual(rep["symptom_suppression_alerts"][0]["symptom_code"], "M25.561")

    def test_pos_cpt_compatibility_and_snf_consolidated_billing(self):
        # Office 99214 in SNF POS 31 -> incompatible
        r1 = self.kb.check_claim_codes(["99214"], ["I10"], pos="31")
        self.assertFalse(r1["verified"])
        self.assertFalse(r1["pos_cpt_compatibility"]["compatible"])

        # SNF 99309 in Office POS 11 -> incompatible
        r2 = self.kb.check_claim_codes(["99309"], ["I10"], pos="11")
        self.assertFalse(r2["verified"])
        self.assertFalse(r2["pos_cpt_compatibility"]["compatible"])

        # Therapy 97110 during Part A SNF stay POS 31 -> SNF Consolidated Billing block
        r3 = self.kb.check_claim_codes(["97110"], ["M62.81"], pos="31", payer_type="MEDICARE")
        self.assertFalse(r3["verified"])
        self.assertIn("SNF_CONSOLIDATED_BILLING_BUNDLED", {i["code"] for i in r3["issues"]})

        # Telehealth POS 10 requires -95 or -93
        r4_bad = self.kb.check_claim_codes(["99214"], ["I10"], pos="10")
        self.assertFalse(r4_bad["verified"])
        self.assertIn("MISSING_TELEHEALTH_MODIFIER", {i["code"] for i in r4_bad["issues"]})

        r4_ok = self.kb.check_claim_codes(["99214"], ["I10"], pos="10", modifiers=["95"])
        self.assertTrue(r4_ok["verified"])

    def test_ncci_ptp_mue_and_g2211_rules(self):
        # 99214 + 20610 without -25 fails; with -25 passes
        no_mod = self.kb.check_claim_codes(["99214", "20610"], ["M17.0"], pos="11")
        self.assertFalse(no_mod["verified"])
        self.assertIn("NCCI_MISSING_MODIFIER", {i["code"] for i in no_mod["issues"]})

        with_mod = self.kb.check_claim_codes(["99214-25", "20610"], ["M17.0"], pos="11")
        self.assertTrue(with_mod["verified"])

        # G2211 invalid with SNF 99309; valid with Office 99214; invalid with 99214-25 + 20610
        g_snf = self.kb.check_claim_codes(["99309", "G2211"], ["I10"], pos="31")
        self.assertFalse(g_snf["verified"])
        self.assertIn("G2211_INELIGIBLE", {i["code"] for i in g_snf["issues"]})

        g_off = self.kb.check_claim_codes(["99214", "G2211"], ["I10"], pos="11")
        self.assertTrue(g_off["verified"])

        g_25 = self.kb.check_claim_codes(["99214-25", "20610", "G2211"], ["M17.0"], pos="11")
        self.assertFalse(g_25["verified"])
        self.assertIn("G2211_MODIFIER_25_CONFLICT", {i["code"] for i in g_25["issues"]})

        # MUE > 1 on 99305
        mue_fail = self.kb.check_claim_codes([{"code": "99305", "units": 2}], ["I10"], pos="31")
        self.assertFalse(mue_fail["verified"])
        self.assertIn("MUE_UNITS_EXCEEDED", {i["code"] for i in mue_fail["issues"]})

    def test_deleted_consult_codes_fcso_lcd_ndc_and_medicaid_qmb(self):
        # Deleted / nonpayable Medicare consult code 99254
        del_rep = self.kb.check_claim_codes(["99254"], ["I10"], pos="31", payer_type="MEDICARE")
        self.assertFalse(del_rep["verified"])
        self.assertIn("DELETED_OR_NONPAYABLE_CPT", {i["code"] for i in del_rep["issues"]})

        # FCSO FL LCD L36063 for 82306
        lcd_fail = self.kb.check_claim_codes(["82306"], ["I10"], pos="11", payer_type="MEDICARE")
        self.assertFalse(lcd_fail["verified"])
        self.assertIn("FCSO_LCD_NOT_MET_L36063", {i["code"] for i in lcd_fail["issues"]})

        lcd_ok = self.kb.check_claim_codes(["82306"], ["E55.9"], pos="11", payer_type="MEDICARE")
        self.assertTrue(lcd_ok["verified"])

        # J3301 NDC check
        ndc_fail = self.kb.check_claim_codes(["J3301"], ["M17.0"], pos="11")
        self.assertFalse(ndc_fail["verified"])
        self.assertFalse(ndc_fail["ndc_check"]["valid"])

        ndc_ok = self.kb.check_claim_codes(
            ["J3301"],
            ["M17.0"],
            pos="11",
            ndc={"ndc": "1234-5678-90", "unit": "ML", "quantity": 1.0},
            payer_type="QMB_MEDICAID",
            provider_type="APRN",
        )
        self.assertTrue(ndc_ok["verified"])
        self.assertEqual(ndc_ok["ndc_check"]["normalized_ndc_11"], "01234567890")
        notes_joined = " ".join(ndc_ok["reimbursement_and_payer_notes"])
        self.assertIn("85%", notes_joined)
        self.assertIn("Qualified Medicare Beneficiaries", notes_joined)

        # FL Medicaid 10-digit ID helper
        self.assertTrue(valid_fl_medicaid_id("1234567890"))
        self.assertFalse(valid_fl_medicaid_id("0000000000"))
        self.assertFalse(valid_fl_medicaid_id("12345"))

    def test_openfda_lookup_mocked_and_offline_fallback(self):
        fake_payload = {
            "results": [
                {
                    "product_ndc": "0009-3449",
                    "generic_name": "TRIAMCINOLONE ACETONIDE",
                    "brand_name": "KENALOG-40",
                    "labeler_name": "Pharmacia & Upjohn",
                    "dosage_form": "INJECTION, SUSPENSION",
                    "route": ["INTRA-ARTICULAR"],
                }
            ]
        }

        class _DummyResp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        with patch("urllib.request.urlopen", return_value=_DummyResp(json.dumps(fake_payload).encode("utf-8"))):
            live = self.kb.lookup_ndc("0009-3449-01", unit="ML", quantity=1.0, allow_openfda_live=True)
        self.assertEqual(live["normalized_ndc_11"], "00009344901")
        self.assertEqual(live["verification_source"], "openfda_live_directory")
        self.assertEqual(live["openfda_record"]["brand_name"], "KENALOG-40")

        # Network error gracefully falls back to deterministic offline validator
        with patch("urllib.request.urlopen", side_effect=OSError("offline")):
            offline = self.kb.lookup_ndc("0009-3449-01", unit="ML", quantity=1.0, allow_openfda_live=True)
        self.assertTrue(offline["valid_format"])
        self.assertEqual(offline["verification_source"], "offline_deterministic_5_4_2_validator")


class TestMedicalCodingAssistantZeroHallucinationGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kb = MedicalCodingAssistant()

    def test_verified_answers_include_authoritative_citations(self):
        queries = [
            ("Is ICD-10 M54.5 billable or do I need a more specific code?", "NON-BILLABLE"),
            ("Is I10 a billable ICD-10 diagnosis code?", "IS a valid, billable leaf code"),
            ("How should Dr. Eftekhari bill an initial SNF consultation and Modifier AI in POS 31?", "99304"),
            ("Can we bill G2211 on a SNF visit 99309?", "NEVER"),
            ("What is the difference between telehealth POS 02 and POS 10 with modifier 95?", "POS 10"),
            ("Convert FDA NDC 1234-5678-90 for Loop 2410 J-code billing", "01234567890"),
            ("Check Medicare MBI 8XC3F49DT93 and BXC3F49DT93", "INVALID"),
            ("Can we balance bill a QMB Florida Medicaid 77027 crossover patient?", "1902(n)(3)(B)"),
            ("What are Florida APRN autonomous rules under 464.0123 and Incident-To in SNF?", "85%"),
            ("What are the Office Ally receiver ID 330897513 and SFTP channels for 09102?", "ftp10.officeally.com:22"),
        ]
        for q, expected_snippet in queries:
            with self.subTest(query=q):
                ans = self.kb.ask_assistant(q)
                self.assertEqual(ans["status"], "VERIFIED", f"Unexpected abstain: {ans}")
                self.assertTrue(ans["verified"])
                self.assertIn(expected_snippet, ans["answer"])
                self.assertTrue(len(ans["citations"]) >= 1)

    def test_refusal_guard_abstains_on_unverified_codes_fees_and_out_of_scope(self):
        refusal_queries = [
            "What are the bundling rules and coverage for CPT 47562?",
            "Is ICD-10 code Q99.999 billable for Medicare?",
            "What is the exact dollar reimbursement and wRVU value for 99305 in Miami?",
            "What is the commercial fee schedule allowable for 99214?",
            "What dosage should I prescribe for my patient with pneumonia?",
            "Who won the Super Bowl in 2026?",
            "hi",
        ]
        for q in refusal_queries:
            with self.subTest(query=q):
                ans = self.kb.ask_assistant(q)
                self.assertEqual(ans["status"], "ABSTAINED_UNVERIFIED", f"Should have abstained on: {q}")
                self.assertFalse(ans["verified"])
                self.assertEqual(ans["citations"], [])


class TestRound2ChartIngestorClaimGateAndCodingAudit(unittest.TestCase):
    """Covers every chart-level ingestor, claim-gate, and clinical-coding gap from Board #10/#11."""

    def setUp(self):
        from ingestion.multimodal_ingestor import MultimodalDocumentIngestor
        self.ing = MultimodalDocumentIngestor()
        self.coder = ClinicalCodingEngine()

    def test_mbi_strict_filtering_ignores_corrupted_pos1_letters(self):
        # Corrupted OCR 'BXC3F49DT93' on Page 1 must never shadow valid CMS MBI '8XC3F49DT93' on Page 2
        text = (
            "Medicare Beneficiary ID\nMedicare#BXC3F49DT93\n"
            "Resident Name: Dickison, Matilda F.(5310)\nMedicare Beneficiary ID: 8XC3F49DT93\n"
        )
        demo = self.ing.extract_chart_demographics(text)
        self.assertEqual(demo["member_id"], "8XC3F49DT93")
        self.assertTrue(valid_mbi(demo["member_id"]))

    def test_payer_zones_part_a_only_qmb_and_optum_part_d_exclusions(self):
        from compliance.claim_gate import coder_review_notes, street_needs_verification

        # 1) Blank Glades West 'United Healthcare Nursing Home Plan - Optum' + 'Part D Carrier: Wellcare' must NOT trigger MA hold
        clean_chart = (
            "RESIDENT INFORMATION\nLee, Betty\n5900NW7THAVEAPT323MIAMIFL33127\nSex\nF\n"
            "AdmisslonDate\n10/10/2026\nMedicare Beneficiary ID\n8DT5U61VM62\n"
            "PAYER INFORMATION\nSecond Payer MCRA Co-Pay Medicaid\n"
            "OTHER INFORMATION\nMedicare Coverage\nPart A, B & D\nPart D Carrier\nWellcare\n"
            "United Healthcare Nursing Home Plan -\nOptum\nCARE PROVIDERS\n"
        )
        d_qmb = self.ing.extract_chart_demographics(clean_chart)
        self.assertTrue(d_qmb["is_qmb"])
        self.assertEqual(d_qmb["secondary_payer"], "FLORIDA MEDICAID (QMB)")
        self.assertEqual(d_qmb["admission_date"], "2026-10-10")
        self.assertEqual(d_qmb["address"], "5900 NW 7TH AVE APT 323")
        self.assertFalse(street_needs_verification(d_qmb["address"]))
        self.assertEqual(hard_gate_issues(d_qmb, "LEE BETTY.pdf", {}), [])
        notes = {n["code"] for n in coder_review_notes(clean_chart, ["I10"], {"place_of_service": "31"})}
        self.assertIn("QMB_DO_NOT_BALANCE_BILL", notes)

        # 2) Part A Only (#25 Nika Alice) raises MEDICARE_PART_A_ONLY_NO_PART_B
        part_a_chart = (
            "RESIDENT INFORMATION\nNika, Alice\n119 NW 93RD AVE, MIAMI, FL 33172\nSex: F\n"
            "AdmissionDate: 10/24/2026\nMedicare Beneficiary ID: 4G04H72AQ91\n"
            "OTHER INFORMATION\nMedicare Coverage\nMMA Plan\nPart A only\nCARE PROVIDERS\n"
        )
        d_pa = self.ing.extract_chart_demographics(part_a_chart)
        self.assertTrue(d_pa["part_a_only"])
        hg_pa = {i["code"] for i in hard_gate_issues(d_pa, "NIKA ALICE.pdf", {})}
        self.assertIn("MEDICARE_PART_A_ONLY_NO_PART_B", hg_pa)

        # 3) Active Preferred Care Partners MA (#13 Ledo) & Managed Medicare B (#05 Edmonson) raise PRIMARY_PAYER_UNVERIFIED
        ma_chart = (
            "RESIDENT INFORMATION\nLedo, Carmen\n15205 SW 45TH TER UNIT H, MIAMI, FL 33185\nSex: F\n"
            "Admisslon Date\n10/15/2026\nMedicare Beneficiary ID\n3XW9HY7UC56\n"
            "Insurance Name:\nPREFERREDCAREPARTNERS\nInsurance Policy#\n977586643\n"
            "OTHER INFORMATION\nMedicare Coverage\nMedicare Advantage Plan\nCARE PROVIDERS\n"
        )
        d_ma = self.ing.extract_chart_demographics(ma_chart)
        self.assertTrue(d_ma["managed_medicare"])
        self.assertEqual(d_ma["secondary_payer"], "PREFERRED CARE PARTNERS")
        self.assertEqual(d_ma["secondary_member_id"], "977586643")
        self.assertIn("PRIMARY_PAYER_UNVERIFIED", {i["code"] for i in hard_gate_issues(d_ma, "LEDO CARMEN.pdf", {})})

    def test_street_quality_gate_and_middle_initial_mismatch(self):
        from compliance.claim_gate import name_matches_filename, street_needs_verification

        self.assertEqual(self.ing._clean_ocr_street("17081NW23RDSTAptRear"), "17081 NW 23RD ST APT REAR")
        self.assertTrue(street_needs_verification("25927ST"))
        self.assertTrue(street_needs_verification("6060 NW 55TH N"))
        self.assertFalse(street_needs_verification("1 MAIN ST"))
        self.assertFalse(street_needs_verification("17081 NW 23RD ST APT REAR"))

        # Middle initial mismatch: 'SONIA I' vs 'SONIA L.pdf' fails, while 'THERESA MORRISON' vs 'THERESA M..pdf' passes
        self.assertFalse(name_matches_filename("LOPEZ ROCA", "SONIA I", "LOPEZ ROCA SONIA L.pdf"))
        self.assertTrue(name_matches_filename("BRADSHAW", "THERESA MORRISON", "BRADSHAW THERESA M..pdf"))
        self.assertTrue(name_matches_filename("ISSERMAN", "CARLA I", "ISSERMAN CARLA I.pdf"))

    def test_ocr_diagnoses_ckd_staging_diabetic_neuropathy_and_sp_exclusions(self):
        from compliance.claim_gate import icd10_issues

        # Edmonson OCR patterns: Diabetesmellitustype2 + Diabetic neuropathy -> E11.40 (suppresses E11.9 & G62.9);
        # Chronic kidney disease 3B -> N18.32 (suppresses N18.9); Dsteoporosis -> M81.0
        edmonson_text = (
            "HPI/Co-morbidities:\ns/p AKI\ns/pAcute urinary tract infection\nRecurrent UTI\n"
            "Diabetesmellitustype2\nDiabetic neuropathy\nDsteoporosis\nChronic kidney disease 3B\nParkinson's disease"
        )
        codes = [d.code for d in self.coder._extract_diagnoses(edmonson_text)]
        self.assertIn("E11.40", codes)
        self.assertNotIn("E11.9", codes)
        self.assertNotIn("G62.9", codes)
        self.assertIn("N18.32", codes)
        self.assertNotIn("N18.9", codes)
        self.assertIn("M81.0", codes)
        self.assertIn("G20.A1", codes)
        self.assertNotIn("N17.9", codes, "Historical 's/p AKI' must not be coded as active N17.9")
        self.assertNotIn("N39.0", codes, "Historical 's/p Acute UTI' / 'Recurrent UTI' must not be coded as active N39.0")
        self.assertEqual(icd10_issues(codes), [])

        # Confirm active UTI or active AKI IS still coded when documented actively
        active_codes = [d.code for d in self.coder._extract_diagnoses("Assessment: Acute kidney injury and urinary tract infection on macrobid.")]
        self.assertIn("N17.9", active_codes)
        self.assertIn("N39.0", active_codes)

    @unittest.skipUnless(os.path.isdir(NOV26_DIR), "november_2026_0 OCR cache not present")
    def test_all_32_nov26_clinical_charts_have_valid_mbi_admission_date_and_billable_icd10(self):
        from compliance.claim_gate import icd10_issues
        import glob

        files = sorted(glob.glob(os.path.join(NOV26_DIR, "*.pdf.ocr.txt")))
        self.assertEqual(len(files), 33)
        for p in files:
            fn = os.path.basename(p)[:-8]
            t = open(p, encoding="utf-8").read()
            d = self.ing.extract_chart_demographics(t)
            dxs = [x.code for x in self.coder._extract_diagnoses(t)[:12]]
            self.assertEqual(icd10_issues(dxs), [], f"Non-billable ICD-10 on {fn}: {dxs}")
            if fn != "MISC-DATE.pdf":
                self.assertTrue(valid_mbi(d["member_id"]), f"Invalid MBI on {fn}: {d['member_id']}")
                self.assertIsNotNone(d["admission_date"], f"Missing Box 18 admission_date on {fn}")


if __name__ == "__main__":
    unittest.main()
