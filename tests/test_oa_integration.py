# ============================================
# FILE: tests/test_oa_integration.py
# PURPOSE: End-to-End Office Ally Companion Guide & X12 5010A1 EDI Compliance Tests
#          Covers:
#            - Loop 2310C Service Facility Location (with NPI, without NPI, & NPPES SNF lookup)
#            - Loop 2300 Box 18 Admission Date (DTP*435*D8*YYYYMMDD~)
#            - Loop 2410 FDA Drug Identification (LIN**N4*<11-digit-NDC>~ + CTP****<qty>*<unit>~)
#            - Loop 2010AA Solo Type 1 (NM1*85*1) vs Group Type 2 (NM1*85*2) & 9-digit ZIP+4
#            - Single-claim vs Batch 837P (generate_batch_837p) zero-drift fidelity
#            - pyx12 005010X222A1 schema validation with 0 errors across all configurations
#            - OfficeAllyCompanionValidator (all 8 pre-flight rule suites + live draft batch audit)
#            - X12 270/271 CAQH CORE 2.2.0 SOAP & SFTP formatting + 835 ERA SVC/CAS/LQ/PLB/Crossover
# ============================================

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ["OA_ALLOW_SIMULATION"] = "true"

from config import OfficeAllyConfig
from models.encounter import PatientInfo, PayerInfo
from transmission.x12_837_generator import (
    X12_837P_Generator,
    lookup_known_service_facility,
    SOUTH_FLORIDA_SNF_DIRECTORY,
)
from transmission.office_ally_companion import (
    OfficeAllyCompanionValidator,
    OFFICE_ALLY_OFFICIAL_PAYER_DIRECTORY,
    is_valid_cms_mbi,
)
from transmission.office_ally_dispatcher import OfficeAllyDispatcher
from eligibility.eligibility_engine import RealTimeEligibilityEngine, parse_271
from remittance.era_835_parser import ERA_835_Parser


def _clean_snf_encounter(**overrides):
    """Returns a realistic, clean South Florida SNF Medicare Part B encounter with zero placeholders."""
    base = {
        "patient_control_number": "ENC-OA-SNF-001",
        "date_of_service": "2026-10-19",
        "admission_date": "2026-10-18",
        "facility_name": "Glades West Rehabilitation and Nursing Center",
        "patient": {
            "first_name": "CAROL",
            "last_name": "BRATTON-LYNCH",
            "dob": "1961-04-05",
            "gender": "F",
            "member_id": "2R47DQ7TT56",
            "address": "3492 FOXCROFT RD APT 203",
            "city": "MIRAMAR",
            "state": "FL",
            "zip": "330254112",
        },
        "provider": {
            "organization_name": "SOUTH FLORIDA REHAB PHYSICIANS PA",
            "rendering_first_name": "NASSER",
            "rendering_last_name": "EFTEKHARI",
            "rendering_npi_type_1": "1356442776",
            "billing_npi_type_2": "1447249149",  # valid Luhn 10-digit NPI
            "npi": "1356442776",
            "tax_id": "650984321",
            "phone": "3054428100",
            "address": "806 S DOUGLAS RD STE 820",
            "city": "CORAL GABLES",
            "state": "FL",
            "zip": "33134-3157",
            "type": "MD",
            "taxonomy_code": "208100000X",
        },
        "payer": {
            "name": "MEDICARE FLORIDA (FIRST COAST)",
            "payer_id": "09102",
            "type": "MEDICARE",
            "address": "PO BOX 45098",
            "city": "JACKSONVILLE",
            "state": "FL",
            "zip": "322325098",
        },
        "referring_provider": {
            "first_name": "ESTHER",
            "last_name": "VILDOR-DAZIL",
            "npi": "1154454437",
        },
        "billing": {
            "total_charge": 160.00,
            "place_of_service": "31",
            "diagnosis_codes": [
                {"code": "C50.212", "description": "Malignant neoplasm of left breast"},
                {"code": "I10", "description": "Essential hypertension"},
                {"code": "M62.81", "description": "Muscle weakness (generalized)"},
            ],
            "cpt_codes": [
                {
                    "code": "99305",
                    "modifiers": [],
                    "charge": 160.00,
                    "units": 1,
                    "dx_pointers": [1, 2, 3],
                }
            ],
        },
    }
    base.update(overrides)
    return base


class TestSouthFloridaSnfDirectory(unittest.TestCase):
    """Verifies NPPES-verified NPI & USPS ZIP+4 directory for South Florida SNFs."""

    def test_verified_nppes_snf_lookup(self):
        glades = lookup_known_service_facility("GladesWestRehabilitation andNursingCenter")
        self.assertIsNotNone(glades)
        self.assertEqual(glades["npi"], "1669882411")
        self.assertEqual(glades["zip"], "330270301")
        self.assertEqual(glades["city"], "MIRAMAR")

        palmetto = lookup_known_service_facility("PalmettoSubacute Care Center")
        self.assertIsNotNone(palmetto)
        self.assertEqual(palmetto["npi"], "1780696336")
        self.assertEqual(palmetto["address"], "7600 SW 8TH ST")
        self.assertEqual(palmetto["zip"], "331444462")

        victoria = lookup_known_service_facility("VictoriaNursing&Rehabilitation Inc.")
        self.assertIsNotNone(victoria)
        self.assertEqual(victoria["npi"], "1447249149")
        self.assertEqual(victoria["address"], "955 NW 3RD ST")
        self.assertEqual(victoria["zip"], "331281274")


