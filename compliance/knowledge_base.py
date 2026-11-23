# ============================================
# FILE: compliance/knowledge_base.py
# PURPOSE: Deterministic, Citation-Grounded Medical Biller & Coder Knowledge Base
#          and Code-Check Engine (Zero-Hallucination Architecture).
# STANDARDS:
#   - AMA CPT 2026 (Office, Initial/Subsequent SNF, Home/ALF, Telehealth, Modifiers)
#   - CMS / Medicare CY2026 & FCSO FL Jurisdiction N (09102), MBI, NCCI PTP/MUE, G2211
#   - FY2026 CMS/CDC NCHS ICD-10-CM Official Guidelines (live simple_icd_10_cm tree)
#   - FDA National Drug Code (21 CFR Part 207) 11-digit 5-4-2 & Loop 2410 rules
#   - Florida Medicaid (AHCA 77027, SMMC MMA/LTC, QMB SSA § 1902(n)(3)(B))
#   - Office Ally Clearinghouse (Receiver 330897513, SFTP, Portal, 270/271, 999/277CA/835)
# ============================================

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple, Union

try:
    import simple_icd_10_cm as icd
except ImportError:  # pragma: no cover
    icd = None

try:
    from compliance.claim_gate import valid_mbi, valid_npi
    from compliance.reference_data import ReferenceDataLibrary
    from scrutiny.scrutinizer import HCPCS_DRUG_CODE_RE, VALID_NDC_UNITS, normalize_fda_ndc_11
except ImportError:  # pragma: no cover
    from .claim_gate import valid_mbi, valid_npi
    from .reference_data import ReferenceDataLibrary
    from ..scrutiny.scrutinizer import HCPCS_DRUG_CODE_RE, VALID_NDC_UNITS, normalize_fda_ndc_11


STANDARDS_JSON_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "reference_data",
    "standards_2026.json",
)


def valid_fl_medicaid_id(medicaid_id: Optional[str]) -> bool:
    """Validates standard 10-digit numeric Florida Medicaid (AHCA/FMMIS) Recipient ID."""
    if not medicaid_id or not isinstance(medicaid_id, (str, int)):
        return False
    s = str(medicaid_id).strip()
    return bool(re.fullmatch(r"\d{10}", s)) and s != "0000000000"


