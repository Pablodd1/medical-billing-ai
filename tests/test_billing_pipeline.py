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

    def test_clause_negation_does_not_bleed_across_sentences(self):
        """Negation like 'without trauma' must NOT cross sentence boundaries into subsequent diagnoses."""
        text = "Knee pain without trauma. Assessment: M23.201."
        diagnoses = self.coder._extract_diagnoses(text)
        codes = [d.code for d in diagnoses]
        self.assertIn("M23.201", codes, "Definitive diagnosis M23.201 must be extracted")
        self.assertNotIn("M25.561", codes, "Symptom knee pain M25.561 must be suppressed by M23.201")

    def test_multiple_occurrences_negated_then_confirmed(self):
        """When a condition is ruled out earlier but confirmed in Assessment, it must be extracted."""
        text = "Emergency Dept: Ruled out S82.001. Ortho Assessment: S82.001 confirmed on repeat film."
        diagnoses = self.coder._extract_diagnoses(text)
        codes = [d.code for d in diagnoses]
        self.assertIn("S82.001", codes, "Confirmed diagnosis S82.001 must be extracted")

    def test_room_numbers_and_spine_levels_excluded(self):
        """Room numbers ('room C12') and spine levels ('fracture at T12') must not be coded as diagnoses."""
        text = "Compression fracture at T12. Patient evaluated in room C12."
        diagnoses = self.coder._extract_diagnoses(text)
        codes = [d.code for d in diagnoses]
        self.assertNotIn("T12", codes, "Spine level T12 must not be extracted as diagnosis")
        self.assertNotIn("C12", codes, "Room number C12 must not be extracted as diagnosis")

    def test_disorder_and_scheduled_do_not_drop_performed_procedures(self):
        """Word 'disorder' must not trigger 'order' exclusion; separate follow-up schedule must not drop performed work."""
        text1 = "Procedure: 20610 performed, right knee, for meniscal disorder."
        procs1 = [p["code"] for p in self.coder._extract_procedures(text1)]
        self.assertIn("20610", procs1, "Performed 20610 must not be dropped due to 'disorder'")

        text2 = "CPT 17000 cryotherapy performed today; follow-up scheduled in 2 wks."
        procs2 = [p["code"] for p in self.coder._extract_procedures(text2)]
        self.assertIn("17000", procs2, "Performed 17000 must not be dropped due to 'scheduled' follow-up")

    def test_procedure_diagnosis_pointer_medical_necessity(self):
        """Procedure diagnosis pointer must point to the clinically supporting diagnosis (Box 24E / SV107)."""
        note = ClinicalNoteParsed(
            raw_text="Encounter",
            source_type=list(DocumentSourceType)[0],
            chief_complaint="Knee pain",
            history_of_present_illness="Hypertension I10 and meniscus derangement M23.201",
            assessment_and_plan="Procedure: 20610 performed today right knee.",
            time_spent_minutes=25
        )
        billing = self.coder.generate_coding(note)
        proc_lines = [l for l in billing.cpt_codes if l.code == "20610"]
        self.assertTrue(len(proc_lines) == 1)
        self.assertEqual(proc_lines[0].dx_pointers, [2], "Knee injection 20610 must point to M23.201 (pointer 2), not I10 (pointer 1)")

    def test_837p_loop_2320_segment_order(self):
        """ASC X12 5010 standard: AMT*D (Pos 015) must precede OI (Pos 035) in Loop 2320."""
        encounter = {
            "encounter_id": "COB-001",
            "patient_control_number": "COB-001",
            "primary_payer_paid_amount": 85.00,
            "patient": {
                "first_name": "JOHN", "last_name": "DOE", "dob": "1970-01-01", "member_id": "JD001",
                "secondary_payer": {"name": "AETNA SECONDARY", "payer_id": "60054", "type": "COMMERCIAL"},
                "secondary_member_id": "AETNA12345"
            },
            "provider": {"rendering_first_name": "SARAH", "rendering_last_name": "ORTEGA", "npi": "1234567893", "tax_id": "123456789"},
            "payer": {"name": "Medicare Florida", "payer_id": "00901"},
            "billing": {
                "total_charge": 175.00,
                "place_of_service": "11",
                "diagnosis_codes": [{"code": "I10", "description": "HTN"}],
                "cpt_codes": [{"code": "99214", "modifiers": [], "charge": 175.00, "units": 1, "dx_pointers": [1]}]
            }
        }
        generator = X12_837P_Generator()
        edi = generator.generate_837p(encounter)
        amt_pos = edi.find("AMT*D*85.00~")
        oi_pos = edi.find("OI***Y***Y~")
        self.assertTrue(amt_pos != -1, "AMT*D segment must be emitted")
        self.assertTrue(oi_pos != -1, "OI segment must be emitted")
        self.assertTrue(amt_pos < oi_pos, "AMT*D must precede OI in Loop 2320")

    def test_simulation_ledger_status(self):
        """Simulation mode must record SIMULATED_NOT_TRANSMITTED in the ledger, never SUBMITTED."""
        import uuid
        uid = uuid.uuid4().hex[:6]
        enc_id = f"TEST-SIM-{uid}"
        res = self.pipeline.process_encounter(
            raw_doc_or_content="Patient: JANE DOE. HTN I10. Plan: Continue meds.",
            doc_type=list(DocumentSourceType)[0],
            patient_data={"first_name": "JANE", "last_name": "DOE", "dob": "1975-05-10", "member_id": f"JD-{uid}"},
            provider_data={"name": "DR. JAMES RIVERA", "npi": "9876543210", "type": "MD", "tax_id": "123456789"},
            payer_data={"name": "Florida Blue", "payer_id": "00590", "type": "COMMERCIAL"},
            encounter_id=enc_id
        )
        self.assertEqual(res.get("status"), "TRANSMITTED_CLEAN_CLAIM")
        claim = self.pipeline.dispatcher.ledger.get_claim(enc_id)
        self.assertIsNotNone(claim)
        self.assertEqual(claim.get("status"), "SIMULATED_NOT_TRANSMITTED", "Simulation claim must be labeled SIMULATED_NOT_TRANSMITTED in ledger")

    def test_persistent_duplicate_claim_guard(self):
        """Exact duplicate encounter must be caught by Tier 8 Duplicate Claim Guard."""
        import uuid
        uid = uuid.uuid4().hex[:6]
        enc_id_1 = f"TEST-DUP-1-{uid}"
        enc_id_2 = f"TEST-DUP-2-{uid}"
        shared_pat = {"first_name": "CARLOS", "last_name": "SANTANA", "dob": "1960-03-20", "member_id": f"CS-{uid}"}
        shared_prov = {"name": "DR. JAMES RIVERA", "npi": "9876543210", "type": "MD", "tax_id": "123456789"}
        shared_payer = {"name": "Florida Blue", "payer_id": "00590", "type": "COMMERCIAL"}

        # First encounter: clean pass
        res1 = self.pipeline.process_encounter(
            raw_doc_or_content="Patient: CARLOS SANTANA. HTN I10. Plan: Continue meds.",
            doc_type=list(DocumentSourceType)[0],
            patient_data=shared_pat,
            provider_data=shared_prov,
            payer_data=shared_payer,
            encounter_id=enc_id_1,
            date_of_service="2026-09-20"
        )
        self.assertEqual(res1.get("status"), "TRANSMITTED_CLEAN_CLAIM")

        # Second encounter with identical patient, DOS, CPT, and NPI: MUST be flagged as duplicate
        res2 = self.pipeline.process_encounter(
            raw_doc_or_content="Patient: CARLOS SANTANA. HTN I10. Plan: Continue meds.",
            doc_type=list(DocumentSourceType)[0],
            patient_data=shared_pat,
            provider_data=shared_prov,
            payer_data=shared_payer,
            encounter_id=enc_id_2,
            date_of_service="2026-09-20"
        )
        self.assertEqual(res2.get("status"), "FLAGGED_FOR_HUMAN_REVIEW")
        issue_codes = [i["code"] for i in res2.get("scrutiny_issues", [])]
        self.assertIn("DUPLICATE_CLAIM_DETECTED", issue_codes, "Duplicate claim must be detected by Tier 8 Scrutiny")


if __name__ == "__main__":
    unittest.main()