class TestX12837p5010a1Compliance(unittest.TestCase):
    """Validates Loop 2310C, DTP*435, Loop 2410 NDC, Solo vs Group 2010AA, and Batch parity via pyx12."""

    def setUp(self):
        self.cfg = OfficeAllyConfig(claims_submitter_id="SFLREHAB01", usage_indicator="P")
        self.gen = X12_837P_Generator(config=self.cfg)
        self.validator = OfficeAllyCompanionValidator(
            config=self.cfg, verified_submitter_ids={"SFLREHAB01"}
        )

    def test_loop_2310c_with_npi_and_dtp_435_passes_pyx12(self):
        """SNF claim with Loop 2310C (Glades West NPI 1669882411) and Box 18 DTP*435 passes pyx12 with 0 errors."""
        enc = _clean_snf_encounter()
        edi = self.gen.generate_837p(enc)

        self.assertIn("N4*CORAL GABLES*FL*331343157~", edi, "Hyphenated Billing ZIP+4 must strip hyphen to 9 digits")
        self.assertIn("DTP*435*D8*20261018~", edi, "Box 18 SNF admission date must emit DTP*435 in Loop 2300")
        self.assertIn(
            "NM1*77*2*GLADES WEST REHABILITATION AND NURSING CENTER*****XX*1669882411~",
            edi,
        )
        self.assertIn("N3*15955 BASS CREEK ROAD~", edi)
        self.assertIn("N4*MIRAMAR*FL*330270301~", edi)

        # Verify Loop 2300 DTP*435 precedes HI, and Loop 2310C follows 2310B PRV*PE
        self.assertLess(edi.index("DTP*435*D8*20261018~"), edi.index("HI*ABK:C50212"))
        self.assertLess(edi.index("PRV*PE*PXC*208100000X~"), edi.index("NM1*77*2*"))

        pyx12_errors = self.validator.run_pyx12_validation(edi)
        self.assertEqual(pyx12_errors, [], f"pyx12 failed on Loop 2310C + DTP*435: {pyx12_errors}")

        report = self.validator.validate_837p(edi, filename="SFLREHAB01_BAT1_837P.edi", run_pyx12=True)
        self.assertTrue(report["ready_for_office_ally"], f"Unexpected companion errors: {report['errors']}")

    def test_loop_2310c_without_facility_npi_omits_nm108_nm109_and_passes_pyx12(self):
        """When Service Facility NPI is unknown, NM108/NM109 must be omitted (`NM1*77*2*NAME~`) per P0809 syntax."""
        enc = _clean_snf_encounter(
            facility_name=None,
            service_facility={
                "name": "CORAL GABLES ASSISTED LIVING",
                "address": "1500 PONCE DE LEON BLVD",
                "city": "CORAL GABLES",
                "state": "FL",
                "zip": "331341234",
                # No NPI provided
            },
        )
        enc["billing"] = dict(enc["billing"], place_of_service="13")
        edi = self.gen.generate_837p(enc)

        self.assertIn("NM1*77*2*CORAL GABLES ASSISTED LIVING~", edi)
        self.assertNotIn("NM1*77*2*CORAL GABLES ASSISTED LIVING*****XX*", edi)

        pyx12_errors = self.validator.run_pyx12_validation(edi)
        self.assertEqual(pyx12_errors, [], f"pyx12 failed when 2310C NPI omitted: {pyx12_errors}")

    def test_loop_2410_fda_ndc_drug_identification_passes_pyx12(self):
        """J-code line with 11-digit FDA NDC, quantity, unit, and optional Rx# emits Loop 2410 and passes pyx12."""
        enc = _clean_snf_encounter()
        enc["billing"] = {
            "total_charge": 215.00,
            "place_of_service": "11",
            "diagnosis_codes": [{"code": "M17.11", "description": "Unilateral primary OA, right knee"}],
            "cpt_codes": [
                {
                    "code": "20610",
                    "modifiers": ["RT"],
                    "charge": 175.00,
                    "units": 1,
                    "dx_pointers": [1],
                },
                {
                    "code": "J3301",
                    "modifiers": [],
                    "charge": 40.00,
                    "units": 4,
                    "dx_pointers": [1],
                    "ndc": "00009-0348-01",  # 5-4-2 hyphenated Kenalog -> normalizes to 00009034801
                    "ndc_quantity": 1.0,
                    "ndc_unit": "ML",
                    "prescription_number": "RX998877",
                },
            ],
        }
        enc["facility_name"] = None
        enc["admission_date"] = None

        edi = self.gen.generate_837p(enc)
        self.assertIn("LIN**N4*00009034801~", edi)
        self.assertIn("CTP****1*ML~", edi)
        self.assertIn("REF*XZ*RX998877~", edi)

        pyx12_errors = self.validator.run_pyx12_validation(edi)
        self.assertEqual(pyx12_errors, [], f"pyx12 failed on Loop 2410 NDC: {pyx12_errors}")

        # Verify invalid NDC unit raises ValueError immediately
        bad_enc = _clean_snf_encounter()
        bad_enc["billing"] = dict(enc["billing"], cpt_codes=[{
            "code": "J3301", "charge": 40.0, "units": 1, "dx_pointers": [1],
            "ndc": "00009034801", "ndc_quantity": 1, "ndc_unit": "MG",  # MG is invalid in 5010A1 (must be ME)
        }])
        with self.assertRaises(ValueError):
            self.gen.generate_837p(bad_enc)

    def test_solo_type_1_billing_provider_loop_2010aa_passes_pyx12(self):
        """Solo practitioner Type 1 Billing Provider emits NM1*85*1*LAST*FIRST and passes pyx12."""
        enc = _clean_snf_encounter()
        enc["provider"] = {
            "organization_name": "",
            "is_solo_proprietor": True,
            "billing_entity_type": "1",
            "billing_last_name": "EFTEKHARI",
            "billing_first_name": "NASSER",
            "billing_middle_name": "M",
            "rendering_first_name": "NASSER",
            "rendering_last_name": "EFTEKHARI",
            "rendering_npi_type_1": "1356442776",
            "billing_npi_type_2": "1356442776",
            "tax_id": "650984321",
            "phone": "3054428100",
            "address": "806 S DOUGLAS RD STE 820",
            "city": "CORAL GABLES",
            "state": "FL",
            "zip": "331343157",
            "type": "MD",
            "taxonomy_code": "208100000X",
        }
        edi = self.gen.generate_837p(enc)
        self.assertIn("NM1*85*1*EFTEKHARI*NASSER*M***XX*1356442776~", edi)

        pyx12_errors = self.validator.run_pyx12_validation(edi)
        self.assertEqual(pyx12_errors, [], f"pyx12 failed on Solo Type 1 2010AA: {pyx12_errors}")

    def test_single_and_batch_837p_zero_segment_drift_and_pyx12_clean(self):
        """generate_837p and generate_batch_837p produce identical ST..SE bodies (modulo ST02) and pass pyx12."""
        enc1 = _clean_snf_encounter(patient_control_number="ENC-BAT-001")
        enc2 = _clean_snf_encounter(
            patient_control_number="ENC-BAT-002",
            facility_name="Victoria Nursing & Rehabilitation Inc.",
            admission_date="10/12/2026",
        )

        single_1 = self.gen.generate_837p(enc1)
        batch_from_dicts = self.gen.generate_batch_837p([enc1, enc2])
        batch_from_strings = self.gen.generate_batch_837p([single_1, self.gen.generate_837p(enc2)])

        self.assertEqual(self.validator.run_pyx12_validation(batch_from_dicts), [])
        self.assertEqual(self.validator.run_pyx12_validation(batch_from_strings), [])
        self.assertIn("GE*2*", batch_from_dicts)
        self.assertIn("NM1*77*2*VICTORIA NURSING AND REHABILITATION CENTER INC*****XX*1447249149~", batch_from_dicts)
        self.assertIn("DTP*435*D8*20261012~", batch_from_dicts)

        # Strip ST/SE/BHT timestamps to verify exact inner segment parity between single and batch
        def middle_segs(edi_str):
            lines = [ln for ln in edi_str.splitlines() if not ln.startswith(("ISA*", "GS*", "ST*", "BHT*", "SE*", "GE*", "IEA*"))]
            return lines

        batch_1_only = self.gen.generate_batch_837p([enc1])
        self.assertEqual(middle_segs(single_1), middle_segs(batch_1_only))


