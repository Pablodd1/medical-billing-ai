# ============================================
# FILE: compliance/claim_gate.py
# PURPOSE: Pre-pipeline claim gates: hard holds (missing/ambiguous identity data),
#          coder-review soft flags, billing-entity validation. No silent defaults.
# DEPENDENCIES: stdlib only
# ============================================

import os
import re
from typing import Any, Dict, List, Optional, Tuple

PLACEHOLDER_TAX_IDS = {"591234567", "123456789", "000000000", "111111111"}
PLACEHOLDER_PHONES = {"8005551212", "0000000000", "1234567890"}

# keyword -> canonical name. All are commercial/Medicare-Advantage plans: a chart
# listing one means Original Medicare Part B may NOT be the primary payer.
KNOWN_OTHER_PLANS: Dict[str, str] = {
    "humana": "HUMANA",
    "simply": "SIMPLY HEALTHCARE",
    "aetna": "AETNA",
    "cigna": "CIGNA",
    "united": "UNITEDHEALTHCARE",
    "molina": "MOLINA HEALTHCARE",
    "sunshine": "SUNSHINE HEALTH",
    "wellcare": "WELLCARE",
    "florida blue": "FLORIDA BLUE",
    "devoted": "DEVOTED HEALTH",
    "careplus": "CAREPLUS HEALTH PLANS",
    "avmed": "AVMED",
    "leon": "LEON HEALTH",
    "medica": "MEDICA HEALTHCARE",
    "preferred care": "PREFERRED CARE PARTNERS",
    "doctors healthcare": "DOCTORS HEALTHCARE PLANS",
    "alignment": "ALIGNMENT HEALTH PLAN",
    "clover": "CLOVER HEALTH",
    "oscar": "OSCAR HEALTH",
    "ambetter": "AMBETTER",
}

# CMS Official 11-Character Medicare Beneficiary Identifier (MBI) Format:
# Pos 1: 1-9
# Pos 2, 5, 8, 9: A-Z excluding S, L, O, I, B, Z
# Pos 3, 6: 0-9 or A-Z excluding S, L, O, I, B, Z
# Pos 4, 7, 10, 11: 0-9
CMS_MBI_RE = re.compile(
    r"^[1-9][AC-HJKMNP-RT-Y][0-9AC-HJKMNP-RT-Y][0-9]"
    r"[AC-HJKMNP-RT-Y][0-9AC-HJKMNP-RT-Y][0-9]"
    r"[AC-HJKMNP-RT-Y]{2}[0-9]{2}$"
)

# Secondary-only / crossover payers that do NOT displace Original Medicare Part B as primary payer
PURE_SECONDARY_PAYERS = {
    "FLORIDA MEDICAID",
    "FLORIDA MEDICAID (QMB)",
    "MCRA CO-PAY MEDICAID (QMB)",
    "TRICARE",
    "AARP",
    "AARP SUPPLEMENTAL",
    "MEDIGAP",
}

LAB_PRONE_DX = {"D64.9", "E83.52", "D72.829", "E87.1", "E87.6", "D69.6", "R79.89"}
EM_CODE_RE = re.compile(r"\b(?:9930[4-9]|99310)\b")
TIME_RE = re.compile(r"(\d+)\s*(?:minutes|mins|min)\b", re.IGNORECASE)
QMB_NOTE_RE = re.compile(
    r"(?:MCRA\s*Co[\-\s]*Pay\s*Med[il1]ca[il1]d|"
    r"QMB\s*El[il1g]*ble[:\s]*(?:Yes|Y)\b)",
    re.IGNORECASE,
)
INITIAL_SNF_NOTE_RE = re.compile(
    r"(?:consult(?:ation|ion)\s*report|"
    r"physiatr?ist\s*consult|pm&r\s*consultation|"
    r"\binitial\s+(?:nursing\s+facility|snf|evaluation|consult(?:ation)?|visit|assessment)\b|"
    r"evaluation\s*and\s*treatment\s*courtesy\s*of\s*dr)",
    re.IGNORECASE,
)

Issue = Dict[str, str]


def _issue(code: str, message: str, severity: str = "CRITICAL") -> Issue:
    return {"tier": "Tier 0: Claim Gate", "severity": severity, "code": code, "message": message}


