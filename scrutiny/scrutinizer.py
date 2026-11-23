import hashlib
from datetime import datetime
from typing import Dict, List, Any, Tuple, Optional, Set
import re

VALID_NDC_UNITS = {"UN", "ML", "GR", "F2", "ME"}
HCPCS_DRUG_CODE_RE = re.compile(
    r"^(?:J\d{4}|Q(?:01[3-8]\d|20\d{2}|40[78]\d|51\d{2}|99[5-9]\d)|S0[01]\d{2})$",
    re.IGNORECASE,
)


def normalize_fda_ndc_11(ndc: Optional[str]) -> Optional[str]:
    """Deterministically converts a HIPAA/FDA National Drug Code into an 11-digit 5-4-2 string
    (no hyphens) for X12 837P Loop 2410 (LIN*N4).
    Supports:
      - 11-digit unhyphenated or 5-4-2 hyphenated (e.g. '00002-7510-01' -> '00002751001')
      - 10-digit 4-4-2 hyphenated (e.g. '1234-5678-90' -> '01234567890')
      - 10-digit 5-3-2 hyphenated (e.g. '12345-678-90' -> '12345067890')
      - 10-digit 5-4-1 hyphenated (e.g. '12345-6789-0' -> '12345678900')
    Rejects None, non-digits, all-zeros, or ambiguous unhyphenated 10-digit strings."""
    if not ndc or not isinstance(ndc, (str, int)):
        return None
    raw = re.sub(r"^N4[\s:\*]*", "", str(ndc).strip(), flags=re.IGNORECASE).strip()
    if not raw:
        return None

    if "-" in raw:
        parts = raw.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            return None
        s1, s2, s3 = parts
        shape = (len(s1), len(s2), len(s3))
        if shape == (5, 4, 2):
            res = f"{s1}{s2}{s3}"
        elif shape == (4, 4, 2):
            res = f"0{s1}{s2}{s3}"
        elif shape == (5, 3, 2):
            res = f"{s1}0{s2}{s3}"
        elif shape == (5, 4, 1):
            res = f"{s1}{s2}0{s3}"
        else:
            return None
    else:
        if not re.fullmatch(r"\d{11}", raw):
            return None
        res = raw

    if res == "00000000000":
        return None
    return res

class ScrutinyResult:
    def __init__(self, passed: bool, confidence_score: float, issues: List[Dict[str, Any]], clean_claim_data: Dict[str, Any] = None, claim_fingerprint: str = "", timely_filing: Dict[str, Any] = None):
        self.passed = passed
        self.confidence_score = confidence_score
        self.issues = issues
        self.clean_claim_data = clean_claim_data or {}
        self.claim_fingerprint = claim_fingerprint
        self.timely_filing = timely_filing or {}

    def to_dict(self):
        return {
            "passed": self.passed,
            "confidence_score": self.confidence_score,
            "issue_count": len(self.issues),
            "issues": self.issues,
            "claim_fingerprint": self.claim_fingerprint,
            "timely_filing": self.timely_filing,
            "clean_claim_data": self.clean_claim_data
        }