class TestOfficeAllyCompanionValidator(unittest.TestCase):
    """Tests all 8 pre-flight rejection rules in OfficeAllyCompanionValidator."""

    def test_cms_mbi_strict_positional_validator(self):
        self.assertTrue(is_valid_cms_mbi("2R47DQ7TT56"))
        self.assertTrue(is_valid_cms_mbi("1EG4TE5MK73"))
        # Restricted letters S, L, O, I, B, Z must be rejected
        self.assertFalse(is_valid_cms_mbi("2S47DQ7TT56"))  # S in pos 2
        self.assertFalse(is_valid_cms_mbi("2R47OQ7TT56"))  # O in pos 5
        self.assertFalse(is_valid_cms_mbi("0R47DQ7TT56"))  # 0 in pos 1
        self.assertFalse(is_valid_cms_mbi("JD12345"))      # commercial ID

    def test_catches_placeholders_5digit_zip_missing_2310c_bad_payer_and_oatest(self):
        """Validates that fake addresses, 5-digit 2010AA ZIP, missing 2310C on POS 31, bad MBI, and missing OATEST fire."""
        cfg = OfficeAllyConfig(claims_submitter_id="MAGNOLIA_HEALTH", usage_indicator="T")
        gen = X12_837P_Generator(config=cfg)
        dirty_enc = {
            "patient_control_number": "ENC-DIRTY-01",
            "date_of_service": "2026-10-19",
            "patient": {
                "first_name": "JOHN",
                "last_name": "DOE",
                "dob": "1950-01-01",
                "gender": "M",
                "member_id": "2S47OQ7TT56",  # invalid MBI (has S and O)
                "address": "450 Biscayne Blvd",  # placeholder street
                "city": "Miami",
                "state": "FL",
                "zip": "33132",
            },
            "provider": {
                "organization_name": "BILLING ENTITY NOT CONFIGURED",
                "rendering_first_name": "JANE",
                "rendering_last_name": "SMITH",
                "billing_npi_type_2": "1234567893",  # placeholder NPI
                "rendering_npi_type_1": "1234567893",
                "tax_id": "591234567",  # placeholder EIN
                "phone": "8005551212",  # placeholder phone
                "address": "1200 Brickell Ave",  # placeholder street
                "city": "Miami",
                "state": "FL",
                "zip": "33131",  # 5-digit ZIP in 2010AA (violates 9-digit rule)
                "type": "MD",
            },
            "payer": {
                "name": "Medicare Florida",
                "payer_id": "00901",  # legacy wrong ID (should be 09102)
            },
            "billing": {
                "total_charge": 160.00,
                "place_of_service": "31",  # SNF POS without Loop 2310C!
                "diagnosis_codes": [{"code": "I10", "description": "HTN"}],
                "cpt_codes": [{"code": "99309", "charge": 160.00, "units": 1, "dx_pointers": [1]}],
            },
        }
        edi = gen.generate_837p(dirty_enc)
        validator = OfficeAllyCompanionValidator(config=cfg)
        # Pass filename WITHOUT 'OATEST' while ISA15='T' -> must trigger OA_FILENAME_MISSING_OATEST
        res = validator.validate_837p(edi, filename="MAGNOLIA_BATCH_837P.edi", run_pyx12=True)

        self.assertFalse(res["ready_for_office_ally"])
        rule_ids = {e["rule_id"] for e in res["errors"]}
        self.assertIn("OA_FILENAME_MISSING_OATEST", rule_ids)
        self.assertIn("OA_PLACEHOLDER_SUBMITTER_ID", rule_ids)
        self.assertIn("OA_PLACEHOLDER_BILLING_NAME", rule_ids)
        self.assertIn("OA_PLACEHOLDER_BILLING_NPI", rule_ids)
        self.assertIn("OA_PLACEHOLDER_TAX_ID", rule_ids)
        self.assertIn("OA_PLACEHOLDER_PHONE", rule_ids)
        self.assertIn("OA_PLACEHOLDER_ADDRESS", rule_ids)
        self.assertIn("LOOP_2010AA_ZIP9_REQUIRED", rule_ids)
        self.assertIn("LOOP_2310C_MISSING_FOR_FACILITY_POS", rule_ids)
        self.assertIn("MEDICARE_INVALID_MBI", rule_ids)
        self.assertIn("OA_INVALID_PAYER_ID", rule_ids)

    def test_audits_live_draft_nov26_0_batch_file_if_present(self):
        """Audits exports/DRAFT_NOT_FOR_SUBMISSION_NOV26_0.837 and confirms pre-flight blocks unconfigured draft."""
        draft_path = os.path.join(PROJECT_ROOT, "exports", "DRAFT_NOT_FOR_SUBMISSION_NOV26_0.837")
        if not os.path.exists(draft_path):
            self.skipTest("DRAFT_NOT_FOR_SUBMISSION_NOV26_0.837 not present")
        validator = OfficeAllyCompanionValidator()
        res = validator.validate_837p_file(draft_path, run_pyx12=True)
        self.assertFalse(res["ready_for_office_ally"])
        self.assertGreaterEqual(res["claim_count"], 1)
        rule_ids = {e["rule_id"] for e in res["errors"]}
        self.assertIn("OA_PLACEHOLDER_BILLING_NPI", rule_ids)
        self.assertIn("LOOP_2010AA_ZIP9_REQUIRED", rule_ids)

    def test_official_payer_directory_contains_verified_florida_and_national_ids(self):
        for pid in ("09102", "77027", "60054", "87726", "61101", "62308", "BCBSF", "SIMPLY", "68069", "14163", "00324", "DEVOT", "65088"):
            self.assertIn(pid, OFFICE_ALLY_OFFICIAL_PAYER_DIRECTORY)
        self.assertEqual(
            OFFICE_ALLY_OFFICIAL_PAYER_DIRECTORY["65088"]["name"],
            "PREFERRED CARE PARTNERS (MEDICARE ADVANTAGE FL)",
        )
        self.assertEqual(OFFICE_ALLY_OFFICIAL_PAYER_DIRECTORY["65088"]["type"], "MEDICARE_ADVANTAGE")

    def test_non_numeric_ctp04_drug_quantity_emits_loop_2410_invalid_qty_without_crashing(self):
        """Incoming .837 with non-numeric CTP04 (e.g. CTP****ABC*ML~) must emit LOOP_2410_INVALID_QTY without ValueError."""
        cfg = OfficeAllyConfig(claims_submitter_id="SFLREHAB01", usage_indicator="P")
        gen = X12_837P_Generator(config=cfg)
        enc = _clean_snf_encounter(facility_name=None, admission_date=None)
        enc["billing"] = {
            "total_charge": 40.00,
            "place_of_service": "11",
            "diagnosis_codes": [{"code": "M17.11", "description": "Right knee OA"}],
            "cpt_codes": [{
                "code": "J3301",
                "modifiers": [],
                "charge": 40.00,
                "units": 1,
                "dx_pointers": [1],
                "ndc": "00009034801",
                "ndc_quantity": 1.0,
                "ndc_unit": "ML",
            }],
        }
        corrupted_edi = gen.generate_837p(enc).replace("CTP****1*ML~", "CTP****ABC*ML~")
        validator = OfficeAllyCompanionValidator(config=cfg, verified_submitter_ids={"SFLREHAB01"})
        res = validator.validate_837p(corrupted_edi, filename="SFLREHAB01_BAT1_837P.edi", run_pyx12=False)
        self.assertFalse(res["ready_for_office_ally"])
        rule_ids = {e["rule_id"] for e in res["errors"]}
        self.assertIn("LOOP_2410_INVALID_QTY", rule_ids)

    def test_dispatcher_auto_enforces_oatest_filename_when_isa15_is_test(self):
        """When ISA15='T', OfficeAllyDispatcher must automatically include 'OATEST_' in SFTP upload filename."""
        uploaded = {}

        class DummySftpClient:
            def upload_claim_file(self, content, batch_id, test=False):
                fn = f"{'OATEST_' if test else ''}SUB_{batch_id}_837P.edi"
                uploaded["filename"] = fn
                uploaded["test"] = test
                return {"filename": fn, "remote_path": f"/inbound/{fn}", "byte_size": len(content)}

        cfg = OfficeAllyConfig(
            claims_submitter_id="SFLREHAB01",
            sftp_username="user1",
            sftp_password="pw1",
            usage_indicator="T",
        )
        gen = X12_837P_Generator(config=cfg)
        edi = gen.generate_837p(_clean_snf_encounter())

        import tempfile
        from remittance.claim_ledger import ClaimLedger
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ClaimLedger(db_path=os.path.join(tmp, "test_ledger.sqlite3"))
            disp = OfficeAllyDispatcher(config=cfg, sftp_client=DummySftpClient(), ledger=ledger)
            out = disp.dispatch_batch(edi, batch_id="B001")
            self.assertTrue(uploaded["test"])
            self.assertTrue(out["transmission"]["filename"].startswith("OATEST_"))


