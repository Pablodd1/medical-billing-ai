# ============================================
# FILE: medical_billing_ai/transmission/office_ally_companion.py
# PURPOSE: Deterministic Office Ally Pre-Flight Companion Validator &
#          Integration Readiness Engine (837P 005010X222A1 + CMS 5010A1 Front-End Edits)
# ============================================

import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from config import OfficeAllyConfig
except ImportError:
    from ..config import OfficeAllyConfig


# Official Office Ally Electronic Payer Directory (Verified Florida & National Carriers)
OFFICE_ALLY_OFFICIAL_PAYER_DIRECTORY: Dict[str, Dict[str, Any]] = {
    "09102": {
        "name": "First Coast Service Options (FCSO) - Medicare Part B Florida",
        "type": "MEDICARE",
        "filing_indicator": "MB",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": True,
        "notes": "Requires EDI enrollment with FCSO Jurisdiction N before live 837P/835 routing.",
    },
    "77027": {
        "name": "Florida Medicaid (AHCA / FLMMIS)",
        "type": "MEDICAID",
        "filing_indicator": "MC",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": True,
        "notes": "Requires linking Office Ally as Trading Partner/Agent on FLMMIS web portal.",
    },
    "60054": {
        "name": "Aetna Healthcare (Commercial / Medicare Advantage / Better Health)",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "87726": {
        "name": "UnitedHealthcare (UHC Commercial / Medicare Advantage / Community Plan)",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "61101": {
        "name": "Humana (Commercial / Gold Plus Medicare Advantage / Medicaid MMA)",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "62308": {
        "name": "Cigna Healthcare",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "BCBSF": {
        "name": "Florida Blue (Blue Cross Blue Shield of Florida)",
        "type": "COMMERCIAL",
        "filing_indicator": "BL",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "00590": {
        "name": "Florida Blue (BCBS of Florida - Legacy/Regional ID)",
        "type": "COMMERCIAL",
        "filing_indicator": "BL",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "SIMPLY": {
        "name": "Simply Healthcare Plans (Florida Medicaid MMA / Medicare Advantage)",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "SIMPLY-LTC": {
        "name": "Simply Healthcare Long Term Care (Florida Medicaid LTC)",
        "type": "MEDICAID",
        "filing_indicator": "MC",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "68069": {
        "name": "Sunshine Health (Centene Florida Medicaid MMA / Ambetter)",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "14163": {
        "name": "Wellcare Health Plans (Centene Medicare Advantage)",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "MLNA-FL": {
        "name": "Molina Healthcare of Florida",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "00324": {
        "name": "CarePlus Health Plans (Florida Medicare Advantage)",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "DEVOT": {
        "name": "Devoted Health Plans",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "59274": {
        "name": "AvMed Health Plans",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "A3565": {
        "name": "Leon Health Plans",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "94265": {
        "name": "Medica Health Plans",
        "type": "COMMERCIAL",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "59065": {
        "name": "Community Care Plan (South Florida Medicaid MMA)",
        "type": "MEDICAID",
        "filing_indicator": "MC",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "99727": {
        "name": "TRICARE East Region",
        "type": "COMMERCIAL",
        "filing_indicator": "CH",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": True,
    },
    "12807": {
        "name": "TRICARE For Life",
        "type": "COMMERCIAL",
        "filing_indicator": "CH",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": False,
    },
    "00882": {
        "name": "Railroad Medicare (Palmetto GBA)",
        "type": "MEDICARE",
        "filing_indicator": "MB",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "enrollment_required": True,
    },
    "65088": {
        "name": "PREFERRED CARE PARTNERS (MEDICARE ADVANTAGE FL)",
        "type": "MEDICARE_ADVANTAGE",
        "filing_indicator": "CI",
        "supports_837p": True,
        "supports_270": True,
        "supports_835": True,
        "elig_270": True,
        "era_835": True,
        "enrollment_required": False,
    },
}

DEPRECATED_OR_WRONG_PAYER_IDS: Dict[str, str] = {
    "00901": "Legacy/invalid FL Medicare code '00901'; use '09102' for First Coast Service Options (FCSO) Medicare Part B FL.",
    "SEC01": "Placeholder secondary payer ID 'SEC01'; replace with official Office Ally Payer ID (e.g., '77027' for FL Medicaid).",
}

# Strict CMS 11-character Medicare Beneficiary Identifier (MBI) regex:
# Pos 1: 1-9
# Pos 2, 5, 8, 9: A-Z excluding S, L, O, I, B, Z
# Pos 3, 6: 0-9 or A-Z excluding S, L, O, I, B, Z
# Pos 4, 7, 10, 11: 0-9
_MBI_ALPHA = "ACDEFGHJKMNPQRTUVWXY"
_MBI_ALNUM = "0123456789ACDEFGHJKMNPQRTUVWXY"
CMS_MBI_REGEX = re.compile(
    rf"^[1-9][{_MBI_ALPHA}][{_MBI_ALNUM}][0-9][{_MBI_ALPHA}][{_MBI_ALNUM}][0-9][{_MBI_ALPHA}]{{2}}[0-9]{{2}}$"
)

FACILITY_POS_REQUIRING_2310C: Set[str] = {"21", "22", "23", "31", "32"}
INPATIENT_SNF_POS_REQUIRING_ADMISSION_DATE: Set[str] = {"21", "31", "51", "61"}

PLACEHOLDER_STREETS = {
    "1200 BRICKELL AVE",
    "1200 BRICKELL AVE STE 400",
    "450 BISCAYNE BLVD",
    "NOT CONFIGURED",
    "100 MAIN ST",
}
PLACEHOLDER_TAX_IDS = {"123456789", "591234567", "000000000", "111111111", "999999999"}
PLACEHOLDER_NPIS = {"1234567890", "1234567893", "0000000000", "9876543210", "9999999999"}
PLACEHOLDER_PHONES = {"8005551212", "0000000000", "1234567890"}
PLACEHOLDER_SUBMITTER_IDS = {"MAGNOLIA_HEALTH", "SUBMITTER_ID", "NOT CONFIGURED", "TEST_SUBMITTER", "000000000"}
PLACEHOLDER_ORG_NAMES = {"BILLING ENTITY NOT CONFIGURED", "NOT CONFIGURED", "MEDICAL PRACTICE"}


def is_valid_cms_mbi(mbi: Optional[str]) -> bool:
    """Validates a Medicare Beneficiary Identifier against CMS 11-char positional specification."""
    if not mbi:
        return False
    cleaned = re.sub(r"[\s\-]", "", str(mbi).upper())
    return bool(CMS_MBI_REGEX.fullmatch(cleaned))


def is_valid_npi_luhn(npi: Optional[str]) -> bool:
    """Validates 10-digit NPI with CMS 80840 prefix Luhn check digit."""
    if not npi or not re.fullmatch(r"\d{10}", str(npi)):
        return False
    total = 0
    for i, ch in enumerate(reversed("80840" + str(npi))):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class OfficeAllyCompanionValidator:
    """
    Deterministic Pre-Flight Companion Validator & Integration Readiness Engine
    for Office Ally 837P (`005010X222A1`), SFTP (`ftp10.officeally.com:22`),
    Web Portal (`sc.officeally.com`), and CAQH CORE Real-Time SOAP (`wsd.officeally.com`).
    """

    def __init__(
        self,
        config: Optional[OfficeAllyConfig] = None,
        verified_submitter_ids: Optional[Set[str]] = None,
    ):
        self.config = config or OfficeAllyConfig()
        env_verified = {
            s.strip().upper()
            for s in os.getenv("OA_VERIFIED_SUBMITTER_IDS", "").split(",")
            if s.strip()
        }
        self.verified_submitter_ids: Set[str] = (
            {s.upper() for s in verified_submitter_ids} if verified_submitter_ids else set()
        ) | env_verified

    @staticmethod
    def _issue(
        rule_id: str,
        severity: str,
        message: str,
        claim_pcn: Optional[str] = None,
        segment: Optional[str] = None,
    ) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "rule_id": rule_id,
            "severity": severity,  # ERROR (blocks live OA submission) or WARNING (advisory)
            "message": message,
        }
        if claim_pcn:
            item["claim_pcn"] = claim_pcn
        if segment:
            item["segment"] = segment
        return item

    @staticmethod
    def _split_segments(edi_text: str) -> List[List[str]]:
        raw = edi_text.replace("\r\n", "\n")
        chunks = [c.strip() for c in raw.replace("~\n", "~").split("~") if c.strip()]
        return [c.split("*") for c in chunks]

    @staticmethod
    def run_pyx12_validation(edi_text: str) -> List[str]:
        """Executes pyx12 X12 5010A1 (`005010X222A1`) structural & syntax validation."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "preflight.837")
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(edi_text)
            proc = subprocess.run(
                [sys.executable, "-m", "pyx12.scripts.x12valid", path],
                capture_output=True,
                text=True,
                timeout=120,
            )
        err_lines = [
            ln.strip()
            for ln in (proc.stdout + proc.stderr).splitlines()
            if " ERROR " in ln or ln.strip().endswith("FAILED")
        ]
        if proc.returncode != 0 and not err_lines:
            err_lines = [f"pyx12 exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:240]}"]
        return err_lines

    def validate_837p_file(self, path: str, run_pyx12: bool = True) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
        return self.validate_837p(content, filename=os.path.basename(path), run_pyx12=run_pyx12)

    def validate_837p(
        self,
        edi_text: str,
        filename: Optional[str] = None,
        run_pyx12: bool = True,
    ) -> Dict[str, Any]:
        """
        Audits an `.837` string against all 8 Office Ally & CMS 5010A1 front-end rule suites:
          1. Envelope (`ISA07='30'`, `ISA08='330897513'`, `GS03='330897513'`, `NM1*40` `330897513`, `ISA15` vs `OATEST` filename)
          2. Placeholder / Fake Data Detector (blocks fake streets, EINs, NPIs, phones, unverified `MAGNOLIA_HEALTH`)
          3. Loop `2010AA` Billing Provider 9-digit ZIP+4 (no hyphen, not `0000` / `9999` dummy suffix) & PO Box block
          4. Loop `2310C` Service Facility check when `CLM05-1` POS in `{21, 22, 23, 31, 32}` + `DTP*435` admission check
          5. Subscriber MBI validation when `SBR09='MB'`
          6. Payer ID validation against Office Ally Official Payer List
          7. `pyx12` X12 5010A1 structural check
          8. Channel readiness diagnostics (Web Portal vs SFTP vs CAQH CORE SOAP)
        """
        issues: List[Dict[str, Any]] = []
        if not edi_text or not edi_text.strip():
            issues.append(self._issue("OA_EMPTY_FILE", "ERROR", "837P EDI payload is empty."))
            return self._finalize_report(issues, 0, filename, {})

        segs = self._split_segments(edi_text)
        envelope_meta: Dict[str, Any] = {}

        # ------------------------------------------------------------------
        # 1. Envelope Check (ISA / GS / IEA)
        # ------------------------------------------------------------------
        isa_segs = [s for s in segs if s[0] == "ISA"]
        gs_segs = [s for s in segs if s[0] == "GS"]
        ge_segs = [s for s in segs if s[0] == "GE"]
        iea_segs = [s for s in segs if s[0] == "IEA"]

        if not isa_segs:
            issues.append(self._issue("OA_ENV_MISSING_ISA", "ERROR", "Missing ISA interchange header segment."))
        else:
            isa = isa_segs[0]
            isa06 = isa[6].strip() if len(isa) > 6 else ""
            isa07 = isa[7].strip() if len(isa) > 7 else ""
            isa08 = isa[8].strip() if len(isa) > 8 else ""
            isa13 = isa[13].strip() if len(isa) > 13 else ""
            isa15 = isa[15].strip() if len(isa) > 15 else ""
            envelope_meta.update({
                "isa06_submitter_id": isa06,
                "isa07_receiver_qual": isa07,
                "isa08_receiver_id": isa08,
                "isa13_control_number": isa13,
                "isa15_usage_indicator": isa15,
            })

            if len(isa) > 6 and len(isa[6]) != 15:
                issues.append(self._issue("OA_ENV_ISA06_WIDTH", "ERROR", f"ISA06 must be fixed-width 15 chars (got {len(isa[6])}).", segment="ISA"))
            if len(isa) > 8 and len(isa[8]) != 15:
                issues.append(self._issue("OA_ENV_ISA08_WIDTH", "ERROR", f"ISA08 must be fixed-width 15 chars (got {len(isa[8])}).", segment="ISA"))
            if isa07 != "30":
                issues.append(self._issue("OA_ENV_ISA07_INVALID", "ERROR", f"Office Ally requires ISA07='30' (got '{isa07}').", segment="ISA"))
            if isa08 != "330897513":
                issues.append(self._issue("OA_ENV_ISA08_INVALID", "ERROR", f"Office Ally requires ISA08='330897513' (got '{isa08}').", segment="ISA"))
            if isa15 not in ("P", "T"):
                issues.append(self._issue("OA_ENV_ISA15_INVALID", "ERROR", f"ISA15 usage indicator must be 'P' or 'T' (got '{isa15}').", segment="ISA"))

            # Office Ally crucial rule: filename 'OATEST' routes test traffic, NOT ISA15!
            if filename:
                has_oatest = "OATEST" in filename
                if isa15 == "T" and not has_oatest and not filename.startswith("DRAFT"):
                    issues.append(self._issue(
                        "OA_FILENAME_MISSING_OATEST",
                        "ERROR",
                        f"ISA15='T' (test) but filename '{filename}' lacks 'OATEST'. Office Ally ignores ISA15 and will route this file to PRODUCTION payers unless 'OATEST' is in the filename!",
                        segment="ISA",
                    ))
                if isa15 == "P" and has_oatest:
                    issues.append(self._issue(
                        "OA_FILENAME_OATEST_MISMATCH",
                        "WARNING",
                        f"ISA15='P' (production) but filename '{filename}' contains 'OATEST'; Office Ally will treat this upload as a test file.",
                        segment="ISA",
                    ))
                if "837P" not in filename.upper() and not filename.lower().endswith(".837"):
                    issues.append(self._issue(
                        "OA_SFTP_FILENAME_TAG",
                        "WARNING",
                        f"Office Ally SFTP requires '837P' in the filename (got '{filename}').",
                    ))

            # Placeholder Submitter ID check
            if isa06.upper() in PLACEHOLDER_SUBMITTER_IDS and isa06.upper() not in self.verified_submitter_ids:
                issues.append(self._issue(
                    "OA_PLACEHOLDER_SUBMITTER_ID",
                    "ERROR",
                    f"Submitter ID ISA06='{isa06}' is an unverified default/placeholder. Configure your OA-assigned Sender/Submitter ID via OA_CLAIMS_SUBMITTER_ID (or mark verified if officially issued by OA).",
                    segment="ISA",
                ))

            if iea_segs:
                iea02 = iea_segs[0][2].strip() if len(iea_segs[0]) > 2 else ""
                if iea02 != isa13:
                    issues.append(self._issue("OA_ENV_IEA_MISMATCH", "ERROR", f"IEA02 ('{iea02}') does not match ISA13 ('{isa13}').", segment="IEA"))

        if not gs_segs:
            issues.append(self._issue("OA_ENV_MISSING_GS", "ERROR", "Missing GS functional group header segment."))
        else:
            gs = gs_segs[0]
            gs01 = gs[1].strip() if len(gs) > 1 else ""
            gs02 = gs[2].strip() if len(gs) > 2 else ""
            gs03 = gs[3].strip() if len(gs) > 3 else ""
            gs06 = gs[6].strip() if len(gs) > 6 else ""
            gs08 = gs[8].strip() if len(gs) > 8 else ""
            envelope_meta.update({"gs02_sender_id": gs02, "gs03_receiver_id": gs03, "gs06_control_number": gs06, "gs08_version": gs08})

            if gs01 != "HC":
                issues.append(self._issue("OA_ENV_GS01_INVALID", "ERROR", f"837P requires GS01='HC' (got '{gs01}').", segment="GS"))
            if gs03 != "330897513":
                issues.append(self._issue("OA_ENV_GS03_INVALID", "ERROR", f"Office Ally requires GS03='330897513' (got '{gs03}').", segment="GS"))
            if gs08 != "005010X222A1":
                issues.append(self._issue("OA_ENV_GS08_INVALID", "ERROR", f"837P 5010A1 requires GS08='005010X222A1' (got '{gs08}').", segment="GS"))
            if ge_segs:
                ge02 = ge_segs[0][2].strip() if len(ge_segs[0]) > 2 else ""
                if ge02 != gs06:
                    issues.append(self._issue("OA_ENV_GE_MISMATCH", "ERROR", f"GE02 ('{ge02}') does not match GS06 ('{gs06}').", segment="GE"))

        # ------------------------------------------------------------------
        # Split into individual ST..SE transaction sets & audit per-claim rules
        # ------------------------------------------------------------------
        transactions: List[List[List[str]]] = []
        cur_tx: Optional[List[List[str]]] = None
        for s in segs:
            if s[0] == "ST":
                cur_tx = [s]
            elif cur_tx is not None:
                cur_tx.append(s)
                if s[0] == "SE":
                    transactions.append(cur_tx)
                    cur_tx = None

        if ge_segs and len(ge_segs[0]) > 1:
            expected_tx = int(ge_segs[0][1]) if ge_segs[0][1].isdigit() else -1
            if expected_tx != len(transactions):
                issues.append(self._issue(
                    "OA_ENV_GE01_COUNT_MISMATCH",
                    "ERROR",
                    f"GE01 reports {expected_tx} transaction set(s), but {len(transactions)} ST..SE block(s) were found.",
                    segment="GE",
                ))

        for tx in transactions:
            self._audit_single_transaction(tx, issues)

        # ------------------------------------------------------------------
        # 7. pyx12 X12 5010A1 Structural Validation
        # ------------------------------------------------------------------
        pyx12_errors: List[str] = []
        if run_pyx12:
            pyx12_errors = self.run_pyx12_validation(edi_text)
            for err in pyx12_errors[:15]:
                issues.append(self._issue("PYX12_SCHEMA_ERROR", "ERROR", err))

        return self._finalize_report(
            issues=issues,
            claim_count=len(transactions),
            filename=filename,
            envelope_meta=envelope_meta,
            pyx12_errors=pyx12_errors,
        )

    def _audit_single_transaction(self, tx: List[List[str]], issues: List[Dict[str, Any]]) -> None:
        pcn = "UNKNOWN"
        for s in tx:
            if s[0] == "CLM" and len(s) > 1:
                pcn = s[1].strip()
                break

        # Verify ST..SE segment count
        st_seg = tx[0]
        se_seg = tx[-1]
        st02 = st_seg[2].strip() if len(st_seg) > 2 else ""
        if se_seg[0] == "SE":
            se01 = int(se_seg[1]) if len(se_seg) > 1 and se_seg[1].isdigit() else -1
            se02 = se_seg[2].strip() if len(se_seg) > 2 else ""
            if se01 != len(tx):
                issues.append(self._issue(
                    "X12_SE01_SEGMENT_COUNT_MISMATCH",
                    "ERROR",
                    f"SE01 segment count is {se01}, actual ST..SE count is {len(tx)}.",
                    claim_pcn=pcn,
                    segment="SE",
                ))
            if se02 != st02:
                issues.append(self._issue(
                    "X12_SE02_CONTROL_MISMATCH",
                    "ERROR",
                    f"SE02 ('{se02}') does not match ST02 ('{st02}').",
                    claim_pcn=pcn,
                    segment="SE",
                ))

        # State machine tracking current loop context
        context = "HEADER"
        hl_level = ""
        has_nm1_40 = False
        nm1_40_id = ""

        billing_addr = ""
        billing_zip = ""
        sbr_filing_indicator = ""
        primary_member_id = ""
        clm_pos = ""
        has_loop_2310c = False
        has_dtp_435 = False
        in_other_subscriber = False

        for s in tx:
            seg_id = s[0]

            if seg_id == "HL" and len(s) > 3:
                hl_level = s[3].strip()
                if hl_level == "20":
                    context = "2000A"
                elif hl_level == "22":
                    context = "2000B"
                elif hl_level == "23":
                    context = "2000C"

            elif seg_id == "SBR" and len(s) > 1:
                seq = s[1].strip()
                if seq == "P":
                    context = "2000B"
                    in_other_subscriber = False
                    sbr_filing_indicator = s[9].strip() if len(s) > 9 else ""
                else:
                    context = "2320"
                    in_other_subscriber = True

            elif seg_id == "CLM":
                context = "2300"
                in_other_subscriber = False
                if len(s) > 5:
                    comp = s[5].split(":")
                    clm_pos = comp[0].strip() if comp else ""

            elif seg_id == "LX":
                context = "2400"

            elif seg_id == "LIN":
                context = "2410"
                ndc_qual = s[2].strip() if len(s) > 2 else ""
                ndc_val = s[3].strip() if len(s) > 3 else ""
                if ndc_qual != "N4" or not re.fullmatch(r"\d{11}", ndc_val):
                    issues.append(self._issue(
                        "LOOP_2410_INVALID_NDC",
                        "ERROR",
                        f"Loop 2410 LIN segment must use qualifier 'N4' and an exact 11-digit FDA NDC without hyphens (got '{ndc_qual}':'{ndc_val}').",
                        claim_pcn=pcn,
                        segment="LIN",
                    ))

            elif seg_id == "CTP" and context == "2410":
                qty_val = s[4].strip() if len(s) > 4 else ""
                unit_val = s[5].split(":")[0].strip() if len(s) > 5 else ""
                try:
                    qty_num = float(qty_val) if qty_val else 0.0
                    qty_valid = qty_num > 0
                except ValueError:
                    qty_valid = False
                if not qty_val or not qty_valid:
                    issues.append(self._issue(
                        "LOOP_2410_INVALID_QTY",
                        "ERROR",
                        f"Loop 2410 CTP04 drug unit count must be > 0 (got '{qty_val}').",
                        claim_pcn=pcn,
                        segment="CTP",
                    ))
                if unit_val not in {"F2", "GR", "ME", "ML", "UN"}:
                    issues.append(self._issue(
                        "LOOP_2410_INVALID_UNIT",
                        "ERROR",
                        f"Loop 2410 CTP05-1 unit '{unit_val}' invalid; must be F2, GR, ME, ML, or UN.",
                        claim_pcn=pcn,
                        segment="CTP",
                    ))

            elif seg_id == "NM1" and len(s) > 1:
                ent = s[1].strip()
                ent_type = s[2].strip() if len(s) > 2 else ""
                name_last_or_org = s[3].strip() if len(s) > 3 else ""
                id_qual = s[8].strip() if len(s) > 8 else ""
                ident = s[9].strip() if len(s) > 9 else ""

                if ent == "41":
                    context = "1000A"
                    if name_last_or_org.upper() in PLACEHOLDER_ORG_NAMES:
                        issues.append(self._issue(
                            "OA_PLACEHOLDER_SUBMITTER_NAME",
                            "ERROR",
                            f"Loop 1000A Submitter name '{name_last_or_org}' is an unconfigured placeholder.",
                            claim_pcn=pcn,
                            segment="NM1*41",
                        ))
                elif ent == "40":
                    context = "1000B"
                    has_nm1_40 = True
                    nm1_40_id = ident
                    if ident != "330897513":
                        issues.append(self._issue(
                            "OA_RECEIVER_NM1_40_INVALID",
                            "ERROR",
                            f"Loop 1000B NM1*40 Receiver ID must be '330897513' for Office Ally (got '{ident}').",
                            claim_pcn=pcn,
                            segment="NM1*40",
                        ))
                elif ent == "85":
                    context = "2010AA"
                    if name_last_or_org.upper() in PLACEHOLDER_ORG_NAMES:
                        issues.append(self._issue(
                            "OA_PLACEHOLDER_BILLING_NAME",
                            "ERROR",
                            f"Loop 2010AA Billing Provider name '{name_last_or_org}' is an unconfigured placeholder.",
                            claim_pcn=pcn,
                            segment="NM1*85",
                        ))
                    if ent_type == "1" and not (len(s) > 4 and s[4].strip()):
                        issues.append(self._issue(
                            "LOOP_2010AA_SOLO_MISSING_FIRST_NAME",
                            "ERROR",
                            "Solo Billing Provider (NM1*85*1) requires First Name in NM104.",
                            claim_pcn=pcn,
                            segment="NM1*85",
                        ))
                    if ident in PLACEHOLDER_NPIS:
                        issues.append(self._issue(
                            "OA_PLACEHOLDER_BILLING_NPI",
                            "ERROR",
                            f"Loop 2010AA Billing NPI '{ident}' is a known test/placeholder NPI.",
                            claim_pcn=pcn,
                            segment="NM1*85",
                        ))
                    elif not is_valid_npi_luhn(ident):
                        issues.append(self._issue(
                            "LOOP_2010AA_INVALID_NPI",
                            "ERROR",
                            f"Loop 2010AA Billing NPI '{ident}' fails CMS Luhn check-digit validation.",
                            claim_pcn=pcn,
                            segment="NM1*85",
                        ))
                elif ent == "IL":
                    context = "2330A" if in_other_subscriber else "2010BA"
                    if not in_other_subscriber:
                        primary_member_id = ident
                elif ent == "PR":
                    context = "2330B" if in_other_subscriber else "2010BB"
                    self._audit_payer_id(ident, name_last_or_org, pcn, issues)
                elif ent == "82":
                    context = "2310B"
                    if ident in PLACEHOLDER_NPIS:
                        issues.append(self._issue(
                            "OA_PLACEHOLDER_RENDERING_NPI",
                            "ERROR",
                            f"Loop 2310B Rendering NPI '{ident}' is a known test/placeholder NPI.",
                            claim_pcn=pcn,
                            segment="NM1*82",
                        ))
                    elif not is_valid_npi_luhn(ident):
                        issues.append(self._issue(
                            "LOOP_2310B_INVALID_NPI",
                            "ERROR",
                            f"Loop 2310B Rendering NPI '{ident}' fails CMS Luhn check-digit validation.",
                            claim_pcn=pcn,
                            segment="NM1*82",
                        ))
                elif ent == "77":
                    context = "2310C"
                    has_loop_2310c = True
                    if id_qual or ident:
                        if id_qual != "XX" or not is_valid_npi_luhn(ident):
                            issues.append(self._issue(
                                "LOOP_2310C_INVALID_FACILITY_NPI",
                                "ERROR",
                                f"Loop 2310C Service Facility NPI ('{id_qual}'/'{ident}') is invalid; omit NM108/NM109 if unknown or supply valid 10-digit Luhn NPI.",
                                claim_pcn=pcn,
                                segment="NM1*77",
                            ))

            elif seg_id == "PER" and context == "1000A":
                for idx in (4, 6, 8):
                    if len(s) > idx and s[idx - 1].strip() == "TE":
                        ph = re.sub(r"\D", "", s[idx])
                        if ph in PLACEHOLDER_PHONES:
                            issues.append(self._issue(
                                "OA_PLACEHOLDER_PHONE",
                                "ERROR",
                                f"Loop 1000A Submitter phone '{s[idx]}' is a placeholder.",
                                claim_pcn=pcn,
                                segment="PER",
                            ))

            elif seg_id == "N3" and len(s) > 1:
                street_up = s[1].strip().upper()
                if street_up in PLACEHOLDER_STREETS:
                    issues.append(self._issue(
                        "OA_PLACEHOLDER_ADDRESS",
                        "ERROR",
                        f"Loop {context} street address '{s[1].strip()}' is a hardcoded placeholder/unconfigured address.",
                        claim_pcn=pcn,
                        segment="N3",
                    ))
                if context == "2010AA":
                    billing_addr = street_up
                    if re.search(r"\bP\.?\s*O\.?\s*BOX\b", street_up):
                        issues.append(self._issue(
                            "LOOP_2010AA_PO_BOX_PROHIBITED",
                            "ERROR",
                            f"CMS 5010A1 prohibits PO Box ('{s[1].strip()}') in Loop 2010AA Billing Provider physical address (use Loop 2010AB Pay-To Address for lockbox).",
                            claim_pcn=pcn,
                            segment="N3",
                        ))

            elif seg_id == "N4" and len(s) > 3:
                zip_val = s[3].strip()
                if context == "2010AA":
                    billing_zip = zip_val
                    # CMS 5010A1 TR3: Loop 2010AA N403 MUST be a full 9-digit USPS ZIP+4 without hyphen!
                    if "-" in zip_val or not re.fullmatch(r"\d{9}", zip_val):
                        issues.append(self._issue(
                            "LOOP_2010AA_ZIP9_REQUIRED",
                            "ERROR",
                            f"CMS/5010A1 Front-End Edit: Loop 2010AA Billing Provider N403 MUST be a 9-digit USPS ZIP+4 without hyphen (got '{zip_val}').",
                            claim_pcn=pcn,
                            segment="N4",
                        ))
                    elif zip_val.endswith(("0000", "9999")) or zip_val.startswith("00000"):
                        issues.append(self._issue(
                            "LOOP_2010AA_DUMMY_ZIP4",
                            "WARNING",
                            f"Loop 2010AA Billing Provider ZIP+4 '{zip_val}' uses generic '0000'/'9999' extension; verify exact USPS +4 delivery code to avoid MAC front-end edits.",
                            claim_pcn=pcn,
                            segment="N4",
                        ))
                elif context == "2310C":
                    if "-" in zip_val or not re.fullmatch(r"\d{5}(\d{4})?", zip_val):
                        issues.append(self._issue(
                            "LOOP_2310C_INVALID_ZIP",
                            "ERROR",
                            f"Loop 2310C Service Facility N403 ZIP '{zip_val}' must be 5 or 9 digits without hyphen.",
                            claim_pcn=pcn,
                            segment="N4",
                        ))
                    elif len(zip_val) == 5:
                        issues.append(self._issue(
                            "LOOP_2310C_ZIP9_RECOMMENDED",
                            "WARNING",
                            f"Loop 2310C Service Facility ZIP '{zip_val}' is 5 digits; 9-digit USPS ZIP+4 is strongly recommended by Medicare MACs.",
                            claim_pcn=pcn,
                            segment="N4",
                        ))

            elif seg_id == "REF" and context == "2010AA" and len(s) > 2:
                if s[1].strip() in ("EI", "SY"):
                    ein = re.sub(r"\D", "", s[2])
                    if len(ein) != 9 or ein in PLACEHOLDER_TAX_IDS:
                        issues.append(self._issue(
                            "OA_PLACEHOLDER_TAX_ID",
                            "ERROR",
                            f"Loop 2010AA Tax ID '{s[2].strip()}' is invalid or a hardcoded placeholder.",
                            claim_pcn=pcn,
                            segment="REF*EI",
                        ))

            elif seg_id == "DTP" and context == "2300" and len(s) > 1:
                if s[1].strip() == "435":
                    has_dtp_435 = True

        # Post-claim cross-segment rules:
        if not has_nm1_40:
            issues.append(self._issue(
                "OA_MISSING_NM1_40",
                "ERROR",
                "Missing Loop 1000B Receiver segment (NM1*40*2*OFFICE ALLY*****46*330897513~).",
                claim_pcn=pcn,
            ))

        # 4. Loop 2310C Service Facility check when CLM05-1 POS is 21, 22, 23, 31, 32
        if clm_pos in FACILITY_POS_REQUIRING_2310C and not has_loop_2310c:
            issues.append(self._issue(
                "LOOP_2310C_MISSING_FOR_FACILITY_POS",
                "ERROR",
                f"Claim POS is '{clm_pos}' (Facility), which differs from Billing Provider office, but Loop 2310C (NM1*77 Service Facility Location / Box 32) is missing.",
                claim_pcn=pcn,
                segment="CLM",
            ))

        if clm_pos in INPATIENT_SNF_POS_REQUIRING_ADMISSION_DATE and not has_dtp_435:
            issues.append(self._issue(
                "LOOP_2300_MISSING_ADMISSION_DATE_DTP435",
                "WARNING",
                f"Claim POS is '{clm_pos}' (Inpatient/SNF) without Loop 2300 DTP*435 Admission Date (CMS-1500 Box 18). Include admission_date to prevent inpatient/SNF front-end edits.",
                claim_pcn=pcn,
                segment="DTP*435",
            ))

        # 5. Subscriber MBI validation when SBR09='MB'
        if sbr_filing_indicator == "MB":
            if not is_valid_cms_mbi(primary_member_id):
                issues.append(self._issue(
                    "MEDICARE_INVALID_MBI",
                    "ERROR",
                    f"SBR09='MB' (Medicare Part B), but Subscriber ID '{primary_member_id}' fails strict 11-char CMS MBI format check.",
                    claim_pcn=pcn,
                    segment="NM1*IL",
                ))

    def _audit_payer_id(
        self,
        payer_id: str,
        payer_name: str,
        pcn: str,
        issues: List[Dict[str, Any]],
    ) -> None:
        pid = payer_id.strip().upper()
        if pid in DEPRECATED_OR_WRONG_PAYER_IDS:
            issues.append(self._issue(
                "OA_INVALID_PAYER_ID",
                "ERROR",
                DEPRECATED_OR_WRONG_PAYER_IDS[pid],
                claim_pcn=pcn,
                segment="NM1*PR",
            ))
            return
        if pid not in OFFICE_ALLY_OFFICIAL_PAYER_DIRECTORY:
            issues.append(self._issue(
                "OA_UNRECOGNIZED_PAYER_ID",
                "WARNING",
                f"Payer ID '{payer_id}' ('{payer_name}') is not in the verified Office Ally FL/National Payer Directory; verify against Office Ally's live Payer List before transmitting.",
                claim_pcn=pcn,
                segment="NM1*PR",
            ))

    def diagnose_channels(self) -> Dict[str, Any]:
        """
        8. Channel Readiness Diagnostics across all 3 Office Ally integration workflows:
           - Channel A: Office Ally Service Center Web Upload (`https://sc.officeally.com` -> Upload Claims -> Online Claim Fix)
           - Channel B: Automated SFTP Batch (`ftp10.officeally.com:22`, `/inbound` & `/outbound`)
           - Channel C: CAQH CORE 2.2.0 Real-Time 270/271 SOAP (`https://wsd.officeally.com/TransactionService/rtx.svc`)
        """
        sub_ok = not self.config.is_placeholder_submitter_id or (
            self.config.claims_submitter_id.upper() in self.verified_submitter_ids
        )
        sftp_ready = bool(self.config.has_sftp_credentials and sub_ok)
        soap_ready = bool(self.config.has_soap_credentials and (self.config.eligibility_submitter_id or sub_ok))

        return {
            "web_portal_service_center": {
                "url": self.config.service_center_url,
                "workflow": "Login -> Upload Claims -> Upload Professional (837P) -> Manage Claims / Online Claim Fix",
                "ready_for_manual_upload": sub_ok,
                "requires": [] if sub_ok else ["Set real Office Ally Sender/Submitter ID in OA_CLAIMS_SUBMITTER_ID + Box 33 Billing Entity env vars"],
                "advantages": "Allows visual CMS-1500 inspection inside Office Ally Online Claim Fix queue before release.",
            },
            "sftp_batch_clearinghouse": {
                "host": self.config.sftp_host,
                "port": self.config.sftp_port,
                "inbound_dir": self.config.inbound_dir,
                "outbound_dir": self.config.outbound_dir,
                "ready": sftp_ready,
                "credentials_configured": self.config.has_sftp_credentials,
                "submitter_id_verified": sub_ok,
                "test_routing_rule": "Include 'OATEST' (case-sensitive) in filename for test uploads; ISA15 alone does NOT prevent live payer routing.",
                "outbound_reports": ["TA1 (Interchange)", "999 (Functional Ack)", "277CA (Claim-Level Scrub)", "835 (ERA Remittance)"],
                "missing_prerequisites": [
                    p for p, cond in [
                        ("OA_SFTP_USERNAME / OA_SFTP_PASSWORD", not self.config.has_sftp_credentials),
                        ("Verified OA_CLAIMS_SUBMITTER_ID", not sub_ok),
                    ] if cond
                ],
            },
            "caqh_core_soap_realtime_270_271": {
                "endpoint": self.config.soap_realtime_url,
                "legacy_asmx": self.config.soap_legacy_url,
                "core_rule_version": self.config.core_rule_version,
                "receiver_id": self.config.core_receiver_id,
                "ready": soap_ready,
                "credentials_configured": self.config.has_soap_credentials,
                "separate_eligibility_submitter_id_set": bool(self.config.eligibility_submitter_id),
                "missing_prerequisites": [
                    p for p, cond in [
                        ("OA_SOAP_USERNAME / OA_SOAP_PASSWORD (or SFTP creds)", not self.config.has_soap_credentials),
                        ("OA_ELIGIBILITY_SUBMITTER_ID", not self.config.eligibility_submitter_id),
                    ] if cond
                ],
            },
        }

    def _finalize_report(
        self,
        issues: List[Dict[str, Any]],
        claim_count: int,
        filename: Optional[str],
        envelope_meta: Dict[str, Any],
        pyx12_errors: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        errors = [i for i in issues if i["severity"] == "ERROR"]
        warnings = [i for i in issues if i["severity"] == "WARNING"]
        channels = self.diagnose_channels()
        return {
            "ready_for_office_ally": len(errors) == 0,
            "pyx12_clean": len(pyx12_errors or []) == 0,
            "filename": filename,
            "claim_count": claim_count,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "envelope": envelope_meta,
            "errors": errors,
            "warnings": warnings,
            "issues": issues,
            "channels": channels,
        }