class MedicalBillingScrutinizer:
    """
    Autonomous Medical Billing & Clinical Note Scrutinizer.
    Enforces Florida Statutes, FCSO Medicare LCD/NCD, NCCI PTP/MUE, AMA MDM Guidelines,
    Payer Timely Filing Limits, and Duplicate Claim Fingerprinting.
    """

    def __init__(self, ledger: Optional[Any] = None):
        self.ledger = ledger
        self.submitted_claims_fingerprints: Set[str] = set()
        if self.ledger and hasattr(self.ledger, "get_all_fingerprints"):
            try:
                self.submitted_claims_fingerprints.update(self.ledger.get_all_fingerprints())
            except Exception:
                pass

    def register_submitted_fingerprint(self, fingerprint: str, pcn: str = "") -> None:
        """Registers a submitted claim fingerprint in memory and persistent database."""
        if not fingerprint:
            return
        self.submitted_claims_fingerprints.add(fingerprint)
        if self.ledger and hasattr(self.ledger, "register_fingerprint"):
            try:
                self.ledger.register_fingerprint(fingerprint, pcn)
            except Exception:
                pass


    FCSO_LCD_RULES = {
        "82306": {
            "lcd_id": "L36063",
            "allowed_dx": ["E55.9", "E55.0", "M81.0", "N18.3", "N18.4", "N18.5", "E21.0"],
            "description": "Vitamin D; 25 hydroxy, includes fraction(s), if performed"
        },
        "20610": {
            "lcd_id": "L36805",
            "allowed_dx": ["M17.0", "M17.11", "M17.12", "M17.9", "M16.0", "M16.11", "M16.12", "M16.9", "M25.561", "M25.562"],
            "description": "Aspiration/injection intermediate or major joint"
        },
        "17000": {
            "lcd_id": "L33947",
            "allowed_dx": ["L57.0"],
            "description": "Destruction premalignant lesions; first lesion"
        }
    }

    NCCI_PTP_EDITS = {
        ("99214", "17000"): {"allowed_with_modifier": True, "required_modifier": "25", "reason": "E/M with minor surgical procedure requires significant separate identifiable service"},
        ("99213", "17000"): {"allowed_with_modifier": True, "required_modifier": "25", "reason": "E/M with minor surgical procedure requires significant separate identifiable service"},
        ("99214", "20610"): {"allowed_with_modifier": True, "required_modifier": "25", "reason": "E/M with major joint injection requires separate identifiable E/M service"},
        ("99213", "20610"): {"allowed_with_modifier": True, "required_modifier": "25", "reason": "E/M with major joint injection requires separate identifiable E/M service"},
        ("99305", "20610"): {"allowed_with_modifier": True, "required_modifier": "25", "reason": "SNF E/M with major joint injection requires separate identifiable E/M service"},
        ("99309", "20610"): {"allowed_with_modifier": True, "required_modifier": "25", "reason": "SNF E/M with major joint injection requires separate identifiable E/M service"},
        ("99305", "17000"): {"allowed_with_modifier": True, "required_modifier": "25", "reason": "SNF E/M with minor surgical procedure requires separate identifiable E/M service"},
        ("99309", "17000"): {"allowed_with_modifier": True, "required_modifier": "25", "reason": "SNF E/M with minor surgical procedure requires separate identifiable E/M service"},
        ("11042", "97597"): {"allowed_with_modifier": False, "required_modifier": None, "reason": "Mutually exclusive wound debridement services"},
        ("99304", "99307"): {"allowed_with_modifier": False, "required_modifier": None, "reason": "Initial and Subsequent Nursing Facility E/M cannot be billed together on same date of service"},
        ("99305", "99308"): {"allowed_with_modifier": False, "required_modifier": None, "reason": "Initial and Subsequent Nursing Facility E/M cannot be billed together on same date of service"},
        ("99305", "99309"): {"allowed_with_modifier": False, "required_modifier": None, "reason": "Initial and Subsequent Nursing Facility E/M cannot be billed together on same date of service"},
        ("99306", "99309"): {"allowed_with_modifier": False, "required_modifier": None, "reason": "Initial and Subsequent Nursing Facility E/M cannot be billed together on same date of service"},
        ("99306", "99310"): {"allowed_with_modifier": False, "required_modifier": None, "reason": "Initial and Subsequent Nursing Facility E/M cannot be billed together on same date of service"},
    }

    MUE_LIMITS = {
        "99202": 1, "99203": 1, "99204": 1, "99205": 1,
        "99212": 1, "99213": 1, "99214": 1, "99215": 1,
        "99304": 1, "99305": 1, "99306": 1,
        "99307": 1, "99308": 1, "99309": 1, "99310": 1,
        "99341": 1, "99342": 1, "99344": 1, "99345": 1,
        "99347": 1, "99348": 1, "99349": 1, "99350": 1,
        "G2211": 1,
        "17000": 1,
        "20610": 2,
        "82306": 1
    }

    VALID_POS_CODES = {
        "02": "Telehealth Other than Patient's Home",
        "10": "Telehealth in Patient's Home",
        "11": "Office",
        "12": "Home / Domiciliary",
        "13": "Assisted Living Facility",
        "14": "Group Home",
        "16": "Temporary Lodging",
        "20": "Urgent Care Facility",
        "21": "Inpatient Hospital",
        "22": "On Campus Outpatient Hospital",
        "23": "Emergency Room",
        "24": "Ambulatory Surgical Center",
        "31": "Skilled Nursing Facility",
        "32": "Nursing Facility",
        "33": "Custodial Care Facility",
        "34": "Hospice",
    }

    normalize_fda_ndc_11 = staticmethod(normalize_fda_ndc_11)

    def scrutinize_encounter(self, encounter: Dict[str, Any]) -> ScrutinyResult:
        issues = []
        confidence = 1.0

        provider = encounter.get("provider", {})
        patient = encounter.get("patient", {})
        clinical_note = encounter.get("clinical_note", {})
        billing = encounter.get("billing", {})
        payer = encounter.get("payer", {})
        dos = encounter.get("date_of_service", datetime.now().strftime("%Y-%m-%d"))

        # TIER 1: Clinical Note Completeness, Diagnosis Presence & E/M MDM Scrutiny
        dx_codes = billing.get("diagnosis_codes", [])
        if not dx_codes:
            issues.append({
                "tier": "Tier 1: Clinical Documentation & Coding Integrity",
                "severity": "CRITICAL",
                "code": "MISSING_DIAGNOSES",
                "message": "Encounter has no valid ICD-10 diagnosis codes. Cannot bill encounter without primary diagnosis."
            })
            confidence -= 0.50

        cpt_codes = billing.get("cpt_codes", [])
        em_code = cpt_codes[0].get("code", "") if cpt_codes and isinstance(cpt_codes[0], dict) else (cpt_codes[0].code if cpt_codes else "")
        if em_code.startswith("992") or re.match(r"^9930[4-9]|99310$", em_code):
            mdm_issues, mdm_penalty = self._validate_em_documentation(em_code, clinical_note, billing)
            issues.extend(mdm_issues)
            confidence -= mdm_penalty

        # TIER 2: Florida Scope of Practice, QMB Federal Protection & Credentialing
        fl_issues, fl_penalty = self._validate_florida_scope_of_practice(provider, billing, clinical_note, payer, patient)
        issues.extend(fl_issues)
        confidence -= fl_penalty

        # TIER 3: Medical Necessity & FCSO Florida LCD/NCD + FDA NDC Drug/Biologic Rules
        med_issues, med_penalty = self._validate_medical_necessity(billing, payer)
        issues.extend(med_issues)
        confidence -= med_penalty

        ndc_issues, ndc_penalty = self._validate_fda_ndc_compliance(billing)
        issues.extend(ndc_issues)
        confidence -= ndc_penalty

        # TIER 4: NCCI Edits (PTP Bundling, G2211 Add-On & MUE limits)
        ncci_issues, ncci_penalty = self._validate_ncci_edits(billing, clinical_note)
        issues.extend(ncci_issues)
        confidence -= ncci_penalty

        # TIER 5: WEDI SNIP & Demographic Completeness
        snip_issues, snip_penalty = self._validate_wedi_snip(patient, provider, billing, payer)
        issues.extend(snip_issues)
        confidence -= snip_penalty

        # TIER 6: Place of Service (POS) Classification & CPT Setting Compatibility
        pos_issues, pos_penalty = self._validate_place_of_service(billing, clinical_note)
        issues.extend(pos_issues)
        confidence -= pos_penalty

        # TIER 7: Payer Timely Filing Deadline Monitoring
        timely_res, timely_issues, timely_penalty = self._validate_timely_filing(dos, payer)
        issues.extend(timely_issues)
        confidence -= timely_penalty

        # TIER 8: Duplicate Claim Fingerprint & Hash Guard
        fingerprint, dup_issues, dup_penalty = self._validate_duplicate_claims(patient, dos, billing, provider)
        issues.extend(dup_issues)
        confidence -= dup_penalty

        confidence = max(0.0, min(1.0, round(confidence, 2)))
        passed = (len([i for i in issues if i["severity"] == "CRITICAL"]) == 0) and (confidence >= 0.90)

        if passed:
            self.register_submitted_fingerprint(fingerprint, encounter.get("encounter_id", ""))

        clean_claim = encounter if passed else {}

        return ScrutinyResult(
            passed=passed,
            confidence_score=confidence,
            issues=issues,
            clean_claim_data=clean_claim,
            claim_fingerprint=fingerprint,
            timely_filing=timely_res
        )

    def _validate_place_of_service(self, billing: Dict[str, Any], note: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0
        pos = str(billing.get("place_of_service", "11")).strip()

        if pos not in self.VALID_POS_CODES:
            issues.append({
                "tier": "Tier 6: Place of Service (POS)",
                "severity": "CRITICAL",
                "code": "INVALID_POS_CODE",
                "message": f"Place of Service code '{pos}' is not a valid standard HIPAA POS code."
            })
            penalty += 0.25
            return issues, penalty

        for item in billing.get("cpt_codes", []):
            code = str(item.get("code") if isinstance(item, dict) else item.code).strip().upper()
            if re.match(r"^(?:9930[4-9]|99310)$", code) and pos not in ("31", "32", "02"):
                issues.append({
                    "tier": "Tier 6: Place of Service (POS)",
                    "severity": "CRITICAL",
                    "code": "POS_CPT_MISMATCH",
                    "message": f"Nursing Facility E/M CPT {code} cannot be billed with POS '{pos}' ({self.VALID_POS_CODES.get(pos)}). Expected POS 31, 32, or 02 (Telehealth)."
                })
                penalty += 0.35
            elif re.match(r"^992[01][1-5]$", code) and pos in ("21", "31", "32"):
                issues.append({
                    "tier": "Tier 6: Place of Service (POS)",
                    "severity": "CRITICAL",
                    "code": "POS_CPT_MISMATCH",
                    "message": f"Office/Outpatient E/M CPT {code} cannot be billed in institutional facility POS '{pos}' ({self.VALID_POS_CODES.get(pos)})."
                })
                penalty += 0.35
            elif re.match(r"^(?:9934[1245]|9934[7-9]|99350)$", code) and pos in ("11", "21", "31", "32"):
                issues.append({
                    "tier": "Tier 6: Place of Service (POS)",
                    "severity": "CRITICAL",
                    "code": "POS_CPT_MISMATCH",
                    "message": f"Home/Residence E/M CPT {code} cannot be billed with POS '{pos}' ({self.VALID_POS_CODES.get(pos)}). Expected POS 12, 13, 14, 16, 02, or 10."
                })
                penalty += 0.35

        return issues, penalty

    def _validate_timely_filing(self, dos_str: str, payer: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0
        
        deadline_days = payer.get("timely_filing_days", 180)
        payer_type = str(payer.get("type", "")).upper()
        if "MEDICARE" in payer_type or "MEDICARE" in str(payer.get("name", "")).upper():
            deadline_days = 365
        elif "MEDICAID" in payer_type or "MEDICAID" in str(payer.get("name", "")).upper():
            deadline_days = min(deadline_days, 180)

        days_elapsed = 0
        try:
            dos_date = datetime.strptime(dos_str, "%Y-%m-%d").date()
            today = datetime.now().date()
            days_elapsed = (today - dos_date).days
            if days_elapsed < 0:
                issues.append({
                    "tier": "Tier 7: Timely Filing",
                    "severity": "CRITICAL",
                    "code": "FUTURE_DATE_OF_SERVICE",
                    "message": f"Claim DOS ({dos_str}) is in the future. Cannot bill unrendered services."
                })
                penalty += 0.50
        except Exception:
            issues.append({
                "tier": "Tier 7: Timely Filing",
                "severity": "CRITICAL",
                "code": "UNPARSABLE_DOS",
                "message": f"Date of Service '{dos_str}' is invalid or unparsable. Expected YYYY-MM-DD format."
            })
            penalty += 0.40
            days_elapsed = 0

        days_remaining = deadline_days - days_elapsed
        status = "ON_TIME"

        if days_elapsed > deadline_days:
            status = "EXPIRED"
            issues.append({
                "tier": "Tier 7: Timely Filing",
                "severity": "CRITICAL",
                "code": "TIMELY_FILING_EXPIRED",
                "message": f"Claim DOS ({dos_str}) is {days_elapsed} days old, exceeding {payer.get('name', 'Payer')} limit of {deadline_days} days. High CARC 29 denial risk."
            })
            penalty += 0.50
        elif 0 <= days_remaining <= 30:
            status = "WARNING_NEARING_DEADLINE"
            issues.append({
                "tier": "Tier 7: Timely Filing",
                "severity": "WARNING",
                "code": "TIMELY_FILING_WARNING",
                "message": f"Claim DOS ({dos_str}) has only {days_remaining} days remaining before payer timely filing deadline ({deadline_days} days)."
            })
            penalty += 0.05

        assessment = {
            "date_of_service": dos_str,
            "days_elapsed": max(0, days_elapsed),
            "payer_deadline_days": deadline_days,
            "days_remaining": days_remaining,
            "status": status
        }

        return assessment, issues, penalty

    def _validate_duplicate_claims(self, patient: Dict[str, Any], dos: str, billing: Dict[str, Any], provider: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0

        member_id = str(patient.get("member_id", "")).strip().upper()
        rendering_npi = str(provider.get("rendering_npi_type_1") or provider.get("npi") or "").strip()
        cpt_codes = sorted([
            (item.get("code") if isinstance(item, dict) else item.code)
            for item in billing.get("cpt_codes", [])
        ])
        cpts_str = ",".join(cpt_codes)

        raw_fingerprint = f"{member_id}|{dos}|{cpts_str}|{rendering_npi}"
        fingerprint_hash = hashlib.sha256(raw_fingerprint.encode()).hexdigest()

        is_dup = (fingerprint_hash in self.submitted_claims_fingerprints)
        if not is_dup and self.ledger and hasattr(self.ledger, "has_fingerprint"):
            try:
                is_dup = self.ledger.has_fingerprint(fingerprint_hash)
            except Exception:
                pass

        if is_dup:
            issues.append({
                "tier": "Tier 8: Duplicate Claim Guard",
                "severity": "CRITICAL",
                "code": "DUPLICATE_CLAIM_DETECTED",
                "message": f"Exact duplicate claim already registered in batch/session with fingerprint '{fingerprint_hash[:12]}...'. Immediate CARC 18 denial risk."
            })
            penalty += 0.50

        return fingerprint_hash, issues, penalty

    def _validate_em_documentation(self, em_code: str, note: Dict[str, Any], billing: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0

        chief_complaint = note.get("chief_complaint", "").strip()
        assessment_plan = note.get("assessment_and_plan", "").strip()
        time_spent_min = note.get("time_spent_minutes", 0)

        if not chief_complaint:
            issues.append({
                "tier": "Tier 1: Clinical E/M",
                "severity": "CRITICAL",
                "code": "MISSING_CHIEF_COMPLAINT",
                "message": "Encounter note lacks a distinct, documented Chief Complaint (CC)."
            })
            penalty += 0.25

        if not assessment_plan:
            issues.append({
                "tier": "Tier 1: Clinical E/M",
                "severity": "CRITICAL",
                "code": "MISSING_ASSESSMENT_PLAN",
                "message": "Encounter note lacks an Assessment & Plan detailing clinical rationale."
            })
            penalty += 0.30

        if em_code == "99214":
            rx_terms = ["prescription", "prescribed", "refilled", "medication", "ibuprofen", "tid", "bid", "qd", "prn", "mg", "tablets", "capsules"]
            has_rx_mgmt = any(w in assessment_plan.lower() for w in rx_terms)
            has_sufficient_time = time_spent_min >= 30
            diagnoses_count = len(billing.get("diagnosis_codes", [])) if billing else len(note.get("diagnoses", []))

            if not (has_rx_mgmt or diagnoses_count >= 2 or has_sufficient_time):
                issues.append({
                    "tier": "Tier 1: Clinical E/M",
                    "severity": "WARNING",
                    "code": "DOWNCODE_RISK_99214",
                    "message": "Clinical documentation may not support 99214 (Moderate MDM). High audit downcode risk to 99213."
                })
                penalty += 0.15

        return issues, penalty

    def _validate_florida_scope_of_practice(
        self,
        provider: Dict[str, Any],
        billing: Dict[str, Any],
        note: Dict[str, Any],
        payer: Dict[str, Any],
        patient: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0

        prov_type = str(provider.get("type", "")).upper()
        state = str(provider.get("state", "")).upper()
        specialty = str(provider.get("specialty", "")).lower()
        is_autonomous = provider.get("fl_autonomous_registered", False)
        has_protocol = provider.get("has_collaborative_protocol", False)
        pos = str(billing.get("place_of_service", "11")).strip()

        if state == "FL" and "APRN" in prov_type:
            primary_care_specialties = ["family medicine", "general pediatrics", "general internal medicine", "primary care"]
            is_primary_care = any(pc in specialty for pc in primary_care_specialties)

            if is_autonomous and not is_primary_care and not has_protocol:
                issues.append({
                    "tier": "Tier 2: Florida Scope of Practice",
                    "severity": "CRITICAL",
                    "code": "FL_STATUTE_464_0123_VIOLATION",
                    "message": f"Autonomous APRN in FL billing specialty '{specialty}'. Under Florida Statute § 464.0123, autonomous practice is restricted to Primary Care."
                })
                penalty += 0.40

            if not is_autonomous and not has_protocol:
                issues.append({
                    "tier": "Tier 2: Florida Scope of Practice",
                    "severity": "CRITICAL",
                    "code": "MISSING_SUPERVISORY_PROTOCOL",
                    "message": "Non-autonomous APRN in Florida must have an active collaborative protocol on file."
                })
                penalty += 0.40

        # CMS 42 CFR § 410.26: Incident-To (100% physician fee schedule) is strictly prohibited in institutional settings (POS 21, 31, 32)
        if billing.get("incident_to") and pos in ("21", "22", "23", "31", "32"):
            issues.append({
                "tier": "Tier 2: Medicare Incident-To Compliance",
                "severity": "CRITICAL",
                "code": "INCIDENT_TO_FACILITY_PROHIBITED",
                "message": f"Incident-To billing (42 CFR § 410.26) is strictly prohibited in facility POS '{pos}'. NPP services in SNF/hospital must be billed directly under the NPP's NPI (85% MPFS)."
            })
            penalty += 0.40

        # Federal QMB Balance Billing Prohibition: Social Security Act § 1902(n)(3)(B)
        pat = patient or {}
        sec = pat.get("secondary_payer")
        sec_str = str(sec.get("name", "") if isinstance(sec, dict) else (sec or "")).lower()
        is_qmb = bool(pat.get("qmb_eligible")) or "qmb" in sec_str or "qmb" in str(payer.get("name", "")).lower()
        copay_collected = float(billing.get("patient_copay_collected", 0.0) or 0.0)
        if is_qmb and copay_collected > 0:
            issues.append({
                "tier": "Tier 2: Federal QMB Protection",
                "severity": "CRITICAL",
                "code": "QMB_BALANCE_BILLING_PROHIBITED",
                "message": f"Patient is QMB-protected; collecting ${copay_collected:.2f} violates Social Security Act § 1902(n)(3)(B) (42 U.S.C. § 1396a(n)(3)(B)). Refund immediately."
            })
            penalty += 0.50

        return issues, penalty

    def _validate_fda_ndc_compliance(self, billing: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
        """Enforces FDA/CMS/Medicaid Loop 2410 (LIN*N4 + CTP) 11-digit 5-4-2 NDC and unit rules
        for HCPCS drug/biologic codes (J0000-J9999, Q drug/biosimilar codes, S0010-S0199)."""
        issues: List[Dict[str, Any]] = []
        penalty = 0.0

        for item in billing.get("cpt_codes", []):
            is_dict = isinstance(item, dict)
            code = str(item.get("code") if is_dict else getattr(item, "code", "")).strip().upper()
            raw_ndc = (
                (item.get("ndc") or item.get("ndc_code"))
                if is_dict
                else (getattr(item, "ndc", None) or getattr(item, "ndc_code", None))
            )
            raw_qty = (
                (item.get("ndc_quantity") if item.get("ndc_quantity") is not None else item.get("ndc_units"))
                if is_dict
                else (getattr(item, "ndc_quantity", None) if getattr(item, "ndc_quantity", None) is not None else getattr(item, "ndc_units", None))
            )
            raw_unit = (
                (item.get("ndc_unit") or item.get("ndc_uom"))
                if is_dict
                else (getattr(item, "ndc_unit", None) or getattr(item, "ndc_uom", None))
            )

            is_drug_code = bool(HCPCS_DRUG_CODE_RE.match(code))
            if not is_drug_code and raw_ndc is None:
                continue

            norm_ndc = normalize_fda_ndc_11(raw_ndc)
            unit_clean = str(raw_unit or "").strip().upper()
            qty_ok = False
            if raw_qty is not None:
                try:
                    qty_ok = float(raw_qty) > 0
                except (TypeError, ValueError):
                    qty_ok = False
            unit_ok = unit_clean in VALID_NDC_UNITS

            if not norm_ndc or (is_drug_code and (not qty_ok or not unit_ok)):
                issues.append({
                    "tier": "Tier 3: FDA NDC & Drug/Biologic Compliance",
                    "severity": "CRITICAL",
                    "code": "MISSING_OR_INVALID_FDA_NDC",
                    "message": (
                        f"Service line '{code}' requires a valid 11-digit 5-4-2 FDA NDC (LIN*N4), "
                        f"positive NDC quantity, and valid X12 unit qualifier ({', '.join(sorted(VALID_NDC_UNITS))}) "
                        f"for Loop 2410 (CTP). Received ndc='{raw_ndc}', quantity='{raw_qty}', unit='{raw_unit}'."
                    ),
                })
                penalty += 0.40
            elif is_dict:
                item["ndc"] = norm_ndc
                if unit_ok:
                    item["ndc_unit"] = unit_clean

        return issues, penalty

    def _validate_medical_necessity(self, billing: Dict[str, Any], payer: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0

        cpt_codes = billing.get("cpt_codes", [])
        dx_codes = [dx.get("code") if isinstance(dx, dict) else dx.code for dx in billing.get("diagnosis_codes", [])]
        is_medicare = "medicare" in str(payer.get("name", "")).lower()

        for item in cpt_codes:
            cpt = item.get("code") if isinstance(item, dict) else item.code
            if cpt in self.FCSO_LCD_RULES and is_medicare:
                rule = self.FCSO_LCD_RULES[cpt]
                allowed_dx = rule["allowed_dx"]
                has_qualifying_dx = any(dx in allowed_dx for dx in dx_codes)

                if not has_qualifying_dx:
                    issues.append({
                        "tier": "Tier 3: FCSO Medical Necessity",
                        "severity": "CRITICAL",
                        "code": f"FCSO_LCD_FAIL_{rule['lcd_id']}",
                        "message": f"CPT {cpt} fails Florida MAC FCSO LCD {rule['lcd_id']}. Diagnosis {dx_codes} does not meet medical necessity. Expected: {allowed_dx}."
                    })
                    penalty += 0.30

        return issues, penalty

    def _validate_ncci_edits(self, billing: Dict[str, Any], note: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0

        cpt_items = billing.get("cpt_codes", [])
        billed_cpts = [str(item.get("code") if isinstance(item, dict) else item.code).strip().upper() for item in cpt_items]
        modifiers_by_cpt = {
            str(item.get("code") if isinstance(item, dict) else item.code).strip().upper(): (
                item.get("modifiers", []) if isinstance(item, dict) else item.modifiers
            )
            for item in cpt_items
        }

        # CMS G2211 Complexity Add-On Validation (2025/2026 MPFS):
        # Eligible ONLY with Office/Outpatient (99202-99215) or Home/Residence (99341-99350);
        # NEVER valid on SNF/NF (99304-99310), Inpatient, or ED claims.
        if "G2211" in billed_cpts:
            eligible_bases = [
                c for c in billed_cpts
                if re.match(r"^(?:992[01][2-5]|9934[1245]|9934[7-9]|99350)$", c)
            ]
            if not eligible_bases:
                issues.append({
                    "tier": "Tier 4: CMS G2211 Add-On Edits",
                    "severity": "CRITICAL",
                    "code": "G2211_INELIGIBLE_BASE_OR_SETTING",
                    "message": "HCPCS G2211 is restricted to Office/Outpatient (99202-99215) or Home/Residence (99341-99350) longitudinal care and is NEVER payable with SNF/NF codes (99304-99310) or standalone."
                })
                penalty += 0.40
            else:
                exempt_prev = {"G0438", "G0439", "G0101", "G0008", "G0009", "G0010"}
                has_mod25_on_base = any("25" in (modifiers_by_cpt.get(b) or []) for b in eligible_bases)
                if has_mod25_on_base and not (set(billed_cpts) & exempt_prev):
                    issues.append({
                        "tier": "Tier 4: CMS G2211 Add-On Edits",
                        "severity": "CRITICAL",
                        "code": "G2211_MODIFIER_25_CONFLICT",
                        "message": "HCPCS G2211 is not payable when the base E/M code carries Modifier -25 unless paired with an exempt Medicare Part B preventive service (e.g., AWV G0438/G0439, G0101, or Part B vaccine administration)."
                    })
                    penalty += 0.35

        for (col1, col2), edit in self.NCCI_PTP_EDITS.items():
            if col1 in billed_cpts and col2 in billed_cpts:
                req_mod = edit["required_modifier"]
                can_override = edit["allowed_with_modifier"]

                if not can_override:
                    issues.append({
                        "tier": "Tier 4: NCCI PTP Edits",
                        "severity": "CRITICAL",
                        "code": "NCCI_MUTUALLY_EXCLUSIVE",
                        "message": f"CPT {col1} and CPT {col2} are mutually exclusive under NCCI PTP edits. {edit['reason']}."
                    })
                    penalty += 0.40
                elif req_mod:
                    has_mod = req_mod in modifiers_by_cpt.get(col1, []) or req_mod in modifiers_by_cpt.get(col2, [])
                    if not has_mod:
                        issues.append({
                            "tier": "Tier 4: NCCI PTP Edits",
                            "severity": "CRITICAL",
                            "code": f"MISSING_MODIFIER_{req_mod}",
                            "message": f"CPT {col1} billed alongside {col2} requires Modifier -{req_mod}. {edit['reason']}."
                        })
                        penalty += 0.30

        # MUE (Medically Unlikely Edits) Unit Limits
        for item in cpt_items:
            code = str(item.get("code") if isinstance(item, dict) else item.code).strip().upper()
            units = item.get("units", 1) if isinstance(item, dict) else getattr(item, "units", 1)
            if code in self.MUE_LIMITS:
                max_units = self.MUE_LIMITS[code]
                if units > max_units:
                    issues.append({
                        "tier": "Tier 4: NCCI MUE Edits",
                        "severity": "CRITICAL",
                        "code": "MUE_UNITS_EXCEEDED",
                        "message": f"CPT {code} billed for {units} unit(s), exceeding CMS MUE limit of {max_units} unit(s) per date of service."
                    })
                    penalty += 0.35

        return issues, penalty

    def _validate_wedi_snip(self, patient: Dict[str, Any], provider: Dict[str, Any], billing: Dict[str, Any], payer: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0

        billing_npi = str(provider.get("billing_npi_type_2") or provider.get("npi") or "")
        rendering_npi = str(provider.get("rendering_npi_type_1") or provider.get("npi") or "")

        if not billing_npi or not re.match(r"^\d{10}$", billing_npi):
            issues.append({
                "tier": "Tier 5: WEDI SNIP 1-2 (Dual NPI)",
                "severity": "CRITICAL",
                "code": "INVALID_BILLING_NPI_TYPE_2",
                "message": f"Billing Provider NPI (Box 33a) is invalid ('{billing_npi}'). Expected valid 10-digit Type 2 Organization NPI."
            })
            penalty += 0.25

        if not rendering_npi or not re.match(r"^\d{10}$", rendering_npi):
            issues.append({
                "tier": "Tier 5: WEDI SNIP 1-2 (Dual NPI)",
                "severity": "CRITICAL",
                "code": "INVALID_RENDERING_NPI_TYPE_1",
                "message": f"Rendering Provider NPI (Box 24J) is invalid ('{rendering_npi}'). Expected valid 10-digit Type 1 Clinician NPI."
            })
            penalty += 0.25

        if not patient.get("dob") or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(patient.get("dob"))):
            issues.append({
                "tier": "Tier 5: WEDI SNIP 1-2",
                "severity": "CRITICAL",
                "code": "INVALID_PATIENT_DOB",
                "message": "Patient Date of Birth missing or invalid format (Expected YYYY-MM-DD)."
            })
            penalty += 0.20

        if not payer.get("payer_id"):
            issues.append({
                "tier": "Tier 5: WEDI SNIP 1-2",
                "severity": "CRITICAL",
                "code": "MISSING_PAYER_ID",
                "message": "Clearinghouse Electronic Payer ID is required."
            })
            penalty += 0.25

        return issues, penalty