class TestEligibility270And835EraUpgrades(unittest.TestCase):
    """Tests 270 segment terminators + BHT04/BHT05 split + CAQH CORE SOAP + 835 SVC/LQ/Crossover/PLB."""

    def test_270_inquiry_terminates_every_segment_and_splits_bht04_bht05(self):
        cfg = OfficeAllyConfig(
            claims_submitter_id="SFLREHAB01",
            eligibility_submitter_id="SFLREHAB_ELIG",
            soap_username="soap_user",
            soap_password="soap_pw",
            usage_indicator="T",
        )
        eng = RealTimeEligibilityEngine(config=cfg)
        pat = PatientInfo(first_name="CAROL", last_name="BRATTON-LYNCH", dob="1961-04-05", gender="F", member_id="2R47DQ7TT56")
        pyr = PayerInfo(name="MEDICARE FLORIDA", payer_id="09102")
        edi_270 = eng.build_270(pat, pyr, provider_npi="1356442776", provider_name="SOUTH FLORIDA REHAB")

        lines = [ln.strip() for ln in edi_270.splitlines() if ln.strip()]
        for ln in lines:
            self.assertTrue(ln.endswith("~"), f"Every 270 segment must end with '~', failed on: {ln}")

        bht_line = next(ln for ln in lines if ln.startswith("BHT*"))
        bht_parts = bht_line.rstrip("~").split("*")
        self.assertEqual(len(bht_parts), 6, f"BHT must have BHT01..BHT05 separate elements: {bht_line}")
        self.assertEqual(len(bht_parts[4]), 8, "BHT04 must be 8-digit CCYYMMDD")
        self.assertEqual(len(bht_parts[5]), 4, "BHT05 must be 4-digit HHMM")

        soap_xml = eng.build_core_soap_envelope(edi_270, payload_id="TEST-UUID-1234")
        self.assertIn("X12_270_Request_005010X279A1", soap_xml)
        self.assertIn("<ReceiverID>OFFALLY</ReceiverID>", soap_xml)
        self.assertIn("<CORERuleVersion>2.2.0</CORERuleVersion>", soap_xml)

        # Round-trip a CAQH CORE 271 SOAP response with Medicare Advantage MCO lock-in indicator (EB*R + NM1*PRP)
        sample_271 = (
            "ISA*00*          *00*          *30*330897513      *ZZ*SFLREHAB_ELIG  *261114*1200*^*00501*000000001*0*P*:~"
            "GS*HB*330897513*SFLREHAB_ELIG*20261114*1200*1*X*005010X279A1~"
            "ST*271*0001*005010X279A1~"
            "BHT*0022*11*RESP0001*20261114*1200~"
            "HL*1**20*1~NM1*PR*2*CMS MEDICARE*****PI*09102~"
            "HL*2*1*21*1~NM1*1P*2*SOUTH FLORIDA REHAB*****XX*1356442776~"
            "HL*3*2*22*0~NM1*IL*1*BRATTON-LYNCH*CAROL****MI*2R47DQ7TT56~"
            "DTP*346*D8*20260101~"
            "EB*1*IND*30*MB*MEDICARE PART B~"
            "EB*C*IND*30*MB**29*85.00~"
            "EB*R*IND*30*HN~"
            "NM1*PRP*2*HUMANA GOLD PLUS HMO*****PI*61101~"
            "SE*14*0001~GE*1*1~IEA*1*000000001~"
        )
        soap_resp = f"""<?xml version="1.0" encoding="utf-8"?>
        <soapenv:Envelope xmlns:soapenv="http://www.w3.org/2003/05/soap-envelope"
                          xmlns:cor="http://www.caqh.org/SOAP/WSDL/CORERule2.2.0.xsd">
          <soapenv:Body>
            <cor:COREEnvelopeRealTimeResponse>
              <PayloadType>X12_271_Response_005010X279A1</PayloadType>
              <PayloadID>TEST-UUID-1234</PayloadID>
              <ErrorCode>Success</ErrorCode>
              <ErrorMessage></ErrorMessage>
              <Payload>{sample_271}</Payload>
            </cor:COREEnvelopeRealTimeResponse>
          </soapenv:Body>
        </soapenv:Envelope>"""
        decoded = eng.parse_core_soap_response(soap_resp)
        self.assertEqual(decoded["error_code"], "Success")
        p271 = decoded["parsed_271"]
        self.assertTrue(p271["is_active"])
        self.assertEqual(p271["remaining_deductible"], 85.0)
        self.assertEqual(p271["other_payers"], [{"name": "HUMANA GOLD PLUS HMO", "payer_id": "61101"}])

    def test_835_parser_handles_single_line_tilde_stream_svc_lq_crossover_and_plb(self):
        """Verifies ERA_835_Parser parses single-line ~-delimited 835s with CLP*19 crossover, SVC, CAS, LQ*HE, and PLB."""
        raw_835 = (
            "ISA*00*          *00*          *30*330897513      *ZZ*SFLREHAB01     *261114*1200*^*00501*000000500*0*P*:~"
            "GS*HP*330897513*SFLREHAB01*20261114*1200*500*X*005010X221A1~"
            "ST*835*0001*005010X221A1~"
            "BPR*I*102.31*C*ACH*CCP*01*063100277*DA*123456789*1591234567**01*063100277*DA*987654321*20261114~"
            "TRN*1*EFT2026111499*1591234567~"
            "N1*PR*FIRST COAST SERVICE OPTIONS~"
            "N1*PE*SOUTH FLORIDA REHAB PHYSICIANS PA*XX*1447249149~"
            "CLP*ENC-OA-SNF-001*19*160.00*104.40*26.10*MB*ICN9988776655*31*1~"
            "NM1*QC*1*BRATTON-LYNCH*CAROL****MI*2R47DQ7TT56~"
            "NM1*TT*2*FLORIDA MEDICAID*****PI*77027~"
            "SVC*HC:99305*160.00*104.40**1~"
            "DTM*472*20261019~"
            "CAS*CO*45*29.50*1*253*2.09*1~"
            "CAS*PR*2*26.10*1~"
            "LQ*HE*N381~"
            "PLB*1447249149*20261231*72:2.09*2.09~"
            "SE*15*0001~GE*1*500~IEA*1*000000500~"
        )
        parser = ERA_835_Parser()
        era = parser.parse_835(raw_835)

        self.assertEqual(era["payer_name"], "FIRST COAST SERVICE OPTIONS")
        self.assertEqual(era["payee_npi"], "1447249149")
        self.assertEqual(era["check_trace_number"], "EFT2026111499")
        self.assertEqual(era["total_payment_amount"], 102.31)
        self.assertEqual(len(era["claims"]), 1)

        clm = era["claims"][0]
        self.assertEqual(clm["patient_control_number"], "ENC-OA-SNF-001")
        self.assertTrue(clm["is_crossover"])
        self.assertEqual(clm["crossover_carrier"], "FLORIDA MEDICAID")
        self.assertEqual(len(clm["service_lines"]), 1)
        self.assertEqual(clm["service_lines"][0]["procedure_code"], "99305")
        self.assertEqual(clm["service_lines"][0]["service_date"], "20261019")
        self.assertIn("N381", clm["remark_codes"])
        self.assertEqual(len(clm["adjustments"]), 3)
        self.assertEqual(len(era["provider_adjustments"]), 1)
        self.assertEqual(era["provider_adjustments"][0]["reason_code"], "72")