class MedicalCodingAssistant:
    """
    Deterministic, Citation-Grounded Medical Biller & Coder Assistant + Code-Check Engine.
    Strictly answers and validates from verified code tables (`simple_icd_10_cm`,
    `standards_2026.json`, and `ReferenceDataLibrary` NCCI/LCD CSVs).
    Never guesses or fabricates codes, fee schedules, RVUs, or payer policies.
    """

    SYMPTOM_SUPPRESSION_RULES: Dict[str, Tuple[List[str], str]] = {
        "M25.561": (["M17.", "M23."], "Right knee pain (M25.561) is integral to confirmed knee osteoarthritis (M17.x) or internal derangement (M23.x)."),
        "M25.562": (["M17.", "M23."], "Left knee pain (M25.562) is integral to confirmed knee osteoarthritis (M17.x) or internal derangement (M23.x)."),
        "M25.569": (["M17.", "M23."], "Unspecified knee pain (M25.569) is integral to confirmed knee osteoarthritis (M17.x) or internal derangement (M23.x)."),
        "M54.50": (["M54.16", "M51."], "Low back pain (M54.50) is integral to confirmed lumbar radiculopathy (M54.16) or intervertebral disc disorder (M51.x)."),
        "M54.59": (["M54.16", "M51."], "Other low back pain (M54.59) is integral to confirmed lumbar radiculopathy (M54.16) or disc disorder (M51.x)."),
        "R53.83": (["E55.", "E11.", "E03.", "D64."], "Other fatigue (R53.83) is routinely associated with the documented underlying metabolic/hematologic condition."),
    }

    CPT_LCD_MAP: Dict[str, str] = {
        "82306": "L36063",
        "20610": "L36805",
        "20605": "L36805",
        "17000": "L33947",
        "17003": "L33947",
        "20552": "L33622",
        "20553": "L33622",
    }

    KNOWN_NON_CPT_NUMBERS: Set[str] = {
        "09102", "77027", "00590", "60054", "87726", "61101", "62308", "51062",
        "68069", "14163", "00324", "59274", "65088", "A3565", "94265", "59065", "99727", "12807",
        "33025", "33027", "33127", "33128", "33131", "33144", "33145", "33185", "32202",
        "00501", "01234", "12345",
    }

    UNVERIFIED_PRICING_QUERY_RE = re.compile(
        r"\b(?:w-?rvu|rvu\s+value|rvus|conversion\s+factor|"
        r"exact\s+(?:dollar|fee|reimbursement|payment|allowable)|"
        r"how\s+much\s+(?:money|in\s+dollars)|"
        r"dollar\s+(?:amount|reimbursement|rate|allowable)|"
        r"commercial\s+fee\s+schedule)\b",
        re.IGNORECASE,
    )

    OUT_OF_SCOPE_CLINICAL_OR_LEGAL_RE = re.compile(
        r"\b(?:what\s+dosage\s+should\s+i\s+prescribe|which\s+antibiotic\s+should\s+i\s+treat|"
        r"malpractice\s+lawsuit\s+strategy|prescribe\s+for\s+my\s+patient)\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        standards_path: str = STANDARDS_JSON_PATH,
        ref_library: Optional[ReferenceDataLibrary] = None,
    ):
        with open(standards_path, "r", encoding="utf-8") as fh:
            self.standards: Dict[str, Any] = json.load(fh)
        self.ref_lib = ref_library or ReferenceDataLibrary()

    # ── ICD-10-CM Live Tree Helpers ───────────────────────────────────────────

    @staticmethod
    def _collect_leaf_descendants(code: str, max_items: int = 12) -> List[Dict[str, str]]:
        """Recursively collects billable leaf ICD-10-CM codes beneath a non-billable category/header."""
        if icd is None or not icd.is_valid_item(code):
            return []
        leaves: List[Dict[str, str]] = []
        queue = list(icd.get_children(code))
        while queue and len(leaves) < max_items:
            curr = queue.pop(0)
            if icd.is_leaf(curr):
                leaves.append({"code": curr, "description": icd.get_description(curr)})
            else:
                queue.extend(icd.get_children(curr))
        return leaves

    def lookup_icd10(self, raw_code: str) -> Dict[str, Any]:
        """Deterministically validates an ICD-10-CM code against FY2026 NCHS/CMS tables."""
        code = str(raw_code or "").strip().upper()
        # Normalize missing dot if user entered e.g. M5450 -> M54.50
        if len(code) > 3 and "." not in code and icd is not None:
            dotted = f"{code[:3]}.{code[3:]}"
            if icd.is_valid_item(dotted):
                code = dotted

        if icd is None:
            return {"code": code, "exists": False, "billable": False, "error": "simple_icd_10_cm not installed"}

        if not code or not icd.is_valid_item(code):
            return {
                "code": code,
                "exists": False,
                "billable": False,
                "description": None,
                "parent": None,
                "billable_suggestions": [],
                "citation": self.standards["icd10_cm_fy2026_guidelines"]["citation"],
            }

        is_billable = bool(icd.is_leaf(code))
        parent = icd.get_parent(code) or None
        suggestions = [] if is_billable else self._collect_leaf_descendants(code)
        return {
            "code": code,
            "exists": True,
            "billable": is_billable,
            "description": icd.get_description(code),
            "parent": parent,
            "parent_description": icd.get_description(parent) if parent and icd.is_valid_item(parent) else None,
            "billable_suggestions": suggestions,
            "citation": self.standards["icd10_cm_fy2026_guidelines"]["citation"],
        }

    # ── FDA NDC 11-Digit & Optional openFDA Lookup ────────────────────────────

    def lookup_ndc(
        self,
        raw_ndc: str,
        unit: Optional[str] = None,
        quantity: Optional[float] = None,
        allow_openfda_live: bool = False,
    ) -> Dict[str, Any]:
        """Validates and normalizes an FDA NDC to 11-digit 5-4-2 format, with optional
        openFDA live directory verification and strict offline fallback."""
        norm_11 = normalize_fda_ndc_11(raw_ndc)
        unit_clean = str(unit).strip().upper() if unit is not None else None
        unit_valid = (unit_clean in VALID_NDC_UNITS) if unit_clean is not None else True
        qty_valid = (float(quantity) > 0) if quantity is not None else True

        result: Dict[str, Any] = {
            "input_ndc": raw_ndc,
            "normalized_ndc_11": norm_11,
            "hyphenated_5_4_2": f"{norm_11[:5]}-{norm_11[5:9]}-{norm_11[9:]}" if norm_11 else None,
            "valid_format": bool(norm_11),
            "unit": unit_clean,
            "unit_valid": unit_valid,
            "quantity": quantity,
            "quantity_valid": qty_valid,
            "verification_source": "offline_deterministic_5_4_2_validator",
            "openfda_record": None,
            "citation": self.standards["fda_ndc_rules"]["citation"],
        }

        if norm_11 and allow_openfda_live:
            # Derive possible FDA 2-segment product_ndc forms from 11-digit 5-4-2
            candidates = [f"{norm_11[:5]}-{norm_11[5:9]}"]
            if norm_11.startswith("0"):
                candidates.append(f"{norm_11[1:5]}-{norm_11[5:9]}")
            if norm_11[5] == "0":
                candidates.append(f"{norm_11[:5]}-{norm_11[6:9]}")
            for prod_ndc in candidates:
                query_q = urllib.parse.quote(f'product_ndc:"{prod_ndc}"')
                url = f"https://api.fda.gov/drug/ndc.json?search={query_q}&limit=1"
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "MedicalBillingAI-StandardsKB/2026"})
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                    items = payload.get("results") or []
                    if items:
                        rec = items[0]
                        result["verification_source"] = "openfda_live_directory"
                        result["openfda_record"] = {
                            "product_ndc": rec.get("product_ndc"),
                            "generic_name": rec.get("generic_name"),
                            "brand_name": rec.get("brand_name"),
                            "labeler_name": rec.get("labeler_name"),
                            "dosage_form": rec.get("dosage_form"),
                            "route": rec.get("route"),
                        }
                        break
                except Exception:
                    # Graceful offline fallback — never fail deterministic pipeline when offline
                    continue

        return result

    # ── Method 1: Deterministic Multi-Pillar Claim Code Verification ──────────

    def check_claim_codes(
        self,
        cpt_codes: List[Union[str, Dict[str, Any]]],
        icd10_codes: List[str],
        pos: str,
        modifiers: Optional[Union[List[str], Dict[str, List[str]]]] = None,
        ndc: Optional[Union[str, Dict[str, Any]]] = None,
        payer_type: str = "MEDICARE",
        provider_type: str = "MD",
    ) -> Dict[str, Any]:
        """
        Deterministically audits CPT/HCPCS codes, ICD-10-CM specificity & suppression,
        POS/CPT setting compatibility, NCCI PTP/MUE edits, G2211 rules, FCSO FL LCDs,
        FDA 11-digit NDC requirements, and Medicare/Medicaid provider rules.
        """
        pos_str = str(pos or "").strip().zfill(2) if str(pos or "").strip().isdigit() else str(pos or "").strip()
        payer_upper = str(payer_type or "MEDICARE").strip().upper()
        prov_upper = str(provider_type or "MD").strip().upper()
        is_medicare = "MEDICARE" in payer_upper or "FCSO" in payer_upper or "09102" in payer_upper
        is_medicaid_or_qmb = any(k in payer_upper for k in ("MEDICAID", "AHCA", "77027", "QMB", "MMA"))

        issues: List[Dict[str, str]] = []
        citations: List[Dict[str, str]] = []

        def add_citation(source: str, section: str, summary: str) -> None:
            entry = {"source": source, "section": section, "summary": summary}
            if entry not in citations:
                citations.append(entry)

        # 1. Normalize CPT lines & modifiers
        parsed_lines: List[Dict[str, Any]] = []
        for idx, raw_c in enumerate(cpt_codes or []):
            if isinstance(raw_c, dict):
                c_code = str(raw_c.get("code", "")).strip().upper()
                line_mods = [str(m).strip().upper() for m in (raw_c.get("modifiers") or []) if str(m).strip()]
                units = int(raw_c.get("units", 1) or 1)
            else:
                parts = [p.strip().upper() for p in str(raw_c).split("-") if p.strip()]
                c_code = parts[0] if parts else ""
                line_mods = parts[1:] if len(parts) > 1 else []
                units = 1

            if isinstance(modifiers, dict) and c_code in modifiers:
                for m in modifiers[c_code]:
                    mu = str(m).strip().upper()
                    if mu and mu not in line_mods:
                        line_mods.append(mu)
            elif isinstance(modifiers, list) and idx == 0:
                for m in modifiers:
                    mu = str(m).strip().upper()
                    if mu and mu not in line_mods:
                        line_mods.append(mu)

            if c_code:
                parsed_lines.append({"code": c_code, "modifiers": line_mods, "units": units})

        if not parsed_lines:
            issues.append({
                "code": "MISSING_CPT_CODES",
                "severity": "CRITICAL",
                "message": "At least one CPT or HCPCS procedure code is required.",
            })

        # Validate modifiers if any were supplied
        known_mods = self.standards["modifiers"]
        for ln in parsed_lines:
            for m in ln["modifiers"]:
                if m in known_mods:
                    add_citation(known_mods[m]["citation"], f"Modifier -{m}", known_mods[m]["rule"])
                elif not re.fullmatch(r"[A-Z0-9]{2}", m):
                    issues.append({
                        "code": "INVALID_MODIFIER_FORMAT",
                        "severity": "CRITICAL",
                        "message": f"Modifier '{m}' on {ln['code']} is not a valid 2-character CPT/HCPCS modifier.",
                    })

        # 2. CPT Existence, Deleted/Consult Code Check & POS Compatibility
        pos_table = self.standards["pos_codes"]
        pos_issues: List[Dict[str, str]] = []
        pos_desc = pos_table.get(pos_str, {}).get("name") or MedicalCodingAssistant._fallback_pos_name(pos_str)
        if not pos_desc:
            msg = f"Place of Service '{pos_str}' is not a valid HIPAA Place of Service code."
            pos_issues.append({"code": "INVALID_POS_CODE", "severity": "CRITICAL", "message": msg})
            issues.append({"code": "INVALID_POS_CODE", "severity": "CRITICAL", "message": msg})
        elif pos_str in pos_table:
            add_citation(pos_table[pos_str]["citation"], f"POS {pos_str} ({pos_desc})", pos_table[pos_str]["rule"])

        cpt_table = self.standards["cpt_codes"]
        deleted_table = self.standards["deleted_or_nonpayable_cpt"]

        for ln in parsed_lines:
            c = ln["code"]
            mods = ln["modifiers"]

            # Deleted or Medicare-nonpayable consultation codes (99241-99255, 99201, 99343, 99324-99337)
            if c in deleted_table or re.match(r"^(?:9924[1-5]|9925[1-5]|9932[4-8]|9933[4-7])$", c):
                reason = deleted_table.get(c) or (
                    "CPT consultation codes (99241-99255) are not payable by Medicare Part B (MLN MM6740); "
                    "report Initial Nursing Facility Care (99304-99306) or Office E/M (99202-99215)."
                    if c.startswith("9924") or c.startswith("9925")
                    else "Domiciliary/ALF CPT codes (99324-99337) were deleted in CPT 2023; report Home/Residence E/M (99341-99350) with POS 13."
                )
                if is_medicare or c in ("99201", "99343") or re.match(r"^(?:9932[4-8]|9933[4-7])$", c):
                    issues.append({"code": "DELETED_OR_NONPAYABLE_CPT", "severity": "CRITICAL", "message": f"CPT {c}: {reason}"})
                    add_citation("CMS IOM Pub. 100-04, Ch. 12, § 30.6.13 / AMA CPT 2026", f"CPT {c}", reason)
                    continue

            is_drug = bool(HCPCS_DRUG_CODE_RE.match(c))
            if c not in cpt_table and not is_drug:
                issues.append({
                    "code": "UNVERIFIED_CPT_CODE",
                    "severity": "CRITICAL",
                    "message": f"CPT/HCPCS '{c}' is outside verified 2026 reference tables; abstaining from unverified pass.",
                })
                continue

            if c in cpt_table:
                meta = cpt_table[c]
                add_citation(meta["citation"], f"CPT/HCPCS {c}", meta["category"])
                allowed_pos = meta.get("allowed_pos")
                if allowed_pos and pos_desc and pos_str not in allowed_pos:
                    p_msg = (
                        f"CPT {c} ({meta['category']}) is incompatible with POS {pos_str} ({pos_desc}). "
                        f"Allowed POS: {', '.join(allowed_pos)}."
                    )
                    pos_issues.append({"code": "POS_CPT_INCOMPATIBLE", "severity": "CRITICAL", "message": p_msg})
                    issues.append({"code": "POS_CPT_INCOMPATIBLE", "severity": "CRITICAL", "message": p_msg})

                # Telehealth POS 02 / 10 requires telehealth modifier (95 or 93) on E/M
                if pos_str in ("02", "10") and c.startswith("99") and not ({"95", "93"} & set(mods)):
                    t_msg = f"Telehealth POS {pos_str} with E/M {c} requires synchronous telehealth Modifier -95 (audio-video) or -93 (audio-only)."
                    pos_issues.append({"code": "MISSING_TELEHEALTH_MODIFIER", "severity": "CRITICAL", "message": t_msg})
                    issues.append({"code": "MISSING_TELEHEALTH_MODIFIER", "severity": "CRITICAL", "message": t_msg})

                # Part A SNF Consolidated Billing check (POS 31)
                if is_medicare and pos_str == "31" and meta.get("snf_part_a_consolidated_bundled"):
                    cb_msg = (
                        f"SNF Consolidated Billing Alert: {c} ({meta['category']}) is bundled into the Skilled Nursing Facility's "
                        f"Medicare Part A per-diem during a Part A stay (POS 31) per Social Security Act § 1888(e) / 42 CFR § 411.15(p) "
                        f"and cannot be billed separately to Part B MAC 09102."
                    )
                    pos_issues.append({"code": "SNF_CONSOLIDATED_BILLING_BUNDLED", "severity": "CRITICAL", "message": cb_msg})
                    issues.append({"code": "SNF_CONSOLIDATED_BILLING_BUNDLED", "severity": "CRITICAL", "message": cb_msg})
                    add_citation("Social Security Act § 1888(e); 42 CFR § 411.15(p)", "SNF Consolidated Billing", cb_msg)

            # Modifier -AI misuse check on subsequent SNF codes
            if "AI" in mods and not re.match(r"^9930[4-6]$", c):
                ai_msg = f"Modifier -AI (Principal Physician of Record) is invalid on {c}; it applies strictly to Initial Facility Care (e.g., 99304-99306) by the attending physician."
                issues.append({"code": "INVALID_MODIFIER_AI", "severity": "CRITICAL", "message": ai_msg})

        # 3. ICD-10-CM Specificity, Leaf Check & Symptom Suppression (FY2026)
        valid_billable_icd10: List[Dict[str, str]] = []
        invalid_icd10: List[str] = []
        non_billable_suggestions: Dict[str, List[Dict[str, str]]] = {}
        symptom_suppression_alerts: List[Dict[str, Any]] = []
        normalized_dx_codes: List[str] = []

        if not icd10_codes:
            issues.append({
                "code": "MISSING_ICD10_CODES",
                "severity": "CRITICAL",
                "message": "At least one valid, billable ICD-10-CM diagnosis code is required.",
            })

        for raw_dx in icd10_codes or []:
            info = self.lookup_icd10(raw_dx)
            dx_code = info["code"]
            add_citation(
                self.standards["icd10_cm_fy2026_guidelines"]["citation"],
                "Section I.B.2 / I.B.18 (Specificity)",
                self.standards["icd10_cm_fy2026_guidelines"]["specificity_rule"],
            )
            if not info["exists"]:
                invalid_icd10.append(dx_code)
                issues.append({
                    "code": "ICD10_INVALID",
                    "severity": "CRITICAL",
                    "message": f"'{dx_code}' is not a valid FY2026 ICD-10-CM diagnosis code.",
                })
            elif not info["billable"]:
                non_billable_suggestions[dx_code] = info["billable_suggestions"]
                sug_preview = ", ".join(s["code"] for s in info["billable_suggestions"][:5])
                issues.append({
                    "code": "ICD10_NON_BILLABLE_HEADER",
                    "severity": "CRITICAL",
                    "message": (
                        f"'{dx_code}' ({info['description']}) is a non-billable category/header. "
                        f"Select a specific leaf code: {sug_preview or 'see subcategories'}."
                    ),
                })
            else:
                valid_billable_icd10.append({"code": dx_code, "description": info["description"]})
                normalized_dx_codes.append(dx_code)

        # Symptom suppression check (ICD-10-CM Section I.B.5)
        for dx_code in normalized_dx_codes:
            if dx_code in self.SYMPTOM_SUPPRESSION_RULES:
                prefixes, reason = self.SYMPTOM_SUPPRESSION_RULES[dx_code]
                conflicts = [
                    other for other in normalized_dx_codes
                    if other != dx_code and any(other.startswith(p) for p in prefixes)
                ]
                if conflicts:
                    alert = {
                        "symptom_code": dx_code,
                        "definitive_codes": conflicts,
                        "rule": "ICD-10-CM Official Guidelines Section I.B.5",
                        "message": f"{reason} Suppress {dx_code} when {conflicts} is billed.",
                    }
                    symptom_suppression_alerts.append(alert)
                    issues.append({
                        "code": "ICD10_SYMPTOM_SUPPRESSION_VIOLATION",
                        "severity": "WARNING",
                        "message": alert["message"],
                    })
                    add_citation(
                        self.standards["icd10_cm_fy2026_guidelines"]["citation"],
                        "Section I.B.4 / I.B.5 (Signs and Symptoms)",
                        self.standards["icd10_cm_fy2026_guidelines"]["signs_and_symptoms_rule"],
                    )

        # 4. FCSO Florida Medicare LCD Medical Necessity Check
        if is_medicare:
            for ln in parsed_lines:
                c = ln["code"]
                lcd_id = self.CPT_LCD_MAP.get(c)
                if lcd_id and normalized_dx_codes:
                    covered_any = any(
                        self.ref_lib.lcd_covers(lcd_id, dx).get("covered") is True
                        for dx in normalized_dx_codes
                    )
                    if not covered_any:
                        lcd_msg = (
                            f"CPT {c} does not have a covered diagnosis under FCSO Florida MAC (09102) LCD {lcd_id} "
                            f"among billed diagnoses {normalized_dx_codes}."
                        )
                        issues.append({"code": f"FCSO_LCD_NOT_MET_{lcd_id}", "severity": "CRITICAL", "message": lcd_msg})
                        add_citation(f"FCSO Florida MAC (09102) LCD {lcd_id}", "Medical Necessity", lcd_msg)

        # 5. NCCI PTP Bundling, MUE Limits & G2211 Rules
        ncci_alerts: List[Dict[str, Any]] = []
        billed_code_list = [ln["code"] for ln in parsed_lines]
        mods_by_code: Dict[str, Set[str]] = {}
        for ln in parsed_lines:
            mods_by_code.setdefault(ln["code"], set()).update(ln["modifiers"])

        # G2211 validation
        if "G2211" in billed_code_list:
            g_meta = cpt_table["G2211"]
            add_citation(g_meta["citation"], "HCPCS G2211", g_meta["notes"])
            eligible_bases = [
                c for c in billed_code_list
                if cpt_table.get(c, {}).get("g2211_eligible") is True
            ]
            if not eligible_bases:
                g_msg = "G2211 is invalid without an eligible Office (99202-99215) or Home/Residence (99341-99350) E/M base code; NEVER valid with SNF (99304-99310)."
                ncci_alerts.append({"type": "G2211_INELIGIBLE", "codes": billed_code_list, "message": g_msg})
                issues.append({"code": "G2211_INELIGIBLE", "severity": "CRITICAL", "message": g_msg})
            else:
                exempt_prev = {"G0438", "G0439", "G0101", "G0008", "G0009", "G0010"}
                if any("25" in mods_by_code.get(b, set()) for b in eligible_bases) and not (set(billed_code_list) & exempt_prev):
                    g25_msg = "G2211 cannot be billed when the base E/M carries Modifier -25 (absent an exempt Medicare Part B preventive service)."
                    ncci_alerts.append({"type": "G2211_MODIFIER_25_CONFLICT", "codes": eligible_bases + ["G2211"], "message": g25_msg})
                    issues.append({"code": "G2211_MODIFIER_25_CONFLICT", "severity": "CRITICAL", "message": g25_msg})

        # Same-day Initial SNF (99304-99306) + Subsequent SNF (99307-99310) check
        init_snf = [c for c in billed_code_list if re.match(r"^9930[4-6]$", c)]
        sub_snf = [c for c in billed_code_list if re.match(r"^(?:9930[7-9]|99310)$", c)]
        if init_snf and sub_snf:
            snf_dup_msg = f"Initial Nursing Facility E/M ({init_snf[0]}) and Subsequent Nursing Facility E/M ({sub_snf[0]}) are mutually exclusive on the same date of service."
            ncci_alerts.append({"type": "NCCI_PTP_MUTUALLY_EXCLUSIVE", "codes": [init_snf[0], sub_snf[0]], "message": snf_dup_msg})
            issues.append({"code": "NCCI_MUTUALLY_EXCLUSIVE", "severity": "CRITICAL", "message": snf_dup_msg})

        # Pairwise NCCI PTP CSV check
        checked_pairs: Set[Tuple[str, str]] = set()
        for i, c1 in enumerate(billed_code_list):
            for c2 in billed_code_list[i + 1:]:
                if c1 == c2 or (c1, c2) in checked_pairs or (c2, c1) in checked_pairs:
                    continue
                checked_pairs.add((c1, c2))
                hit = self.ref_lib.ptp_edit(c1, c2)
                pair_order = (c1, c2)
                if not hit.get("bundled"):
                    hit = self.ref_lib.ptp_edit(c2, c1)
                    pair_order = (c2, c1)
                if not hit.get("bundled"):
                    continue

                mod_ind = str(hit.get("modifier_indicator", ""))
                add_citation("CMS National Correct Coding Initiative (NCCI) Policy Manual & Quarterly PTP Table", f"PTP {pair_order[0]}/{pair_order[1]}", f"Modifier Indicator={mod_ind}")
                if mod_ind == "0":
                    msg = f"NCCI PTP Edit: {pair_order[0]} and {pair_order[1]} are bundled/mutually exclusive (Modifier Indicator 0; no modifier override permitted)."
                    ncci_alerts.append({"type": "NCCI_PTP_INDICATOR_0", "codes": list(pair_order), "message": msg})
                    issues.append({"code": "NCCI_MUTUALLY_EXCLUSIVE", "severity": "CRITICAL", "message": msg})
                elif mod_ind == "1":
                    both_mods = mods_by_code.get(c1, set()) | mods_by_code.get(c2, set())
                    is_em_pair = any(x.startswith("99") for x in pair_order)
                    valid_override = ({"25"} if is_em_pair else {"59", "XE", "XS", "XP", "XU"})
                    if not (both_mods & valid_override):
                        req_str = "-25 on the E/M code" if is_em_pair else "-59 or X{EPSU} on the distinct procedure"
                        msg = f"NCCI PTP Edit: {pair_order[0]} + {pair_order[1]} requires {req_str} when separately identifiable (Modifier Indicator 1)."
                        ncci_alerts.append({"type": "NCCI_PTP_MISSING_MODIFIER", "codes": list(pair_order), "required": sorted(valid_override), "message": msg})
                        issues.append({"code": "NCCI_MISSING_MODIFIER", "severity": "CRITICAL", "message": msg})

        # MUE check
        for ln in parsed_lines:
            mue = self.ref_lib.mue_limit(ln["code"])
            limit = mue.get("limit")
            if limit is not None and ln["units"] > limit:
                m_msg = f"NCCI MUE Exceeded: {ln['code']} billed with {ln['units']} units exceeds CMS MUE limit of {limit} unit(s)/day (MAI={mue.get('adjudication_indicator')})."
                ncci_alerts.append({"type": "MUE_EXCEEDED", "code": ln["code"], "units": ln["units"], "limit": limit, "message": m_msg})
                issues.append({"code": "MUE_UNITS_EXCEEDED", "severity": "CRITICAL", "message": m_msg})
                add_citation("CMS NCCI Medically Unlikely Edits (MUE) Practitioner Table", f"MUE {ln['code']}", m_msg)

        # 6. FDA NDC & HCPCS Drug/Biologic Check
        drug_codes = [ln["code"] for ln in parsed_lines if HCPCS_DRUG_CODE_RE.match(ln["code"])]
        ndc_required = len(drug_codes) > 0
        raw_ndc_str: Optional[str] = None
        ndc_unit: Optional[str] = None
        ndc_qty: Optional[float] = None
        if isinstance(ndc, dict):
            raw_ndc_str = ndc.get("ndc") or ndc.get("ndc_code")
            ndc_unit = ndc.get("unit") or ndc.get("ndc_unit") or "UN"
            ndc_qty = ndc.get("quantity") if ndc.get("quantity") is not None else ndc.get("ndc_quantity", 1.0)
        elif isinstance(ndc, str) and ndc.strip():
            raw_ndc_str = ndc.strip()
            ndc_unit = "UN"
            ndc_qty = 1.0

        ndc_issues: List[str] = []
        norm_ndc_11 = normalize_fda_ndc_11(raw_ndc_str) if raw_ndc_str else None
        if ndc_required or raw_ndc_str is not None:
            add_citation(
                self.standards["fda_ndc_rules"]["citation"],
                "Loop 2410 Drug Identification (LIN*N4 / CTP)",
                self.standards["fda_ndc_rules"]["edi_mapping"],
            )
            if not norm_ndc_11:
                msg = f"HCPCS drug/biologic code(s) {drug_codes or ['line']} require a valid 11-digit 5-4-2 FDA NDC (received: {raw_ndc_str!r})."
                ndc_issues.append(msg)
                issues.append({"code": "MISSING_OR_INVALID_FDA_NDC", "severity": "CRITICAL", "message": msg})
            if ndc_unit and str(ndc_unit).strip().upper() not in VALID_NDC_UNITS:
                u_msg = f"Invalid X12 Loop 2410 NDC unit '{ndc_unit}'; expected one of {sorted(VALID_NDC_UNITS)}."
                ndc_issues.append(u_msg)
                issues.append({"code": "MISSING_OR_INVALID_FDA_NDC", "severity": "CRITICAL", "message": u_msg})
            if ndc_qty is not None and float(ndc_qty) <= 0:
                q_msg = f"Loop 2410 NDC quantity must be > 0 (received: {ndc_qty})."
                ndc_issues.append(q_msg)
                issues.append({"code": "MISSING_OR_INVALID_FDA_NDC", "severity": "CRITICAL", "message": q_msg})

        ndc_check = {
            "required": ndc_required,
            "valid": len(ndc_issues) == 0,
            "drug_codes": drug_codes,
            "normalized_ndc_11": norm_ndc_11,
            "issues": ndc_issues,
        }

        # 7. Provider & Payer Specific Advisory Notes (APRN 85% / FS §464.0123 / QMB Protection)
        reimbursement_notes: List[str] = []
        if "APRN" in prov_upper or "NP" in prov_upper or "PA" in prov_upper:
            aprn_meta = self.standards["medicare_fl_fcso"]["aprn_pa_rules"]
            reimbursement_notes.append(
                "Direct APRN/PA Medicare reimbursement is 85% of the Physician Fee Schedule (Social Security Act § 1833(a)(1)(O)). "
                "Incident-To 100% billing (42 CFR § 410.26) is prohibited in SNF/NF/Inpatient POS 21, 31, and 32. "
                "In Florida, autonomous APRN registration (FS § 464.0123) is restricted to Primary Care; specialty APRNs require a collaborative physician protocol."
            )
            add_citation(aprn_meta["citation"], "APRN/PA Reimbursement & FL Scope", aprn_meta["fl_autonomous_aprn_statute"])

        if is_medicaid_or_qmb:
            qmb_meta = self.standards["florida_medicaid_ahca"]["qmb_federal_prohibition"]
            reimbursement_notes.append(qmb_meta["rule"])
            add_citation(qmb_meta["citation"], "QMB Balance Billing Prohibition", qmb_meta["rule"])

        if is_medicare:
            reimbursement_notes.append(
                "Medicare Part B Florida (First Coast Service Options Payer ID 09102) timely filing limit is 365 days from Date of Service (42 CFR § 424.44)."
            )
            add_citation(
                self.standards["medicare_fl_fcso"]["timely_filing_citation"],
                "Medicare Timely Filing",
                "Claims must be filed within 1 calendar year (365 days) from the Date of Service.",
            )

        critical_errors = [i for i in issues if i["severity"] == "CRITICAL"]
        verified = len(critical_errors) == 0 and len(parsed_lines) > 0 and len(valid_billable_icd10) > 0

        return {
            "verified": verified,
            "valid_billable_icd10": valid_billable_icd10,
            "invalid_icd10": invalid_icd10,
            "non_billable_suggestions": non_billable_suggestions,
            "symptom_suppression_alerts": symptom_suppression_alerts,
            "ncci_alerts": ncci_alerts,
            "pos_cpt_compatibility": {
                "compatible": len(pos_issues) == 0,
                "pos": pos_str,
                "pos_description": pos_desc,
                "issues": pos_issues,
            },
            "ndc_check": ndc_check,
            "reimbursement_and_payer_notes": reimbursement_notes,
            "issues": issues,
            "citations": citations,
        }

    @staticmethod
    def _fallback_pos_name(pos: str) -> Optional[str]:
        extra = {
            "14": "Group Home",
            "16": "Temporary Lodging",
            "19": "Off Campus-Outpatient Hospital",
            "20": "Urgent Care Facility",
            "22": "On Campus-Outpatient Hospital",
            "23": "Emergency Room - Hospital",
            "24": "Ambulatory Surgical Center",
            "33": "Custodial Care Facility",
            "34": "Hospice",
            "81": "Independent Laboratory",
        }
        return extra.get(pos)

    # ── Method 2: Zero-Hallucination Citation-Grounded Biller/Coder Assistant ─

    def ask_assistant(self, query: str, allow_openfda_live: bool = False) -> Dict[str, Any]:
        """
        Answers biller and coder regulatory/coding questions strictly from verified
        reference tables (`simple_icd_10_cm`, `standards_2026.json`, and CMS/FCSO CSVs).
        Enforces an explicit anti-hallucination refusal guard (`status="ABSTAINED_UNVERIFIED"`)
        whenever a query asks about unverified codes, fee-schedule dollar amounts/RVUs,
        or out-of-scope clinical/legal advice.
        """
        q = (query or "").strip()
        if len(q) < 3:
            return self._abstain("Query is empty or too short to match a verified coding/billing standard.")

        q_low = q.lower()

        # Guard 1: Out-of-scope clinical prescribing or legal strategy questions
        if self.OUT_OF_SCOPE_CLINICAL_OR_LEGAL_RE.search(q):
            return self._abstain(
                "Query requests clinical treatment/prescribing or legal advice outside verified coding and billing standards."
            )

        # Guard 2: Unverified exact dollar fee schedules / RVUs / conversion factors
        if self.UNVERIFIED_PRICING_QUERY_RE.search(q):
            return self._abstain(
                "Exact dollar fee-schedule allowables, GPCI locality RVUs, and commercial contract rates are not stored in "
                "static reference tables; verify live locality pricing on the FCSO MPFS Lookup (09102) or payer contract."
            )

        sections: List[str] = []
        facts: Dict[str, Any] = {}
        citations: List[Dict[str, str]] = []

        def cite(source: str, section: str, summary: str) -> None:
            entry = {"source": source, "section": section, "summary": summary}
            if entry not in citations:
                citations.append(entry)

        cpt_table = self.standards["cpt_codes"]
        deleted_table = self.standards["deleted_or_nonpayable_cpt"]

        # Extract candidate NDC patterns first so 5-digit NDC segments aren't mistaken for CPT codes
        ndc_hyphen_matches = re.findall(r"\b\d{4,5}-\d{3,4}-\d{1,2}\b", q)
        ndc_11_matches = re.findall(r"\b\d{11}\b", q)
        q_without_ndc = q
        for m_ndc in ndc_hyphen_matches + ndc_11_matches:
            q_without_ndc = q_without_ndc.replace(m_ndc, " ")
        # Also mask 9-digit receiver/ZIP+4 and 10-digit NPI/Medicaid numbers
        q_without_long_ids = re.sub(r"\b\d{9,10}\b", " ", q_without_ndc)

        # Guard 3: Check any explicit 5-character CPT or HCPCS codes mentioned in the query
        candidate_5digit = re.findall(r"\b\d{5}\b", q_without_long_ids)
        candidate_hcpcs = re.findall(r"\b[A-CE-HJ-MP-V]\d{4}\b", q_without_long_ids.upper())

        unverified_codes: List[str] = []
        matched_cpts: List[str] = []
        for tok in candidate_5digit + candidate_hcpcs:
            if tok in self.KNOWN_NON_CPT_NUMBERS:
                continue
            # Skip LCD IDs (L36805, L36063, L33947, L33622)
            if tok in ("L36805", "L36063", "L33947", "L33622"):
                continue
            if tok in cpt_table or tok in deleted_table or re.match(r"^(?:9924[1-5]|9925[1-5]|9932[4-8]|9933[4-7])$", tok):
                if tok not in matched_cpts:
                    matched_cpts.append(tok)
            elif HCPCS_DRUG_CODE_RE.match(tok) and ("ndc" in q_low or "2410" in q_low or "drug" in q_low or "j-code" in q_low or ndc_hyphen_matches or ndc_11_matches):
                if tok not in matched_cpts:
                    matched_cpts.append(tok)
            else:
                unverified_codes.append(tok)

        if unverified_codes:
            return self._abstain(
                f"Code(s) {', '.join(sorted(set(unverified_codes)))} are not present in the verified 2026 CPT/HCPCS reference library. "
                "Refusing to guess descriptors, bundling rules, or coverage."
            )

        # 1. Explicit ICD-10-CM Code Lookups (live simple_icd_10_cm verification)
        icd_candidates = re.findall(r"\b([A-TV-Z][0-9]{2}(?:\.[0-9A-Z]{1,4})?)\b", q.upper())
        reserved_tokens = {"POS", "NPI", "CPT", "MBI", "NDC", "COB", "QMB", "MMA", "LTC", "SNF", "E11"}  # E11 IS an ICD-10 category!
        reserved_non_icd = {"POS", "NPI", "CPT", "MBI", "NDC", "COB", "QMB", "MMA", "LTC", "SNF"}
        for cand in icd_candidates:
            if cand in reserved_non_icd or cand in matched_cpts or cand in ("L36805", "L36063", "L33947", "L33622", "G2211"):
                continue
            # Exclude X12 segment qualifiers like N403, S001, A100 unless valid ICD-10 or explicit '.'
            info = self.lookup_icd10(cand)
            if not info["exists"]:
                if "." in cand or "icd" in q_low or "diagnosis" in q_low or "billable" in q_low:
                    return self._abstain(
                        f"'{cand}' is NOT a valid code in the official FY2026 CMS/CDC ICD-10-CM classification."
                    )
                continue

            facts.setdefault("icd10_lookups", []).append(info)
            cite(
                self.standards["icd10_cm_fy2026_guidelines"]["citation"],
                "ICD-10-CM Section I.B.2 / I.B.18",
                self.standards["icd10_cm_fy2026_guidelines"]["specificity_rule"],
            )
            if info["billable"]:
                sections.append(
                    f"ICD-10-CM {info['code']} ('{info['description']}') IS a valid, billable leaf code for FY2026."
                )
            else:
                sugs = ", ".join(f"{s['code']} ({s['description']})" for s in info["billable_suggestions"][:6])
                sections.append(
                    f"ICD-10-CM {info['code']} ('{info['description']}') is a NON-BILLABLE category/subcategory header. "
                    f"Specific billable child codes: {sugs}."
                )

        # 2. Explicit CPT / HCPCS Code Answers
        for c in matched_cpts:
            if c in cpt_table:
                meta = cpt_table[c]
                facts.setdefault("cpt_lookups", {})[c] = meta
                time_part = f", min total time >= {meta['min_time_minutes']} min" if meta.get("min_time_minutes") else ""
                mdm_part = f" (MDM: {meta['mdm']}{time_part})" if meta.get("mdm") else ""
                pos_part = f" Allowed POS: {', '.join(meta['allowed_pos'])}." if meta.get("allowed_pos") else ""
                extra_note = f" {meta['notes']}" if meta.get("notes") else ""
                sections.append(f"{c}: {meta['category']}{mdm_part}.{pos_part}{extra_note}")
                cite(meta["citation"], f"CPT/HCPCS {c}", meta["category"])
            elif c in deleted_table or re.match(r"^(?:9924[1-5]|9925[1-5]|9932[4-8]|9933[4-7])$", c):
                msg = deleted_table.get(c) or deleted_table["99252"]
                sections.append(f"CPT {c}: {msg}")
                cite("CMS IOM Pub. 100-04, Ch. 12, § 30.6.13 / AMA CPT 2026", f"CPT {c}", msg)

        # 3. FDA NDC Conversion & Loop 2410 Drug Rules
        if ndc_hyphen_matches or ndc_11_matches or any(k in q_low for k in ("ndc", "5-4-2", "loop 2410", "lin*n4", "j-code", "openfda")):
            fda_rules = self.standards["fda_ndc_rules"]
            for raw_n in ndc_hyphen_matches + ndc_11_matches:
                ndc_res = self.lookup_ndc(raw_n, allow_openfda_live=allow_openfda_live)
                facts.setdefault("ndc_lookups", []).append(ndc_res)
                if ndc_res["valid_format"]:
                    sections.append(
                        f"FDA NDC '{raw_n}' normalizes to 11-digit 5-4-2 billing format '{ndc_res['normalized_ndc_11']}' "
                        f"(X12 Loop 2410: LIN**N4*{ndc_res['normalized_ndc_11']}~ + CTP with unit qualifier {', '.join(sorted(VALID_NDC_UNITS))})."
                    )
                else:
                    sections.append(f"FDA NDC '{raw_n}' is invalid for 11-digit 5-4-2 conversion.")
            if not (ndc_hyphen_matches or ndc_11_matches):
                sections.append(
                    f"FDA/CMS NDC Rule: HCPCS drug/biologic codes (J-codes, Q-biosimilars, S0010-S0199) require an 11-digit 5-4-2 NDC "
                    f"(4-4-2 pads 1st segment, 5-3-2 pads 2nd segment, 5-4-1 pads 3rd segment; omit hyphens) and valid X12 unit "
                    f"({', '.join(sorted(VALID_NDC_UNITS))}) in Loop 2410 (LIN*N4 / CTP). Single-dose Part B vials require JW (discarded) or JZ (zero wasted)."
                )
            cite(fda_rules["citation"], "FDA 11-Digit NDC & Loop 2410", fda_rules["edi_mapping"])

        # 4. SNF Initial Consultation (99304-99306) vs Subsequent Care (99307-99310) & POS 31 vs 32
        if any(k in q_low for k in ("snf", "nursing facility", "consultation", "consult", "eftekhari", "pos 31", "pos 32", "consolidated billing", "modifier ai")):
            sections.append(
                "SNF / Nursing Facility E/M & Consultations: Medicare eliminated CPT consultation codes (99242-99255). "
                "Initial Nursing Facility Care (99304: >=25m SF/Low; 99305: >=35m Moderate; 99306: >=45m CMS/50m AMA High) is billed "
                "once per admission per practitioner — consulting specialists (e.g., PM&R) report 99304-99306 WITHOUT Modifier -AI "
                "(Modifier -AI is strictly for the Principal/Attending Physician of Record). Follow-up visits use Subsequent NF Care "
                "(99307: >=10m; 99308: >=15-20m; 99309: >=30m; 99310: >=45m). POS 31 = Medicare Part A SNF stay (E/M excluded from "
                "SNF Consolidated Billing and billed to Part B 09102; therapy 97110/97140 bundled into SNF Part A); POS 32 = NF without Part A stay."
            )
            cite("CMS IOM Pub. 100-04, Ch. 12, § 30.6.13; MLN Matters MM6740", "SNF Consultations & Modifier -AI", self.standards["cpt_codes"]["99304"]["notes"])
            cite("Social Security Act § 1888(e); 42 CFR § 411.15(p)", "POS 31 SNF Consolidated Billing vs POS 32", self.standards["pos_codes"]["31"]["rule"])

        # 5. G2211 Complexity Add-On
        if "g2211" in q_low:
            g_meta = self.standards["cpt_codes"]["G2211"]
            if "G2211" not in matched_cpts:
                sections.append(f"G2211 Rule: {g_meta['notes']}")
                cite(g_meta["citation"], "HCPCS G2211 Eligibility", g_meta["notes"])

        # 6. Telehealth POS 02 vs POS 10 & Modifiers 95 / 93
        if any(k in q_low for k in ("telehealth", "telemedicine", "pos 02", "pos 10", "modifier 95", "modifier 93")):
            sections.append(
                "Medicare Telehealth (2025/2026): Report POS 10 when the patient is in their private home (paid at higher non-facility MPFS rate) "
                "and POS 02 when the patient is in a facility/SNF/clinic (paid at facility MPFS rate). Pair with Modifier -95 (real-time audio-video) "
                "or Modifier -93 (eligible audio-only)."
            )
            cite(self.standards["pos_codes"]["10"]["citation"], "Telehealth POS 02 vs POS 10", self.standards["pos_codes"]["10"]["rule"])

        # 7. Medicare MBI Specification
        if any(k in q_low for k in ("mbi", "medicare beneficiary id", "beneficiary identifier")):
            mbi_spec = self.standards["medicare_fl_fcso"]["mbi_format"]
            # Check if user asked to validate a candidate MBI token
            cand_mbis = re.findall(r"\b[0-9A-Z]{11}\b", q.upper())
            for cm in cand_mbis:
                is_ok = valid_mbi(cm)
                facts.setdefault("mbi_checks", {})[cm] = is_ok
                sections.append(f"MBI '{cm}' validation: {'VALID' if is_ok else 'INVALID (fails CMS slot type or contains banned letters S, L, O, I, B, Z)'}.")
            sections.append(
                "CMS 11-Character MBI Format: Pos 1 = 1-9; Pos 2,5,8,9 = A-Z (excluding S, L, O, I, B, Z); "
                "Pos 3,6 = 0-9 or A-Z (excluding S, L, O, I, B, Z); Pos 4,7,10,11 = 0-9. Omit all hyphens in X12 NM109."
            )
            cite(mbi_spec["citation"], "11-Character MBI Specification", mbi_spec["edi_rule"])

        # 8. Modifiers (25, 59, XE/XS/XP/XU, 24, 57, GV, GW, QW, JW, JZ)
        mod_matches = re.findall(r"\bmodifier\s*[-]?\s*([a-z0-9]{2})\b", q_low)
        if "59" in q_low and ("25" in q_low or "xe" in q_low or "subset" in q_low):
            for m_key in ("25", "59", "XE", "XS", "XP", "XU"):
                if m_key not in [x.upper() for x in mod_matches]:
                    mod_matches.append(m_key)
        for m_raw in mod_matches:
            mk = m_raw.upper()
            if mk in self.standards["modifiers"]:
                m_info = self.standards["modifiers"][mk]
                sections.append(f"Modifier -{mk} ({m_info['name']}): {m_info['rule']}")
                cite(m_info["citation"], f"Modifier -{mk}", m_info["rule"])

        # 9. Florida Medicaid (77027), Crossover COB & Federal QMB Balance Billing Prohibition
        if any(k in q_low for k in ("qmb", "balance bill", "medicaid", "77027", "ahca", "crossover", "mma", "dual eligible", "dual-eligible")):
            fl_mcd = self.standards["florida_medicaid_ahca"]
            qmb_info = fl_mcd["qmb_federal_prohibition"]
            sections.append(
                f"Florida Medicaid (AHCA Payer ID 77027) & QMB Rules: Standard FL Medicaid Recipient ID is 10 numeric digits. "
                f"Timely filing is 365 days (original FFS) / 180 days from Medicare EOMB for crossovers (Rule 59G-1.052). "
                f"CRITICAL QMB RULE ({qmb_info['citation']}): {qmb_info['rule']}"
            )
            cite(qmb_info["citation"], "QMB Balance Billing Prohibition", qmb_info["rule"])
            cite(fl_mcd["timely_filing"]["citation"], "FL Medicaid AHCA 77027 & Crossover COB", fl_mcd["crossover_cob_rule"])

        # 10. Florida APRN Scope (FS § 464.0123), 85% MPFS & Split/Shared
        if any(k in q_low for k in ("aprn", "nurse practitioner", "464.0123", "incident-to", "incident to", "split/shared", "split shared")):
            aprn_info = self.standards["medicare_fl_fcso"]["aprn_pa_rules"]
            sections.append(
                f"APRN / PA & FL Scope Rules: Medicare pays direct APRN/PA claims at 85% of MPFS (100% for MD/DO). "
                f"Incident-To (42 CFR § 410.26) is strictly prohibited in institutional facilities (POS 21, 31, 32). "
                f"{aprn_info['fl_autonomous_aprn_statute']}"
            )
            cite(aprn_info["citation"], "APRN/PA Billing & FL Statute § 464.0123", aprn_info["fl_autonomous_aprn_statute"])

        # 11. Timely Filing (Medicare vs Medicaid)
        if "timely filing" in q_low or "filing limit" in q_low or "424.44" in q_low:
            sections.append(
                "Timely Filing Deadlines: Medicare Part B (FCSO FL 09102) = 365 calendar days from Date of Service (42 CFR § 424.44). "
                "Florida Medicaid FFS (AHCA 77027) = 365 days from DOS; Medicare-Medicaid crossovers / SMMC MMA managed care = 180 days."
            )
            cite(self.standards["medicare_fl_fcso"]["timely_filing_citation"], "Timely Filing", "Medicare 365 days; FL Medicaid 365d FFS / 180d crossover-MMA.")

        # 12. ICD-10-CM Lab-Only & Uncertain Diagnosis Guidelines
        if any(k in q_low for k in ("lab value", "lab-only", "rule out", "suspected", "symptom suppression")):
            g_icd = self.standards["icd10_cm_fy2026_guidelines"]
            sections.append(f"ICD-10-CM Official Guidelines (FY2026): {g_icd['outpatient_uncertain_and_lab_rule']} {g_icd['signs_and_symptoms_rule']}")
            cite(g_icd["citation"], "Section I.B.5, III.B & IV.H", g_icd["outpatient_uncertain_and_lab_rule"])

        # 13. Office Ally Clearinghouse & Payer IDs
        if any(k in q_low for k in ("office ally", "330897513", "09102", "sftp", "ftp10", "277ca", "999", "835", "payer id")):
            oa = self.standards["office_ally_clearinghouse"]
            payer_summary = "; ".join(f"{pid}={pname}" for pid, pname in oa["verified_payer_ids"].items())
            sections.append(
                f"Office Ally Clearinghouse: Receiver ID {oa['receiver_id']} (ISA07='30', ISA08/GS03='330897513'). "
                f"Verified Payer IDs: {payer_summary}. "
                f"Channels: Web Portal ({oa['channels']['web_portal']}), SFTP ({oa['channels']['sftp']}), "
                f"Real-Time 270/271 SOAP ({oa['channels']['realtime_270_271_soap']}). Lifecycle: {oa['lifecycle']}."
            )
            cite(oa["citation"], "Office Ally 5010A1 Companion Rules", oa["lifecycle"])

        if not sections:
            return self._abstain(
                "Query did not match any verified code or policy in the 2026 AMA CPT, CMS/FCSO Medicare, FY2026 ICD-10-CM, "
                "FDA NDC, Florida Medicaid AHCA, or Office Ally reference tables."
            )

        return {
            "status": "VERIFIED",
            "verified": True,
            "query": q,
            "answer": " ".join(sections),
            "facts": facts,
            "citations": citations,
        }

    @staticmethod
    def _abstain(reason: str) -> Dict[str, Any]:
        return {
            "status": "ABSTAINED_UNVERIFIED",
            "verified": False,
            "answer": f"ABSTAINED (Zero-Hallucination Guard): {reason}",
            "facts": {},
            "citations": [],
        }
