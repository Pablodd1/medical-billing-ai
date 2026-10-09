"""
Comprehensive test suite verifying clinical coding, Florida scope of practice,
symptom suppression, negation handling, procedure vs ordered test distinction,
and X12 837P EDI compliance.
"""

import unittest
import os
import sys

# Ensure project root is importable
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ["OA_ALLOW_SIMULATION"] = "true"

from models.encounter import DocumentSourceType, ClinicalNoteParsed
from ingestion.multimodal_ingestor import MultimodalDocumentIngestor
from coding.clinical_coder import ClinicalCodingEngine
from scrutiny.scrutinizer import MedicalBillingScrutinizer
from pipeline import AutonomousMedicalBillingPipeline
from transmission.x12_837_generator import X12_837P_Generator


class TestClinicalBillingPipeline(unittest.TestCase):

    def setUp(self):
        self.ingestor = MultimodalDocumentIngestor()
        self.coder = ClinicalCodingEngine()
        self.scrutinizer = MedicalBillingScrutinizer()
        self.pipeline = AutonomousMedicalBillingPipeline()

    def test_non_diagnosis_tokens_not_coded(self):
        """Verify non-diagnosis tokens like 'Vitamin B12' or 'TID' are never coded as ICD-10."""
        text = "Assessment: Vitamin B12 deficiency suspected. Start B12 injections TID. No diabetes."
        diagnoses = self.coder._extract_diagnoses(text)
        codes = [d.code for d in diagnoses]
        self.assertNotIn("B12", codes, "Token 'B12' should not be extracted as an ICD-10 diagnosis")

    def test_negation_awareness_explicit_and_ontology(self):
        """Verify conditions preceded by 'ruled out', 'denies', or 'negative for' are NOT billed."""
        text = "Assessment: Ruled out I10. Patient denies diabetes. History of knee pain."
        diagnoses = self.coder._extract_diagnoses(text)
        codes = [d.code for d in diagnoses]
        self.assertNotIn("I10", codes, "Explicit code I10 preceded by 'Ruled out' must not be billed")
        self.assertNotIn("E11.9", codes, "Diabetes preceded by 'denies' must not be billed")

    def test_symptom_suppression_when_definitive_etiology_coded(self):
        """ICD-10 Section I.B.5: Symptom codes (e.g. knee pain M25.561) must be suppressed
        when a definitive underlying etiology (e.g. M23.201 internal derangement) is coded."""
        text = "Chief Complaint: Severe right knee pain. Assessment: Internal derangement right knee (M23.201)."
        diagnoses = self.coder._extract_diagnoses(text)
        codes = [d.code for d in diagnoses]
        self.assertIn("M23.201", codes)
        self.assertNotIn("M25.561", codes, "Symptom M25.561 must be suppressed when M23.201 is present")

    def test_ordered_tests_not_billed_as_performed_procedures(self):
        """Ordered X-rays, external labs or referrals must NOT be billed as clinic procedures."""
        text = "Plan: X-ray right knee ordered (CPT 73560), PT referral, schedule vitamin d test."
        procedures = self.coder._extract_procedures(text)
        billed_cpts = [p["code"] for p in procedures]
        self.assertNotIn("73560", billed_cpts, "Ordered X-ray 73560 must not be billed as performed")
        self.assertNotIn("82306", billed_cpts, "Scheduled vitamin d test must not be billed as performed")

    def test_modifier_25_only_on_surgical_procedures(self):
        """Modifier -25 must ONLY attach to E/M when a minor surgical procedure (CPT 10000-69999) is performed."""
        # Note with only diagnostic/E/M - should NOT have modifier 25
        note_diagnostic = ClinicalNoteParsed(
            raw_text="Routine follow-up",
            source_type=DocumentSourceType.TEXT if hasattr(DocumentSourceType, "TEXT") else list(DocumentSourceType)[0],
            chief_complaint="Follow-up hypertension",
            history_of_present_illness="Stable",
            assessment_and_plan="Hypertension (I10). Continue medication.",
            time_spent_minutes=25
        )
        billing_diag = self.coder.generate_coding(note_diagnostic)
        em_line = billing_diag.cpt_codes[0]
        self.assertNotIn("25", em_line.modifiers, "Modifier 25 must not be attached without a performed surgical procedure")

        # Note with joint injection (20610) performed - SHOULD have modifier 25
        note_surgical = ClinicalNoteParsed(
            raw_text="Major joint injection performed",
            source_type=DocumentSourceType.TEXT if hasattr(DocumentSourceType, "TEXT") else list(DocumentSourceType)[0],
            chief_complaint="Severe osteoarthritis knee",
            history_of_present_illness="Persistent pain",
            assessment_and_plan="Osteoarthritis. Performed joint injection with steroid.",
            time_spent_minutes=25
        )
        billing_surg = self.coder.generate_coding(note_surgical)
        em_line_surg = billing_surg.cpt_codes[0]
        self.assertIn("25", em_line_surg.modifiers, "Modifier 25 must be attached when minor surgical procedure 20610 performed")

    def test_florida_aprn_scope_scrutiny(self):
        """Verify Florida APRN without collaborative protocol or autonomous registration is paused for review."""
        encounter_payload = {
            "encounter_id": "TEST-APRN-01",
            "date_of_service": "2026-09-28",
            "patient": {"first_name": "JOHN", "last_name": "DOE", "dob": "1970-01-01", "member_id": "JD12345"},
            "provider": {
                "name": "NURSE JANE",
                "type": "APRN",
                "state": "FL",
                "specialty": "Physical Medicine & Rehabilitation",
                "fl_autonomous_registered": False,
                "has_collaborative_protocol": False,
                "npi": "1234567890",
                "tax_id": "123456789"
            },
            "payer": {"name": "Medicare Florida", "payer_id": "00901", "type": "MEDICARE"},
            "clinical_note": {
                "chief_complaint": "Joint pain",
                "assessment_and_plan": "Rehab plan on file"
            },
            "billing": {
                "place_of_service": "11",
                "total_charge": 115.00,
                "diagnosis_codes": [{"code": "M17.0", "description": "Osteoarthritis"}],
                "cpt_codes": [{"code": "99213", "modifiers": [], "charge": 115.00, "units": 1}]
            }
        }
        res = self.scrutinizer.scrutinize_encounter(encounter_payload)
        self.assertFalse(res.passed, "Encounter without protocol must fail clean claim pass")
        issue_codes = [i["code"] for i in res.issues]
        self.assertIn("MISSING_SUPERVISORY_PROTOCOL", issue_codes)

    def test_837p_taxonomy_and_qualifiers(self):
        """Verify 837P EDI generator outputs ABK/ABF qualifiers and APRN taxonomy 363L00000X."""
        encounter_dict = {
            "encounter_id": "ENC-TEST-TAX",
            "patient_control_number": "ENC-TEST-TAX",
            "provider": {
                "organization_name": "SUNSHINE CLINIC",
                "rendering_first_name": "SARAH",
                "rendering_last_name": "ORTEGA",
                "type": "APRN",
                "npi": "1234567893",
                "tax_id": "123456789"
            },
            "patient": {
                "first_name": "MARK", "last_name": "JOHNSON",
                "dob": "1968-04-15", "gender": "M", "member_id": "MJ99887766"
            },
            "payer": {"name": "Medicare Florida", "payer_id": "00901"},
            "billing": {
                "total_charge": 175.00,
                "place_of_service": "11",
                "diagnosis_codes": [
                    {"code": "M23.201", "description": "Internal derangement"},
                    {"code": "I10", "description": "Hypertension"}
                ],
                "cpt_codes": [
                    {"code": "99214", "modifiers": [], "charge": 175.00, "units": 1, "dx_pointers": [1]}
                ]
            }
        }
        generator = X12_837P_Generator()
        edi = generator.generate_837p(encounter_dict)

        self.assertIn("HI*ABK:M23201*ABF:I10~", edi, "Must use ICD-10 ABK/ABF qualifiers")
        self.assertIn("SBR*P*18*******MB~", edi, "Medicare Part B must have MB filing indicator")
        self.assertIn("PRV*PE*PXC*363L00000X~", edi, "APRN provider must default to 363L00000X taxonomy")


if __name__ == "__main__":
    unittest.main()