class TestPhase2ApiPortalAndBatchRunner(unittest.TestCase):
    """Phase 2 end-to-end verification of FastAPI assistant/OA preflight endpoints, Portal UI Tab 7, and batch runner."""

    def setUp(self):
        self._orig_env = dict(os.environ)
        os.environ["API_KEYS"] = "test-hipaa-oa-key"
        self._cleanup_paths = []

    def tearDown(self):
        for p in self._cleanup_paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_fastapi_assistant_ask_code_check_preflight_and_portal_tab7(self):
        """Verifies /api/v1/assistant/ask, /api/v1/assistant/code-check, /api/v1/office-ally/preflight, and /portal Tab 7."""
        from fastapi.testclient import TestClient
        from api import app

        client = TestClient(app)
        headers = {"X-API-Key": "test-hipaa-oa-key"}

        # 1. HIPAA auth guard: unauthenticated requests MUST be rejected (401)
        unauth = client.post("/api/v1/assistant/ask", json={"query": "SNF 99305"})
        self.assertEqual(unauth.status_code, 401)

        # 2. Verified Q&A query (SNF 99305 + QMB)
        ask_ok = client.post(
            "/api/v1/assistant/ask",
            headers=headers,
            json={"query": "Explain SNF 99305 POS 31 vs 32 and federal QMB balance billing prohibition"},
        )
        self.assertEqual(ask_ok.status_code, 200)
        ask_data = ask_ok.json()
        self.assertEqual(ask_data["status"], "VERIFIED")
        self.assertTrue(ask_data["verified"])
        self.assertGreater(len(ask_data["citations"]), 0)
        self.assertIn("1902(n)(3)(B)", ask_data["answer"])

        # 3. Anti-hallucination RVU / exact dollar refusal guard
        ask_abstain = client.post(
            "/api/v1/assistant/ask",
            headers=headers,
            json={"query": "What is the exact dollar reimbursement and wRVU value for CPT 99305?"},
        )
        self.assertEqual(ask_abstain.status_code, 200)
        abstain_data = ask_abstain.json()
        self.assertEqual(abstain_data["status"], "ABSTAINED_UNVERIFIED")
        self.assertFalse(abstain_data["verified"])
        self.assertEqual(abstain_data["citations"], [])

        # 4. Pre-submission code-check: Clean SNF 99305 + QMB shield
        cc_clean = client.post(
            "/api/v1/assistant/code-check",
            headers=headers,
            json={
                "cpt_codes": ["99305"],
                "icd10_codes": ["I69.351", "I10"],
                "pos": "31",
                "payer_type": "MEDICARE_QMB",
                "provider_type": "MD",
            },
        )
        self.assertEqual(cc_clean.status_code, 200)
        cc_clean_data = cc_clean.json()
        self.assertTrue(cc_clean_data["verified"], f"Unexpected issues: {cc_clean_data.get('issues')}")
        self.assertTrue(any("1902(n)(3)(B)" in n or "QMB" in n for n in cc_clean_data["reimbursement_and_payer_notes"]))

        # 5. Pre-submission code-check: Catches non-billable E11, G2211 in SNF, and J1100 missing NDC
        cc_err = client.post(
            "/api/v1/assistant/code-check",
            headers=headers,
            json={
                "cpt_codes": ["99305", "G2211", "J1100"],
                "icd10_codes": ["E11"],
                "pos": "31",
                "payer_type": "MEDICARE",
            },
        )
        self.assertEqual(cc_err.status_code, 200)
        cc_err_data = cc_err.json()
        self.assertFalse(cc_err_data["verified"])
        err_codes = {i["code"] for i in cc_err_data["issues"]}
        self.assertIn("ICD10_NON_BILLABLE_HEADER", err_codes)
        self.assertIn("G2211_INELIGIBLE", err_codes)
        self.assertIn("MISSING_OR_INVALID_FDA_NDC", err_codes)

        # 6. Office Ally 5010A1 Pre-Flight endpoint (clean OATEST SNF 837P vs draft placeholder)
        cfg = OfficeAllyConfig(
            claims_submitter_id="SFLREHAB01",
            usage_indicator="T",
        )
        gen = X12_837P_Generator(config=cfg)
        clean_837 = gen.generate_837p(_clean_snf_encounter())
        pf_ok = client.post(
            "/api/v1/office-ally/preflight",
            headers=headers,
            json={
                "edi_content": clean_837,
                "filename": "OATEST_SNF_API.837",
                "run_pyx12": True,
                "verified_submitter_ids": ["SFLREHAB01"],
            },
        )
        self.assertEqual(pf_ok.status_code, 200)
        pf_ok_data = pf_ok.json()
        self.assertTrue(pf_ok_data["ready_for_office_ally"], f"Unexpected errors: {pf_ok_data.get('errors')}")
        self.assertTrue(pf_ok_data["pyx12_clean"])
        self.assertEqual(pf_ok_data["error_count"], 0)

        pf_bad = client.post(
            "/api/v1/office-ally/preflight",
            headers=headers,
            json={
                "edi_content": clean_837.replace("806 S DOUGLAS RD STE 820", "1200 Brickell Ave").replace("331343157", "33131"),
                "filename": "DRAFT_API.837",
                "run_pyx12": True,
            },
        )
        self.assertEqual(pf_bad.status_code, 200)
        self.assertFalse(pf_bad.json()["ready_for_office_ally"])

        # 7. Portal HTML includes Tab 7 ("AI Coder & OA Pre-Flight Assistant (Zero-Hallucination)")
        portal_resp = client.get("/portal?api_key=test-hipaa-oa-key")
        self.assertEqual(portal_resp.status_code, 200)
        self.assertIn("AI Coder & OA Pre-Flight Assistant (Zero-Hallucination)", portal_resp.text)
        self.assertIn("/api/v1/assistant/ask", portal_resp.text)
        self.assertIn("/api/v1/assistant/code-check", portal_resp.text)
        self.assertIn("/api/v1/office-ally/preflight", portal_resp.text)

    def test_batch_runner_and_results_site_emit_208100000x_loop_2310c_dtp435_qmb_and_oa_preflight(self):
        """Verifies run_september_batch.run_batch + build_results_site emit 208100000X, Loop 2310C, DTP*435, QMB/Medicaid, and OA preflight."""
        import csv as csv_mod
        import json as json_mod
        import tempfile
        from scripts.run_september_batch import run_batch, validate_x12_837
        from scripts.build_results_site import audit_row, build_rows

        batch_prefix = "TSTOA99"
        slug = batch_prefix.lower()
        for fname in (
            f"exports/OATEST_OFFICE_ALLY_BATCH_{batch_prefix}.837",
            f"exports/OFFICE_ALLY_BATCH_{batch_prefix}.837",
            f"exports/DRAFT_NOT_FOR_SUBMISSION_{batch_prefix}.837",
            f"exports/{slug}_oa_preflight.json",
            f"exports/{slug}_overrides_template.json",
            f"exports/{slug}_cms1500_biller_sheet.csv",
            f"exports/{slug}_email_digest.html",
            f"exports/{slug}_biller_package.eml",
            f"exports/{slug}_batch_summary.json",
            f"exports/{slug}_batch_report.md",
            f"exports/batch_ledger_{batch_prefix}.sqlite3",
        ):
            self._cleanup_paths.append(fname)

        os.environ.update({
            "BILLING_ORG_NAME": "SOUTH FLORIDA REHAB PHYSICIANS PA",
            "BILLING_NPI": "1447249149",
            "BILLING_TAX_ID": "650987654",
            "BILLING_PHONE": "3054428100",
            "BILLING_ADDRESS": "3100 SW 145TH AVE STE 300",
            "BILLING_CITY": "MIRAMAR",
            "BILLING_STATE": "FL",
            "BILLING_ZIP": "330276620",
            "OA_CLAIMS_SUBMITTER_ID": "SFLREHAB01",
            "OA_USAGE_INDICATOR": "T",
        })

        synthetic_chart = (
            "GLADES WEST REHABILITATION AND NURSING CENTER\n"
            "15955 BASS CREEK ROAD, MIRAMAR, FL 330270301\n"
            "Resident: DOE, TESTPATIENT\n"
            "DOB: 04/12/1948   Sex: Female\n"
            "Admission Date: 10/14/2026\n"
            "Home Address: 6411 SW 22ND ST, MIRAMAR, FL 330232919\n"
            "Primary Payer: MEDICARE PART A & B   MBI: 2R47DQ7TT56\n"
            "Secondary Payer: FLORIDA MEDICAID (QMB / MCRA CO-PAY MEDICAID)   Medicaid ID: 1234567890\n"
            "Date of Service: 10/19/2026\n"
            "INITIAL PM&R CONSULTATION IN SKILLED NURSING FACILITY\n"
            "History & Exam: Comprehensive 40-minute bedside physiatry evaluation following acute ischemic stroke "
            "with right-sided hemiplegia (dominant side), essential hypertension, and dysphagia.\n"
            "Assessment & Plan:\n"
            "1. I69.351 Hemiplegia and hemiparesis following cerebral infarction affecting right dominant side - "
            "continue inpatient neuro-rehab protocol and spasticity monitoring.\n"
            "2. I10 Essential (primary) hypertension - continue home antihypertensive regimen, monitor BP.\n"
            "3. R13.10 Dysphagia, unspecified - speech therapy modified diet precautions.\n"
            "Total physician time on unit/floor and bedside today: 40 minutes (moderate-to-high MDM).\n"
            "Electronically Signed by: Nasser Eftekhari, MD (NPI: 1356442776)\n"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            # Place a minimal synthetic PDF placeholder + companion .ocr.txt (read_pdf_text prefers .ocr.txt)
            pdf_stub = os.path.join(tmpdir, "01_test_snf_qmb.pdf")
            ocr_path = pdf_stub + ".ocr.txt"
            with open(pdf_stub, "wb") as fh:
                fh.write(b"%PDF-1.4\n%EOF\n")
            with open(ocr_path, "w", encoding="utf-8") as fh:
                fh.write(synthetic_chart)
            overrides_path = os.path.join(tmpdir, "overrides.json")
            with open(overrides_path, "w", encoding="utf-8") as fh:
                json_mod.dump(
                    {
                        "01_test_snf_qmb.pdf": {
                            "first_name": "TESTPATIENT",
                            "last_name": "DOE",
                            "dob": "1948-04-12",
                            "gender": "F",
                            "member_id": "2R47DQ7TT56",
                            "medicaid_id": "1234567890",
                            "secondary_payer": "FL MEDICAID (77027)",
                            "is_qmb": True,
                            "address": "6411 SW 22ND ST",
                            "city": "MIRAMAR",
                            "state": "FL",
                            "zip": "330232919",
                            "admission_date": "2026-10-14",
                            "date_of_service": "2026-10-19",
                            "facility_name": "GLADES WEST REHABILITATION AND NURSING CENTER",
                            "place_of_service": "31",
                            "name_verified": True,
                            "primary_payer_confirmed": True,
                        }
                    },
                    fh,
                )

            out = run_batch(
                batch_prefix=batch_prefix,
                data_dir=tmpdir,
                overrides_path=overrides_path,
                coder_signoff="QA Test Signoff",
            )

            # 1. Verify consolidated .837 file name & segment fidelity
            self.assertEqual(out["batch_837_path"], f"exports/OATEST_OFFICE_ALLY_BATCH_{batch_prefix}.837")
            with open(out["batch_837_path"], "r", encoding="utf-8") as fh:
                edi = fh.read()
            self.assertIn("PRV*BI*PXC*208100000X~", edi)
            self.assertIn("PRV*PE*PXC*208100000X~", edi)
            self.assertIn("NM1*77*2*GLADES WEST REHABILITATION AND NURSING CENTER*****XX*1669882411~", edi)
            self.assertIn("N3*15955 BASS CREEK ROAD~", edi)
            self.assertIn("N4*MIRAMAR*FL*330270301~", edi)
            self.assertIn("DTP*435*D8*20261014~", edi)
            self.assertEqual(validate_x12_837(edi), [])

            # 2. Verify automatic OfficeAllyCompanionValidator preflight passed cleanly
            pf = out["oa_preflight"]
            self.assertTrue(pf["pyx12_clean"])
            self.assertTrue(pf["ready_for_office_ally"], f"Unexpected OA preflight errors: {pf.get('errors')}")
            self.assertEqual(pf["claim_count"], 1)

            # 3. Verify CSV surfaces Box 9a Medicaid ID, Box 9d Payer, QMB shield, Box 18 Admission, and Box 32a NPI
            with open(out["csv_path"], "r", encoding="utf-8") as fh:
                csv_rows = list(csv_mod.DictReader(fh))
            self.assertEqual(len(csv_rows), 1)
            row0 = csv_rows[0]
            self.assertEqual(row0["Box9a_Secondary_Medicaid_ID"], "1234567890")
            self.assertEqual(row0["Box9_QMB_Protection"], "QMB_DO_NOT_BALANCE_BILL")
            self.assertEqual(row0["Box18_SNF_Admission_Date"], "2026-10-14")
            self.assertEqual(row0["Box32a_Service_Facility_NPI"], "1669882411")

            # 4. Verify .eml defaults To zakiullahdanish@gmail.com and highlights QMB & 208100000X
            with open(out["eml_path"], "r", encoding="utf-8", errors="replace") as fh:
                eml_text = fh.read()
            self.assertIn("To: zakiullahdanish@gmail.com", eml_text)
            self.assertIn("QMB", eml_text)
            self.assertIn("208100000X", eml_text)

            # 5. Verify build_results_site does NOT flag FL Medicaid / QMB as SECONDARY_PAYER_NOISE
            from pathlib import Path
            site_rows, site_meta = build_rows(batch_prefix, data_dir=Path(tmpdir))
            self.assertEqual(len(site_rows), 1)
            flag_codes = {f["code"] for f in site_rows[0]["flags"]}
            self.assertNotIn("SECONDARY_PAYER_NOISE", flag_codes)
            self.assertTrue(any("Office Ally 5010A1 Pre-Flight" in g["msg"] for g in site_meta["global"]))


if __name__ == "__main__":
    unittest.main()