def valid_npi(npi: Optional[str]) -> bool:
    """NPI check: 10 digits, Luhn over '80840' + NPI (CMS standard)."""
    if not npi or not re.fullmatch(r"\d{10}", str(npi)):
        return False
    total = 0
    for i, ch in enumerate(reversed("80840" + str(npi))):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return total % 10 == 0


def valid_mbi(mbi: Optional[str]) -> bool:
    """CMS 11-character MBI check: enforces exact slot types and bans S, L, O, I, B, Z."""
    if not mbi or not isinstance(mbi, (str, int)):
        return False
    cleaned = str(mbi).strip().upper()
    return bool(CMS_MBI_RE.fullmatch(cleaned))


def canonical_payer(raw: Optional[str]) -> Optional[str]:
    """Maps OCR text to a known plan name; returns None for anything unrecognised."""
    low = (raw or "").lower()
    if not low:
        return None
    for key, name in KNOWN_OTHER_PLANS.items():
        # Allow optional whitespace in multi-word or compound plan names (e.g. PREFERREDCAREPARTNERS, CARE PLUS)
        parts = key.split()
        if len(parts) > 1:
            key_pat = r"\s*".join(re.escape(p) for p in parts)
        elif key == "careplus":
            key_pat = r"care\s*plus"
        elif key == "wellcare":
            key_pat = r"well\s*care"
        elif key == "avmed":
            key_pat = r"av\s*med"
        else:
            key_pat = re.escape(key)
        # Require trailing word boundary or known plan suffix so 'medica' never matches 'Medicare'/'Medicaid'/'Medicald'
        pattern = rf"\b{key_pat}(?:\s*(?:health(?:\s*care)?|partners|plans?|medical|insurance|hmo|ppo))?\b"
        if re.search(pattern, low):
            return name
    return None


