# ============================================
# FILE: medical_billing_ai/scrutinizer.py
# PURPOSE: 5-Tier Scrutiny Engine for Clinical Encounter & Medical Billing Auditing
# RULES: Florida Scope of Practice, FCSO Medicare MAC, NCCI Edits, WEDI SNIP 1-5
# ============================================

from typing import Dict, List, Any, Tuple
import re

class ScrutinyResult:
    def __init__(self, passed: bool, confidence_score: float, issues: List[Dict[str, Any]], clean_claim_data: Dict[str, Any] = None):
        self.passed = passed
        self.confidence_score = confidence_score
        self.issues = issues
        self.clean_claim_data = clean_claim_data or {}

    def to_dict(self):
        return {
            "passed": self.passed,
            "confidence_score": self.confidence_score,
            "issue_count": len(self.issues),
            "issues": self.issues,
            "clean_claim_data": self.clean_claim_data
        }


class MedicalBillingScrutinizer:
    """
    Autonomous Medical Billing & Clinical Note Scrutinizer.
    Enforces Florida Statutes, FCSO Medicare LCD/NCD, NCCI PTP/MUE, and AMA MDM Guidelines.
    """

    # FCSO (Jurisdiction N - Florida) LCD Medical Necessity Mapping Examples
    FCSO_LCD_RULES = {
        # CPT 82306 (Vitamin D test) - Requires specific metabolic/renal/bone diagnosis codes
        "82306": {
            "lcd_id": "L36063",
            "allowed_dx": ["E55.9", "E55.0", "M81.0", "N18.3", "N18.4", "N18.5", "E21.0"],
            "description": "Vitamin D; 25 hydroxy, includes fraction(s), if performed"
        },
        # CPT 20610 (Arthrocentesis, major joint) - Requires arthritis/joint disorder
        "20610": {
            "lcd_id": "L36805",
            "allowed_dx": ["M17.0", "M17.11", "M17.12", "M16.11", "M16.12", "M25.561", "M25.562"],
            "description": "Aspiration/injection intermediate or major joint"
        },
        # CPT 17000 (Destruction of premalignant lesions e.g. Actinic Keratosis)
        "17000": {
            "lcd_id": "L33947",
            "allowed_dx": ["L57.0"], # Actinic Keratosis
            "description": "Destruction premalignant lesions; first lesion"
        }
    }

    # NCCI Procedure-to-Procedure (PTP) Bundling Pairs with Modifier Indicators
    # Indicator 1 = Allowed with appropriate modifier (e.g., -25, -59)
    # Indicator 0 = Never allowed / unbundling prohibited
    NCCI_PTP_EDITS = {
        ("99214", "17000"): {"allowed_with_modifier": True, "required_modifier": "25", "reason": "E/M with minor surgical procedure requires significant separate identifiable service"},
        ("99213", "17000"): {"allowed_with_modifier": True, "required_modifier": "25", "reason": "E/M with minor surgical procedure requires significant separate identifiable service"},
        ("99214", "20610"): {"allowed_with_modifier": True, "required_modifier": "25", "reason": "E/M with major joint injection requires separate identifiable E/M service"},
        ("11042", "97597"): {"allowed_with_modifier": False, "required_modifier": None, "reason": "Mutually exclusive wound debridement services"},
    }

    # Medically Unlikely Edits (MUE) per day limits
    MUE_LIMITS = {
        "99213": 1,
        "99214": 1,
        "99215": 1,
        "17000": 1, # First lesion is 1 unit; additional use 17003
        "20610": 2, # Bilateral max 2 units
        "82306": 1
    }

    def __init__(self):
        pass

    def scrutinize_encounter(self, encounter: Dict[str, Any]) -> ScrutinyResult:
        issues = []
        confidence = 1.0

        # Extract encounter sections
        provider = encounter.get("provider", {})
        patient = encounter.get("patient", {})
        clinical_note = encounter.get("clinical_note", {})
        billing = encounter.get("billing", {})
        payer = encounter.get("payer", {})

        # ----------------------------------------------------
        # TIER 1: Clinical Note Completeness & E/M MDM Scrutiny
        # ----------------------------------------------------
        em_code = billing.get("cpt_codes", [{}])[0].get("code", "") if billing.get("cpt_codes") else ""
        if em_code.startswith("992"):
            mdm_issues, mdm_confidence_penalty = self._validate_em_documentation(em_code, clinical_note)
            issues.extend(mdm_issues)
            confidence -= mdm_confidence_penalty

        # ----------------------------------------------------
        # TIER 2: Florida Scope of Practice & Credentialing
        # ----------------------------------------------------
        fl_issues, fl_penalty = self._validate_florida_scope_of_practice(provider, billing, clinical_note, payer)
        issues.extend(fl_issues)
        confidence -= fl_penalty

        # ----------------------------------------------------
        # TIER 3: Medical Necessity & FCSO Florida LCD/NCD
        # ----------------------------------------------------
        med_issues, med_penalty = self._validate_medical_necessity(billing, payer)
        issues.extend(med_issues)
        confidence -= med_penalty

        # ----------------------------------------------------
        # TIER 4: NCCI Edits (PTP Bundling & MUE limits)
        # ----------------------------------------------------
        ncci_issues, ncci_penalty = self._validate_ncci_edits(billing, clinical_note)
        issues.extend(ncci_issues)
        confidence -= ncci_penalty

        # ----------------------------------------------------
        # TIER 5: WEDI SNIP & Demographic Completeness
        # ----------------------------------------------------
        snip_issues, snip_penalty = self._validate_wedi_snip(patient, provider, billing, payer)
        issues.extend(snip_issues)
        confidence -= snip_penalty

        confidence = max(0.0, min(1.0, round(confidence, 2)))
        passed = (len([i for i in issues if i["severity"] == "CRITICAL"]) == 0) and (confidence >= 0.90)

        # Prepare normalized clean claim object if passing
        clean_claim = encounter if passed else {}

        return ScrutinyResult(passed=passed, confidence_score=confidence, issues=issues, clean_claim_data=clean_claim)

    def _validate_em_documentation(self, em_code: str, note: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0

        chief_complaint = note.get("chief_complaint", "").strip()
        hpi = note.get("history_of_present_illness", "").strip()
        assessment_plan = note.get("assessment_and_plan", "").strip()
        time_spent_min = note.get("time_spent_minutes", 0)

        if not chief_complaint:
            issues.append({
                "tier": "Tier 1: Clinical E/M",
                "severity": "CRITICAL",
                "code": "MISSING_CHIEF_COMPLAINT",
                "message": "Encounter note lacks a distinct, documented Chief Complaint (CC). Claim will be denied upon payer medical audit."
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

        # E/M Level 4 (99214) Moderate Complexity MDM requirement check:
        # Requires either: Moderate MDM (prescription drug management, 2+ stable chronic conditions or 1 exacerbation)
        # OR >= 30-39 minutes of documented physician time on encounter date.
        if em_code == "99214":
            has_rx_mgmt = "prescription" in assessment_plan.lower() or "prescribed" in assessment_plan.lower() or "refilled" in assessment_plan.lower()
            has_sufficient_time = time_spent_min >= 30
            diagnoses_count = len(note.get("diagnoses", []))

            if not (has_rx_mgmt or diagnoses_count >= 2 or has_sufficient_time):
                issues.append({
                    "tier": "Tier 1: Clinical E/M",
                    "severity": "WARNING",
                    "code": "DOWNCODE_RISK_99214",
                    "message": "Clinical documentation may not support 99214 (Moderate MDM). Missing prescription drug management or documented time >=30 mins. High audit downcode risk to 99213."
                })
                penalty += 0.15

        return issues, penalty

    def _validate_florida_scope_of_practice(self, provider: Dict[str, Any], billing: Dict[str, Any], note: Dict[str, Any], payer: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0

        prov_type = provider.get("type", "").upper() # "MD", "DO", "APRN", "PA"
        state = provider.get("state", "").upper()
        specialty = provider.get("specialty", "").lower()
        is_autonomous = provider.get("fl_autonomous_registered", False)
        has_protocol = provider.get("has_collaborative_protocol", False)

        if state == "FL" and prov_type == "APRN":
            # Check Florida Statute § 464.0123 (Autonomous APRN)
            primary_care_specialties = ["family medicine", "general pediatrics", "general internal medicine", "primary care"]
            is_primary_care = any(pc in specialty for pc in primary_care_specialties)

            if is_autonomous and not is_primary_care:
                # Florida Statute 464.0123 limits autonomous practice specifically to primary care
                if not has_protocol:
                    issues.append({
                        "tier": "Tier 2: Florida Scope of Practice",
                        "severity": "CRITICAL",
                        "code": "FL_STATUTE_464_0123_VIOLATION",
                        "message": f"Provider is registered as an Autonomous APRN in FL but billing specialty '{specialty}'. Under Florida Statute § 464.0123, autonomous practice is restricted to Primary Care. Specialty practice requires an active collaborative protocol with an MD/DO."
                    })
                    penalty += 0.40

            if not is_autonomous and not has_protocol:
                issues.append({
                    "tier": "Tier 2: Florida Scope of Practice",
                    "severity": "CRITICAL",
                    "code": "MISSING_SUPERVISORY_PROTOCOL",
                    "message": "Non-autonomous APRN in Florida must have an active collaborative protocol on file with the Florida Board of Nursing."
                })
                penalty += 0.40

            # Medicare Incident-to check (FCSO Jurisdiction N)
            is_medicare = "medicare" in payer.get("name", "").lower()
            billing_as_incident_to = billing.get("incident_to", False)
            if is_medicare and billing_as_incident_to:
                direct_supervision_documented = note.get("direct_physician_supervision_present", False)
                established_patient = note.get("patient_status", "").lower() == "established"
                new_problem = note.get("has_new_medical_problem", False)

                if not direct_supervision_documented:
                    issues.append({
                        "tier": "Tier 2: Florida Scope of Practice / Medicare",
                        "severity": "CRITICAL",
                        "code": "MEDICARE_INCIDENT_TO_NO_SUPERVISION",
                        "message": "Billing Medicare as 'Incident-To' under Physician NPI requires direct physician physical presence in the office suite. Direct supervision not documented in encounter note."
                    })
                    penalty += 0.35

                if new_problem or not established_patient:
                    issues.append({
                        "tier": "Tier 2: Florida Scope of Practice / Medicare",
                        "severity": "CRITICAL",
                        "code": "MEDICARE_INCIDENT_TO_NEW_PROBLEM",
                        "message": "Medicare incident-to rules strictly prohibit billing new patients or new medical problems without the physician personally establishing the treatment plan."
                    })
                    penalty += 0.35

        return issues, penalty

    def _validate_medical_necessity(self, billing: Dict[str, Any], payer: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0

        cpt_codes = billing.get("cpt_codes", [])
        dx_codes = [dx.get("code") for dx in billing.get("diagnosis_codes", [])]
        is_medicare = "medicare" in payer.get("name", "").lower()

        for item in cpt_codes:
            cpt = item.get("code")
            if cpt in self.FCSO_LCD_RULES and is_medicare:
                rule = self.FCSO_LCD_RULES[cpt]
                allowed_dx = rule["allowed_dx"]
                has_qualifying_dx = any(dx in allowed_dx for dx in dx_codes)

                if not has_qualifying_dx:
                    issues.append({
                        "tier": "Tier 3: FCSO Medical Necessity",
                        "severity": "CRITICAL",
                        "code": f"FCSO_LCD_FAIL_{rule['lcd_id']}",
                        "message": f"CPT {cpt} ({rule['description']}) fails Florida MAC FCSO LCD {rule['lcd_id']}. Billed diagnosis codes {dx_codes} do not meet local coverage determination medical necessity. Expected one of: {allowed_dx}."
                    })
                    penalty += 0.30

        return issues, penalty

    def _validate_ncci_edits(self, billing: Dict[str, Any], note: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0

        cpt_items = billing.get("cpt_codes", [])
        billed_cpts = [item.get("code") for item in cpt_items]
        modifiers_by_cpt = {item.get("code"): item.get("modifiers", []) for item in cpt_items}

        # Check PTP Bundling Pairs
        for (col1, col2), edit in self.NCCI_PTP_EDITS.items():
            if col1 in billed_cpts and col2 in billed_cpts:
                req_mod = edit["required_modifier"]
                can_override = edit["allowed_with_modifier"]

                if not can_override:
                    issues.append({
                        "tier": "Tier 4: NCCI PTP Edits",
                        "severity": "CRITICAL",
                        "code": "NCCI_MUTUALLY_EXCLUSIVE",
                        "message": f"CPT {col1} and CPT {col2} are mutually exclusive under NCCI PTP edits (Indicator 0). Unbundling is prohibited."
                    })
                    penalty += 0.40
                elif req_mod:
                    has_mod = req_mod in modifiers_by_cpt.get(col1, []) or req_mod in modifiers_by_cpt.get(col2, [])
                    if not has_mod:
                        issues.append({
                            "tier": "Tier 4: NCCI PTP Edits",
                            "severity": "CRITICAL",
                            "code": f"MISSING_MODIFIER_{req_mod}",
                            "message": f"CPT {col1} billed alongside {col2} requires Modifier -{req_mod}. {edit['reason']}. Claim will be bundled/denied without this modifier."
                        })
                        penalty += 0.30

        # Check MUE Limits
        cpt_unit_counts = {}
        for item in cpt_items:
            code = item.get("code")
            units = item.get("units", 1)
            cpt_unit_counts[code] = cpt_unit_counts.get(code, 0) + units

        for code, units in cpt_unit_counts.items():
            if code in self.MUE_LIMITS and units > self.MUE_LIMITS[code]:
                issues.append({
                    "tier": "Tier 4: NCCI MUE Limits",
                    "severity": "CRITICAL",
                    "code": "MUE_LIMIT_EXCEEDED",
                    "message": f"CPT {code} billed for {units} units exceeds Medically Unlikely Edit (MUE) daily threshold of {self.MUE_LIMITS[code]} unit(s)."
                })
                penalty += 0.30

        return issues, penalty

    def _validate_wedi_snip(self, patient: Dict[str, Any], provider: Dict[str, Any], billing: Dict[str, Any], payer: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
        issues = []
        penalty = 0.0

        # SNIP 1 & 2: Mandatory demographic/identifiers
        if not provider.get("npi") or not re.match(r"^\d{10}$", str(provider.get("npi"))):
            issues.append({
                "tier": "Tier 5: WEDI SNIP 1-2",
                "severity": "CRITICAL",
                "code": "INVALID_BILLING_NPI",
                "message": "Billing Provider NPI is missing or not a 10-digit number."
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

        # SNIP 3: Balancing check (Line item charges == total claim charge)
        line_items = billing.get("cpt_codes", [])
        calculated_total = sum(item.get("charge", 0.0) * item.get("units", 1) for item in line_items)
        declared_total = billing.get("total_charge", 0.0)
        if abs(calculated_total - declared_total) > 0.01:
            issues.append({
                "tier": "Tier 5: WEDI SNIP 3 Balancing",
                "severity": "CRITICAL",
                "code": "SNIP_3_CLAIM_UNBALANCED",
                "message": f"Claim balancing error: Total charge (${declared_total}) does not equal sum of line items (${calculated_total})."
            })
            penalty += 0.25

        return issues, penalty
