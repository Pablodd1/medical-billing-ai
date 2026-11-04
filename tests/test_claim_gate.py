# ============================================
# FILE: tests/test_claim_gate.py
# PURPOSE: Unit tests for claim gates, extraction fixes, billing-entity validation
# ============================================

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compliance.claim_gate import (
    canonical_payer, coder_review_notes, hard_gate_issues, load_billing_entity,
    name_matches_filename, valid_npi,
)
from ingestion.multimodal_ingestor import MultimodalDocumentIngestor

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "september_2026")


def _ocr(name: str) -> str:
    with open(os.path.join(DATA, f"{name}.ocr.txt"), encoding="utf-8") as fh:
        return fh.read()


def _codes(issues):
    return {i["code"] for i in issues}


GOOD_DEMO = {"gender": "M", "address": "1 Main St", "city": "MIAMI", "state": "FL", "zip": "33129",
             "last_name": "GONZALEZ", "first_name": "JACINTO G.", "patient_name": "GONZALEZ, JACINTO G.",
             "secondary_payer": None, "rendering_npi": "1356442776"}


class TestNpi(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertTrue(valid_npi("1234567893"))
        self.assertFalse(valid_npi("1234567890"))
        self.assertFalse(valid_npi("12345"))
        self.assertFalse(valid_npi(None))


class TestPayerAndNames(unittest.TestCase):
    def test_canonical_payer_rejects_noise(self):
        self.assertEqual(canonical_payer("Humana"), "HUMANA")
        self.assertEqual(canonical_payer("Simply"), "SIMPLY HEALTHCARE")
        for noise in ("Solis", "SSI Disability Pending", "Medicald LTC Pollcy", "", None):
            self.assertIsNone(canonical_payer(noise))

    def test_name_vs_filename(self):
        self.assertTrue(name_matches_filename("DELATORRE", "ZOILA L", "DE LA TORRE ZOILA L.pdf"))
        self.assertFalse(name_matches_filename("DELATORRE", "ZOILAL", "DE LA TORRE ZOILA L.pdf"))
        self.assertFalse(name_matches_filename("QUINTANA", "ROSAM", "QUINTANA ROSA M.pdf"))
        self.assertTrue(name_matches_filename("BRADSHAW", "THERESA MORRISON", "BRADSHAW THERESA M..pdf"))
        self.assertFalse(name_matches_filename("HEMANDEZ", "INOCENTE V.", "HERNANDEZ INOCENTE V..pdf"))
        self.assertFalse(name_matches_filename("GENZALEZ", "ROLANDO FERRER", "FERRER ROLANDO.pdf"))


class TestHardGates(unittest.TestCase):
    def test_clean_demo_passes(self):
        self.assertEqual(hard_gate_issues(GOOD_DEMO, "GONZALEZ JACINTO G..pdf", {}), [])

    def test_each_gate_fires(self):
        demo = dict(GOOD_DEMO, gender=None, address=None, secondary_payer="HUMANA", rendering_npi="1234567890")
        codes = _codes(hard_gate_issues(demo, "OTHER PERSON.pdf", {}))
        self.assertEqual(codes, {"MISSING_SEX", "MISSING_ADDRESS", "NAME_FILENAME_MISMATCH",
                                 "PRIMARY_PAYER_UNVERIFIED", "INVALID_RENDERING_NPI"})

    def test_overrides_clear_review_gates(self):
        demo = dict(GOOD_DEMO, secondary_payer="HUMANA")
        ov = {"name_verified": True, "primary_payer_confirmed": True}
        self.assertEqual(hard_gate_issues(demo, "OTHER PERSON.pdf", ov), [])


class TestCoderNotes(unittest.TestCase):
    def test_flags_when_no_time_or_code(self):
        self.assertEqual(_codes(coder_review_notes("stable, continue meds", ["D64.9"], {"place_of_service": "31"})),
                         {"EM_LEVEL_HEURISTIC", "LAB_DERIVED_DX_CHECK"})

    def test_time_documented_clears_em_flag(self):
        self.assertEqual(coder_review_notes("Total time 35 minutes", ["I10"], {"place_of_service": "31"}), [])


class TestBillingEntity(unittest.TestCase):
    def test_missing_and_placeholder(self):
        _, problems = load_billing_entity({})
        self.assertTrue(any("BILLING_TAX_ID" in p for p in problems))
        _, problems = load_billing_entity({"BILLING_ORG_NAME": "X", "BILLING_NPI": "1234567893",
                                           "BILLING_TAX_ID": "59-1234567", "BILLING_PHONE": "800-555-1212",
                                           "BILLING_ADDRESS": "a", "BILLING_CITY": "c", "BILLING_STATE": "FL",
                                           "BILLING_ZIP": "33000"})
        self.assertEqual(sorted(problems), ["BILLING_PHONE is a placeholder", "BILLING_TAX_ID is invalid or a placeholder"])

    def test_complete_entity(self):
        entity, problems = load_billing_entity({"BILLING_ORG_NAME": "ACME PMR LLC", "BILLING_NPI": "1234567893",
                                                "BILLING_TAX_ID": "84-3216540", "BILLING_PHONE": "305-555-0100",
                                                "BILLING_ADDRESS": "1 Main", "BILLING_CITY": "Miami",
                                                "BILLING_STATE": "fl", "BILLING_ZIP": "33131"})
        self.assertEqual(problems, [])
        self.assertEqual((entity["tax_id"], entity["state"]), ("843216540", "FL"))


@unittest.skipUnless(os.path.isdir(DATA), "batch OCR cache not present")
class TestExtractionOnRealOcr(unittest.TestCase):
    def setUp(self):
        self.ing = MultimodalDocumentIngestor()

    def test_gonzalez_sex_and_home_address(self):
        d = self.ing.extract_chart_demographics(_ocr("GONZALEZ JACINTO G..pdf"))
        self.assertEqual(d["gender"], "M")
        self.assertEqual((d["city"], d["state"], d["zip"]), ("MIAMI", "FL", "33129"))
        self.assertIn("Brickell", d["address"])
        self.assertNotIn("Bass", d["address"])

    def test_no_facility_address_fallback_anywhere(self):
        for fn in os.listdir(DATA):
            if fn.endswith(".ocr.txt"):
                d = self.ing.extract_chart_demographics(_ocr(fn[:-8]))
                self.assertNotIn("Bass", d["address"] or "", fn)

class TestValidators(unittest.TestCase):
    def test_icd10_issues(self):
        from compliance.claim_gate import icd10_issues
        self.assertEqual(icd10_issues(["I10", "F32.A", "E11.9"]), [])
        self.assertEqual(_codes(icd10_issues(["E11"])), {"ICD10_NOT_BILLABLE"})
        self.assertEqual(_codes(icd10_issues(["ZZZ.9"])), {"ICD10_INVALID"})

    def test_pos_note_cleared_by_override(self):
        base = _codes(coder_review_notes("Total time 35 minutes", ["I10"], {}))
        self.assertIn("POS_UNVERIFIED", base)
        self.assertEqual(coder_review_notes("Total time 35 minutes", ["I10"], {"place_of_service": "32"}), [])

    def test_x12_validation_flags_bad_state(self):
        from compliance.claim_gate import validate_x12_837
        edi = _sample_837()
        self.assertEqual(validate_x12_837(edi), [])
        self.assertTrue(validate_x12_837(edi.replace("*FL*", "*FLORIDA*", 1)))


def _sample_837() -> str:
    """One real generated claim for pyx12 round-trips (fabricated demo patient, no PHI)."""
    from transmission.x12_837_generator import X12_837P_Generator
    enc = {
        "patient_control_number": "ENC-TEST-1",
        "patient": {"first_name": "TEST", "last_name": "PATIENT", "dob": "1940-01-01", "gender": "F",
                    "member_id": "1EG4TE5MK73", "address": "1 Main St", "city": "Miami", "state": "FL", "zip": "33131"},
        "provider": {"organization_name": "ACME PMR LLC", "rendering_first_name": "ANN", "rendering_last_name": "DOE",
                     "rendering_npi_type_1": "1234567893", "billing_npi_type_2": "1234567893", "npi": "1234567893",
                     "tax_id": "843216540", "phone": "3055550100", "address": "1 Main St", "city": "Miami",
                     "state": "FL", "zip": "33131", "type": "MD"},
        "payer": {"name": "MEDICARE FLORIDA (FIRST COAST)", "payer_id": "09102", "type": "Medicare"},
        "billing": {"total_charge": 160.0, "place_of_service": "31",
                    "cpt_codes": [{"code": "99309", "charge": 160.0, "units": 1, "modifiers": [], "dx_pointers": [1]}],
                    "diagnosis_codes": [{"code": "I10", "pointer": 1}]},
        "date_of_service": "2026-09-07",
    }
    return X12_837P_Generator().generate_837p(enc)


class TestEmThresholds(unittest.TestCase):
    def _level(self, minutes, n_dx):
        from types import SimpleNamespace
        from coding.clinical_coder import ClinicalCodingEngine
        note = SimpleNamespace(chief_complaint="", history_of_present_illness="", assessment_and_plan="stable",
                               place_of_service_detected="31", time_spent_minutes=minutes)
        return ClinicalCodingEngine()._score_em_level(note, n_dx)[0]

    def test_time_thresholds(self):
        self.assertEqual(self._level(10, 0), "99307")
        self.assertEqual(self._level(15, 0), "99307")
        self.assertEqual(self._level(20, 0), "99308")
        self.assertEqual(self._level(30, 0), "99309")
        self.assertEqual(self._level(45, 0), "99310")

    def test_diagnosis_count_never_reaches_99310(self):
        self.assertEqual(self._level(0, 9), "99309")
        self.assertEqual(self._level(0, 1), "99308")


if __name__ == "__main__":
    unittest.main()