def _squash(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def street_needs_verification(street: Optional[str]) -> bool:
    """Flags OCR-mangled or truncated Box 5 streets that are unmailable without human check
    (e.g. concatenated digits+suffix '25927ST' with no street name/ordinal, or dangling
    single-letter OCR fragment after an ordinal street like '6060 NW 55TH N')."""
    if not street:
        return False
    s = re.sub(r"\s+", " ", str(street).strip().upper())
    if not s:
        return False
    # Case 1: Bare digits + optional street suffix with no street name or ordinal (e.g. '25927ST', '25927 ST', '25927')
    if re.fullmatch(r"\d+\s*(?:ST|AVE|RD|CT|DR|LN|WAY|BLVD|PL|TER|TERRACE|CIR|MNR)?", s):
        return True
    # Case 2: Ordinal street followed at end (or before APT/UNIT/STE/BLDG) by a lone single letter instead of street suffix
    # (e.g. '6060 NW 55TH N' where 'L' in 'LN' was dropped by OCR)
    if re.search(r"\b\d+(?:ST|ND|RD|TH)\s+[A-Z](?:\s+(?:APT|UNIT|STE|BLDG)\b.*)?$", s):
        return True
    return False


def name_matches_filename(last: str, first: str, filename: str) -> bool:
    """OCR name must be consistent with the human-assigned file name: the squashed last name
    must appear in it, the OCR first-name token must equal a whole file-name token
    (catches merged initials such as 'ROSAM' for 'ROSA M'), and if both have a middle
    initial/name token immediately following the first name, their first letters must match
    (catches 'SONIA I.' on chart vs 'SONIA L.pdf' while allowing 'THERESA MORRISON' vs 'M.')."""
    stem_raw = re.sub(r"\.pdf$", "", filename, flags=re.IGNORECASE)
    stem = _squash(stem_raw)
    file_tokens_list = [_squash(t) for t in re.split(r"[\s\.]+", stem_raw) if _squash(t)]
    tokens = set(file_tokens_list)
    first_parts = [_squash(t) for t in re.split(r"[\s\.]+", first or "") if _squash(t)]
    first_token = first_parts[0] if first_parts else ""
    if not (bool(stem) and _squash(last) in stem and first_token in tokens):
        return False
    if len(first_parts) >= 2:
        idx = file_tokens_list.index(first_token)
        if idx + 1 < len(file_tokens_list):
            file_mid = file_tokens_list[idx + 1]
            if file_mid and first_parts[1][0] != file_mid[0]:
                return False
    return True


def hard_gate_issues(demo: Dict[str, Any], filename: str, overrides: Dict[str, Any]) -> List[Issue]:
    """Issues that block a claim from the 837 until a human resolves them."""
    issues: List[Issue] = []
    mbi = demo.get("member_id")
    if mbi and not valid_mbi(mbi):
        issues.append(_issue(
            "INVALID_MBI_FORMAT",
            f"Medicare MBI '{mbi}' fails CMS 11-character MBI specification "
            "(Pos 1: 1-9; Pos 2,5,8,9: A-Z excl S,L,O,I,B,Z; Pos 3,6: 0-9/A-Z excl S,L,O,I,B,Z; Pos 4,7,10,11: 0-9). "
            "Check Medicare card for OCR 0/O, 1/I, 5/S, 8/B, or 2/Z errors."
        ))
    if not demo.get("gender"):
        issues.append(_issue("MISSING_SEX", "Patient sex (Box 3) not found on chart; payers reject unknown/incorrect sex."))
    if not all(demo.get(k) for k in ("address", "city", "state", "zip")):
        issues.append(_issue("MISSING_ADDRESS", "Patient home address (Box 5) not found; facility address must not be substituted."))
    elif (not overrides.get("address_verified") and not overrides.get("address")
          and street_needs_verification(demo.get("address"))):
        issues.append(_issue(
            "ADDRESS_NEEDS_VERIFICATION",
            f"Patient street address '{demo.get('address')}' (Box 5) appears OCR-truncated or concatenated without street name/suffix. "
            "Verify against face sheet and set address or address_verified in overrides."
        ))
    if (demo.get("last_name") and not overrides.get("name_verified")
            and not name_matches_filename(demo["last_name"], demo.get("first_name") or "", filename)):
        issues.append(_issue("NAME_FILENAME_MISMATCH",
                             f"OCR name '{demo.get('patient_name')}' does not match file name '{filename}'. "
                             "Verify spelling against the Medicare card, then set name_verified in overrides."))
    if demo.get("part_a_only") and not overrides.get("part_b_coverage_confirmed"):
        issues.append(_issue(
            "MEDICARE_PART_A_ONLY_NO_PART_B",
            "Face sheet lists 'Medicare Coverage: Part A only' — patient lacks active Medicare Part B coverage for physician/PM&R professional services. "
            "Verify 270/271 Part B entitlement or alternative primary payer before billing."
        ))
    sec_payer = str(demo.get("secondary_payer") or "").strip()
    is_unverified_plan = bool(
        demo.get("managed_medicare")
        or (sec_payer and sec_payer.upper() not in PURE_SECONDARY_PAYERS)
    )
    if is_unverified_plan and not overrides.get("primary_payer_confirmed"):
        plan_label = sec_payer or "Managed Medicare / Part C"
        issues.append(_issue("PRIMARY_PAYER_UNVERIFIED",
                             f"Chart lists '{plan_label}'. Patient may be in a Medicare Advantage/commercial plan; "
                             "run eligibility, then set primary_payer_confirmed in overrides if Original Medicare is primary."))
    if not valid_npi(demo.get("rendering_npi")):
        issues.append(_issue("INVALID_RENDERING_NPI", f"Rendering NPI '{demo.get('rendering_npi')}' fails NPI check digit."))
    return issues


def coder_review_notes(doc_text: str, icd10_codes: List[str], ov: Optional[Dict[str, Any]] = None) -> List[Issue]:
    """Soft flags: claim stays queued but a coder must confirm before upload."""
    notes: List[Issue] = []
    if (ov or {}).get("place_of_service") not in ("31", "32"):
        notes.append(_issue("POS_UNVERIFIED",
                            "POS defaulted from facility text. Confirm resident's status with the facility: "
                            "31 = Medicare Part A SNF stay, 32 = nursing facility (no Part A). "
                            "Set place_of_service in overrides.", "WARNING"))
    if not EM_CODE_RE.search(doc_text) and not TIME_RE.search(doc_text):
        is_initial = bool(INITIAL_SNF_NOTE_RE.search(doc_text or ""))
        code_range = "99304-99306 (Initial NF / Specialist Consult)" if is_initial else "99307-99310 (Subsequent NF)"
        notes.append(_issue("EM_LEVEL_HEURISTIC",
                            f"No documented E/M code or total time; level was inferred from diagnosis count. "
                            f"Confirm {code_range} against MDM/time.", "WARNING"))
    lab = sorted(set(icd10_codes) & LAB_PRONE_DX)
    if lab:
        notes.append(_issue("LAB_DERIVED_DX_CHECK",
                            f"Confirm provider documented a diagnosis (not just a lab value) for: {', '.join(lab)}.", "WARNING"))
    if (ov or {}).get("is_qmb") or QMB_NOTE_RE.search(doc_text or ""):
        notes.append(_issue(
            "QMB_DO_NOT_BALANCE_BILL",
            "QMB / MCRA Co-Pay Medicaid dual-eligible beneficiary (Social Security Act § 1902(n)(3)(B)): "
            "NEVER balance-bill patient for Medicare Part B deductible or 20% coinsurance; route crossover to FL Medicaid (77027).",
            "WARNING",
        ))
    return notes


def load_billing_entity(env: Optional[Dict[str, str]] = None) -> Tuple[Dict[str, str], List[str]]:
    """Billing provider (Loop 2010AA) from BILLING_* env vars. Returns (entity, problems)."""
    e = env if env is not None else os.environ
    entity = {
        "organization_name": e.get("BILLING_ORG_NAME", "").strip(),
        "billing_npi_type_2": e.get("BILLING_NPI", "").strip(),
        "tax_id": re.sub(r"\D", "", e.get("BILLING_TAX_ID", "")),
        "phone": re.sub(r"\D", "", e.get("BILLING_PHONE", "")),
        "address": e.get("BILLING_ADDRESS", "").strip(),
        "city": e.get("BILLING_CITY", "").strip(),
        "state": e.get("BILLING_STATE", "").strip().upper(),
        "zip": e.get("BILLING_ZIP", "").strip(),
        "contact_name": e.get("BILLING_CONTACT", "BILLING DEPT").strip(),
    }
    problems = [f"BILLING_{k.upper()} missing" for k in
                ("org_name", "npi", "tax_id", "phone", "address", "city", "state", "zip")
                if not entity[{"org_name": "organization_name", "npi": "billing_npi_type_2"}.get(k, k)]]
    if entity["billing_npi_type_2"] and not valid_npi(entity["billing_npi_type_2"]):
        problems.append("BILLING_NPI fails NPI check digit")
    if entity["tax_id"] and (len(entity["tax_id"]) != 9 or entity["tax_id"] in PLACEHOLDER_TAX_IDS):
        problems.append("BILLING_TAX_ID is invalid or a placeholder")
    if entity["phone"] and entity["phone"] in PLACEHOLDER_PHONES:
        problems.append("BILLING_PHONE is a placeholder")
    return entity, problems

def icd10_issues(codes: List[str]) -> List[Issue]:
    """Hard holds for ICD-10-CM codes that do not exist or are not billable (non-leaf)."""
    try:
        import simple_icd_10_cm as icd
    except ImportError:
        return [_issue("ICD10_LIBRARY_MISSING", "pip install simple-icd-10-cm to validate diagnosis codes.", "WARNING")]
    problems: List[Issue] = []
    for code in codes:
        if not icd.is_valid_item(code):
            problems.append(_issue("ICD10_INVALID", f"{code} is not a valid ICD-10-CM code."))
        elif not icd.is_leaf(code):
            problems.append(_issue("ICD10_NOT_BILLABLE", f"{code} is a category header; a more specific billable code is required."))
    return problems


def validate_x12_837(edi_text: str) -> List[str]:
    """Validates one X12 5010 837P interchange with pyx12. Returns error lines ([] means valid)."""
    import subprocess
    import sys
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "claim.837")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(edi_text)
        proc = subprocess.run([sys.executable, "-m", "pyx12.scripts.x12valid", path],
                              capture_output=True, text=True, timeout=120)
    lines = [ln for ln in (proc.stdout + proc.stderr).splitlines() if " ERROR " in ln or ln.strip().endswith("FAILED")]
    if proc.returncode != 0 and not lines:
        lines = [f"pyx12 exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}"]
    return lines
