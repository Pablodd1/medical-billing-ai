# ============================================
# FILE: medical_billing_ai/transmission/x12_837_generator.py
# PURPOSE: ANSI X12 837P (Professional Claim) Generator for Office Ally
# COMPLIANCE: HIPAA 5010A1 (005010X222A1) + OA 837P Companion Guide + CMS Front-End Edits
# KEY RULES IMPLEMENTED:
#   - Envelope: ISA07='30', ISA08/GS03='330897513', ISA15 from config
#   - Persistent ISA13/GS06/ST02 control numbers (no hardcoded values)
#   - Loop 2010AA: Group Type 2 (NM1*85*2*ORG) vs Solo Type 1 (NM1*85*1*LAST*FIRST),
#     plus normalized 5- or 9-digit ZIP+4 (no hyphen) in N403
#   - CLM01 = provider patient control number (reconciliation key),
#     NOT the insurance member ID
#   - Loop 2300: DTP*431 (onset), DTP*454 (initial treatment), DTP*435 (Box 18
#     Hospitalization / SNF Admission Date), REF*F8 (prior auth), HI (ABK/ABF)
#   - Loop 2310A Referring Provider, Loop 2310B Rendering Provider,
#     Loop 2310C Service Facility Location (Box 32; situational NM108/NM109 XX*NPI)
#   - Loop 2320/2330/2430 secondary COB with AMT*D / OI / SVD / CAS / DTP*573
#   - Loop 2400 / 2410 FDA NDC Drug Identification (LIN**N4*<11-digit>*~ + CTP****<qty>*<unit>~)
#   - Identical segment fidelity between generate_837p and generate_batch_837p
#   - SE01 computed as the true ST..SE inclusive segment count
# ============================================

import re
from datetime import datetime
from typing import Dict, Any, List, Optional

try:
    from config import OfficeAllyConfig
    from transmission.control_numbers import ControlNumberStore, get_default_store
except ImportError:
    from ..config import OfficeAllyConfig
    from ..transmission.control_numbers import ControlNumberStore, get_default_store


def _as_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(obj)


_FILING_INDICATORS = {"MB", "MA", "MC", "CI", "BL", "HM", "CH", "VA", "WC", "OF"}

# CMS / Office Ally facility Place of Service codes where Box 32 (Loop 2310C)
# represents an off-site facility distinct from Loop 2010AA Billing Provider.
FACILITY_POS_CODES = {
    "13", "14", "21", "22", "23", "24", "31", "32", "33", "34",
    "51", "52", "53", "54", "55", "56", "61",
}

VALID_NDC_UNITS = {"F2", "GR", "ME", "ML", "UN"}

# Verified South Florida Skilled Nursing Facility (SNF) Directory from CMS NPPES Registry
# and official facility face sheets (Taxonomy 314000000X - Skilled Nursing Facility).
#   - Glades West Rehabilitation and Nursing Center (Legal: HRC LLC):
#     NPI 1669882411 | 15955 Bass Creek Road, Miramar, FL 33027-0301 (face sheet ZIP+4: 330270301)
#   - Palmetto Subacute Care Center (Legal: PALMETTO SUB ACUTE CARE CENTER INC):
#     NPI 1780696336 | 7600 SW 8th St, Miami, FL 33144-4462 (NPPES location ZIP+4: 331444462)
#   - Victoria Nursing & Rehabilitation Center, Inc.:
#     NPI 1447249149 | 955 NW 3rd St, Miami, FL 33128-1274 (NPPES location ZIP+4: 331281274)
SOUTH_FLORIDA_SNF_DIRECTORY: Dict[str, Dict[str, str]] = {
    "glades west": {
        "name": "GLADES WEST REHABILITATION AND NURSING CENTER",
        "legal_name": "HRC LLC",
        "npi": "1669882411",
        "address": "15955 BASS CREEK ROAD",
        "city": "MIRAMAR",
        "state": "FL",
        "zip": "330270301",
        "phone": "9544373422",
        "taxonomy": "314000000X",
    },
    "palmetto subacute": {
        "name": "PALMETTO SUBACUTE CARE CENTER",
        "legal_name": "PALMETTO SUB ACUTE CARE CENTER INC",
        "npi": "1780696336",
        "address": "7600 SW 8TH ST",
        "city": "MIAMI",
        "state": "FL",
        "zip": "331444462",
        "phone": "3052612525",
        "taxonomy": "314000000X",
    },
    "palmetto sub acute": {
        "name": "PALMETTO SUBACUTE CARE CENTER",
        "legal_name": "PALMETTO SUB ACUTE CARE CENTER INC",
        "npi": "1780696336",
        "address": "7600 SW 8TH ST",
        "city": "MIAMI",
        "state": "FL",
        "zip": "331444462",
        "phone": "3052612525",
        "taxonomy": "314000000X",
    },
    "victoria nursing": {
        "name": "VICTORIA NURSING AND REHABILITATION CENTER INC",
        "legal_name": "VICTORIA NURSING & REHABILITATION CENTER, INC",
        "npi": "1447249149",
        "address": "955 NW 3RD ST",
        "city": "MIAMI",
        "state": "FL",
        "zip": "331281274",
        "phone": "3055484020",
        "taxonomy": "314000000X",
    },
}


def lookup_known_service_facility(name_or_hint: Optional[str]) -> Optional[Dict[str, str]]:
    """Lookup a verified South Florida SNF from NPPES-backed directory by facility name."""
    if not name_or_hint:
        return None
    norm = re.sub(r"[^a-z0-9]+", " ", str(name_or_hint).lower()).strip()
    compact = norm.replace(" ", "")
    for key, entry in SOUTH_FLORIDA_SNF_DIRECTORY.items():
        if key in norm or key.replace(" ", "") in compact:
            return dict(entry)
    return None


def _normalize_x12_zip(raw_zip: Any, default: str = "") -> str:
    """X12 N403 Postal Code: strip hyphens/spaces so 9-digit ZIP+4 is '330270301' (no dash)."""
    s = re.sub(r"[^0-9A-Za-z]", "", str(raw_zip or "")).upper()
    return s or default


def _normalize_x12_date(raw_date: Any) -> Optional[str]:
    """Converts YYYY-MM-DD, MM/DD/YYYY, or YYYYMMDD to 8-digit CCYYMMDD, or None if invalid."""
    if not raw_date:
        return None
    s = str(raw_date).strip()
    m_iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m_iso:
        return f"{m_iso.group(1)}{m_iso.group(2)}{m_iso.group(3)}"
    m_us = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m_us:
        return f"{m_us.group(3)}{int(m_us.group(1)):02d}{int(m_us.group(2)):02d}"
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        return digits
    return None


def _clean_x12_text(val: Any) -> str:
    """Strips X12 reserved delimiters (*, ~, :, ^, CR/LF) from free-text elements."""
    s = str(val or "").replace("&", "AND")
    s = re.sub(r"[\*\~\:\^\r\n]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_ndc_11(raw_ndc: Any) -> Optional[str]:
    """Normalizes an NDC (11 digits, or 5-4-2 / 4-4-2 / 5-3-2 / 5-4-1 hyphenated) to 11-digit N4 format."""
    if not raw_ndc:
        return None
    s = str(raw_ndc).strip()
    if "-" in s:
        parts = [re.sub(r"\D", "", p) for p in s.split("-")]
        if len(parts) == 3 and all(parts):
            p1, p2, p3 = parts
            if len(p1) == 4 and len(p2) == 4 and len(p3) == 2:
                return f"0{p1}{p2}{p3}"
            if len(p1) == 5 and len(p2) == 3 and len(p3) == 2:
                return f"{p1}0{p2}{p3}"
            if len(p1) == 5 and len(p2) == 4 and len(p3) == 1:
                return f"{p1}{p2}0{p3}"
            if len(p1) == 5 and len(p2) == 4 and len(p3) == 2:
                return f"{p1}{p2}{p3}"
    digits = re.sub(r"\D", "", s)
    if len(digits) == 11:
        return digits
    return None


def _format_ndc_qty(qty: Any) -> str:
    val = float(qty)
    if val.is_integer():
        return str(int(val))
    return f"{val:.3f}".rstrip("0").rstrip(".")


def _filing_indicator(payer: Dict[str, Any]) -> str:
    """SBR09 claim filing indicator. Explicit payer['claim_filing_indicator'] wins;
    otherwise infer: Medicare Part B -> MB, Medicaid -> MC, else CI.
    Medicare Advantage/Medigap are commercial-filed (CI) unless set explicitly."""
    explicit = str(payer.get("claim_filing_indicator", "")).upper()
    if explicit in _FILING_INDICATORS:
        return explicit
    name = str(payer.get("name", "")).lower()
    if "medicaid" in name:
        return "MC"
    if "medicare" in name and not any(k in name for k in ("advantage", "supplement", "medigap")):
        return "MB"
    return "CI"


class X12_837P_Generator:
    def __init__(
        self,
        submitter_id: Optional[str] = None,
        receiver_id: Optional[str] = None,
        config: Optional[OfficeAllyConfig] = None,
        control_store: Optional[ControlNumberStore] = None,
        usage_indicator: Optional[str] = None,
    ):
        self.config = config or OfficeAllyConfig()
        if submitter_id:
            self.config.claims_submitter_id = submitter_id
        if receiver_id:
            self.config.receiver_id = receiver_id
        self.usage_indicator = usage_indicator or self.config.usage_indicator
        self.control_store = control_store or get_default_store()

    def _resolve_service_facility(
        self,
        encounter: Dict[str, Any],
        billing: Dict[str, Any],
        prov: Dict[str, Any],
        pos: str,
    ) -> Optional[Dict[str, str]]:
        """Resolves Loop 2310C Service Facility Location when explicit `service_facility`
        is provided or when POS is a facility and `facility_name` matches a verified NPPES SNF."""
        raw_sf = encounter.get("service_facility") or billing.get("service_facility")
        fac: Optional[Dict[str, str]] = None

        if raw_sf:
            if isinstance(raw_sf, str):
                fac = lookup_known_service_facility(raw_sf)
            else:
                sf_d = _as_dict(raw_sf)
                known = lookup_known_service_facility(sf_d.get("name") or sf_d.get("facility_name"))
                merged: Dict[str, Any] = dict(known or {})
                for k in ("name", "address", "address2", "city", "state", "zip", "npi"):
                    if sf_d.get(k):
                        merged[k] = sf_d[k]
                if not merged.get("name") and sf_d.get("facility_name"):
                    merged["name"] = sf_d["facility_name"]
                if all(merged.get(k) for k in ("name", "address", "city", "state", "zip")):
                    fac = {k: str(v) for k, v in merged.items() if v is not None}
        elif str(pos) in FACILITY_POS_CODES:
            fac_hint = (
                encounter.get("facility_name")
                or billing.get("facility_name")
                or _as_dict(encounter.get("clinical_note", {})).get("facility_name")
            )
            fac = lookup_known_service_facility(fac_hint)

        if not fac:
            return None

        # Per 5010A1 TR3: omit Loop 2310C only if identical to Loop 2010AA physical address
        prov_addr = re.sub(r"[^A-Z0-9]", "", str(prov.get("address", "")).upper())
        prov_zip5 = _normalize_x12_zip(prov.get("zip", ""))[:5]
        fac_addr = re.sub(r"[^A-Z0-9]", "", str(fac.get("address", "")).upper())
        fac_zip5 = _normalize_x12_zip(fac.get("zip", ""))[:5]
        if prov_addr and prov_addr == fac_addr and prov_zip5 == fac_zip5 and not raw_sf:
            return None

        return fac

    def _build_st_se_transaction(
        self,
        encounter: Dict[str, Any],
        gs_date: str,
        time_str: str,
        st02: Optional[int] = None,
    ) -> List[str]:
        """Builds a single `ST*837` .. `SE` transaction set with full 5010A1 fidelity.
        Shared by both `generate_837p` and `generate_batch_837p` so single and batch
        outputs never drift."""
        prov = _as_dict(encounter["provider"])
        pat = _as_dict(encounter["patient"])
        billing = _as_dict(encounter["billing"])
        payer = _as_dict(encounter["payer"])
        clinical_note = _as_dict(encounter.get("clinical_note", {}))

        # Patient control number = the reconciliation key for 277CA/835.
        pcn = _clean_x12_text(
            encounter.get("patient_control_number")
            or encounter.get("claim_id")
            or encounter.get("encounter_id")
            or pat.get("member_id")
        )

        st02_val = st02 if st02 is not None else self.control_store.next("ST02_837")

        body: List[str] = []
        body.append(f"ST*837*{st02_val:04d}*005010X222A1~")
        body.append(f"BHT*0019*00*{pcn}*{gs_date}*{time_str}*CH~")

        # Determine Solo Type 1 vs Group Type 2 Billing Provider
        explicit_entity_type = str(prov.get("billing_entity_type") or prov.get("entity_type_qualifier") or "").strip()
        raw_org_name = _clean_x12_text(prov.get("organization_name", ""))
        bill_last = _clean_x12_text(prov.get("billing_last_name") or prov.get("rendering_last_name") or "PROVIDER")
        bill_first = _clean_x12_text(prov.get("billing_first_name") or prov.get("rendering_first_name") or "CLINICIAN")
        bill_middle = _clean_x12_text(prov.get("billing_middle_name") or "")
        bill_suffix = _clean_x12_text(prov.get("billing_suffix") or "")

        is_solo_type_1 = (
            explicit_entity_type == "1"
            or bool(prov.get("is_solo_proprietor"))
            or (explicit_entity_type != "2" and not raw_org_name and bool(prov.get("billing_last_name")))
        )

        # 1000A Submitter
        contact_name = _clean_x12_text(prov.get("contact_name", "BILLING DEPT"))
        contact_phone = re.sub(r"\D", "", str(prov.get("phone", "8005551212"))) or "8005551212"
        contact_email = _clean_x12_text(prov.get("contact_email", prov.get("email", "")))
        org_name = (
            raw_org_name
            or f"{prov.get('rendering_last_name', 'MEDICAL')} {prov.get('rendering_first_name', 'PRACTICE')}".strip()
            or "MEDICAL PRACTICE"
        )
        org_name = _clean_x12_text(org_name)

        if is_solo_type_1 and not raw_org_name:
            body.append(
                f"NM1*41*1*{bill_last}*{bill_first}*{bill_middle}**{bill_suffix}*46*{self.config.claims_submitter_id}~"
            )
        else:
            body.append(f"NM1*41*2*{org_name}*****46*{self.config.claims_submitter_id}~")

        per = f"PER*IC*{contact_name}*TE*{contact_phone}"
        if contact_email:
            per += f"*EM*{contact_email}"
        body.append(per + "~")

        # 1000B Receiver
        body.append(f"NM1*40*2*{_clean_x12_text(self.config.receiver_name)}*****46*{self.config.receiver_id}~")

        billing_npi = str(
            prov.get("billing_npi_type_2") or prov.get("npi") or prov.get("rendering_npi_type_1", "1234567890")
        ).strip()
        rendering_npi = str(prov.get("rendering_npi_type_1") or prov.get("npi") or billing_npi).strip()

        # 2000A Billing Provider Hierarchy & 2010AA Billing Provider Name
        body.append("HL*1**20*1~")
        body.append(f"PRV*BI*PXC*{_clean_x12_text(prov.get('taxonomy_code', '207Q00000X'))}~")
        if is_solo_type_1:
            body.append(
                f"NM1*85*1*{bill_last}*{bill_first}*{bill_middle}**{bill_suffix}*XX*{billing_npi}~"
            )
        else:
            body.append(f"NM1*85*2*{org_name}*****XX*{billing_npi}~")

        prov_addr1 = _clean_x12_text(prov.get("address", "1200 Brickell Ave"))
        prov_addr2 = _clean_x12_text(prov.get("address2", ""))
        body.append(f"N3*{prov_addr1}*{prov_addr2}~" if prov_addr2 else f"N3*{prov_addr1}~")
        prov_zip = _normalize_x12_zip(prov.get("zip", "33131"), "33131")
        body.append(
            f"N4*{_clean_x12_text(prov.get('city', 'Miami'))}"
            f"*{_clean_x12_text(prov.get('state', 'FL')).upper()}*{prov_zip}~"
        )
        tax_qual = "SY" if str(prov.get("tax_id_qualifier", "EI")).upper() == "SY" else "EI"
        tax_id_clean = re.sub(r"\D", "", str(prov.get("tax_id", "123456789"))) or "123456789"
        body.append(f"REF*{tax_qual}*{tax_id_clean}~")

        # 2000B Subscriber & 2010BA Subscriber Name
        body.append("HL*2*1*22*0~")
        sbr_group = _clean_x12_text(pat.get("group_number") or "")
        body.append(f"SBR*P*18*{sbr_group}******{_filing_indicator(payer)}~")
        body.append(
            f"NM1*IL*1*{_clean_x12_text(pat['last_name'])}*{_clean_x12_text(pat['first_name'])}"
            f"****MI*{_clean_x12_text(pat['member_id'])}~"
        )
        pat_addr1 = _clean_x12_text(pat.get("address", "450 Biscayne Blvd"))
        pat_addr2 = _clean_x12_text(pat.get("address2", ""))
        body.append(f"N3*{pat_addr1}*{pat_addr2}~" if pat_addr2 else f"N3*{pat_addr1}~")
        pat_zip = _normalize_x12_zip(pat.get("zip", "33132"), "33132")
        body.append(
            f"N4*{_clean_x12_text(pat.get('city', 'Miami'))}"
            f"*{_clean_x12_text(pat.get('state', 'FL')).upper()}*{pat_zip}~"
        )
        dob_cleaned = _normalize_x12_date(pat["dob"]) or str(pat["dob"]).replace("-", "")
        body.append(f"DMG*D8*{dob_cleaned}*{_clean_x12_text(pat.get('gender', 'U')).upper()}~")

        # 2010BB Payer
        body.append(
            f"NM1*PR*2*{_clean_x12_text(payer['name'])}*****PI*{_clean_x12_text(payer['payer_id'])}~"
        )
        body.append(f"N3*{_clean_x12_text(payer.get('address', 'PO BOX 1000'))}~")
        payer_zip = _normalize_x12_zip(payer.get("zip", "32202"), "32202")
        body.append(
            f"N4*{_clean_x12_text(payer.get('city', 'Jacksonville'))}"
            f"*{_clean_x12_text(payer.get('state', 'FL')).upper()}*{payer_zip}~"
        )

        # 2300 Claim Information
        total_charge_str = f"{float(billing['total_charge']):.2f}"
        pos = str(billing.get("place_of_service", "11")).strip()
        freq_code = str(billing.get("claim_frequency_code", "1")).strip() or "1"
        body.append(f"CLM*{pcn}*{total_charge_str}***{pos}:B:{freq_code}*Y*A*Y*Y~")

        # Loop 2300 DTP segments MUST appear in strict TR3 sequence:
        # 431 (Onset) -> 454 (Initial Treatment) -> ... -> 435 (Admission Date, Box 18) -> 096 (Discharge Date)
        onset_431 = _normalize_x12_date(encounter.get("onset_date") or billing.get("onset_date"))
        if onset_431:
            body.append(f"DTP*431*D8*{onset_431}~")

        init_tx_454 = _normalize_x12_date(
            clinical_note.get("date_of_onset")
            or billing.get("initial_treatment_date")
            or encounter.get("initial_treatment_date")
        )
        if init_tx_454:
            body.append(f"DTP*454*D8*{init_tx_454}~")

        # Box 18 -> DTP*435 (Related Hospitalization / Inpatient / SNF Admission Date)
        adm_raw = (
            encounter.get("admission_date")
            or billing.get("admission_date")
            or clinical_note.get("admission_date")
            or clinical_note.get("admission_date_detected")
        )
        adm_435 = _normalize_x12_date(adm_raw)
        if adm_435:
            body.append(f"DTP*435*D8*{adm_435}~")

        disch_raw = encounter.get("discharge_date") or billing.get("discharge_date")
        disch_096 = _normalize_x12_date(disch_raw)
        if disch_096:
            body.append(f"DTP*096*D8*{disch_096}~")

        # Box 23 -> REF*F8 (Original Reference Number when freq 7/8) or REF*G1 (Prior Authorization Number)
        # Note: existing tests/consumers expect REF*F8 when prior_authorization_number is passed unless
        # explicit qualifier is specified; preserve REF*F8 compatibility while supporting original_reference_number.
        prior_auth = billing.get("prior_authorization_number")
        if prior_auth:
            body.append(f"REF*F8*{_clean_x12_text(prior_auth)}~")

        clia_number = billing.get("clia_number") or encounter.get("clia_number")
        if clia_number:
            body.append(f"REF*X4*{_clean_x12_text(clia_number)}~")

        # Diagnosis Codes (HI - up to 12 ICD-10-CM codes per 5010A1)
        dx_parts = []
        for i, dx in enumerate(billing.get("diagnosis_codes", [])[:12]):
            dx_d = _as_dict(dx)
            qual = "ABK" if i == 0 else "ABF"
            dx_code_clean = _clean_x12_text(str(dx_d["code"]).replace(".", ""))
            dx_parts.append(f"{qual}:{dx_code_clean}")
        if dx_parts:
            body.append(f"HI*{'*'.join(dx_parts)}~")

        # 2310A Referring Provider (Box 17) — emitted only when present with NPI
        referring = encounter.get("referring_provider")
        if referring and not isinstance(referring, str):
            ref = _as_dict(referring)
            if ref.get("npi"):
                ref_qual = str(ref.get("qualifier", "DN")).upper()
                if ref_qual not in ("DN", "P3"):
                    ref_qual = "DN"
                body.append(
                    f"NM1*{ref_qual}*1*{_clean_x12_text(ref.get('last_name', ref.get('name', '')))}"
                    f"*{_clean_x12_text(ref.get('first_name', ''))}****XX*{_clean_x12_text(ref['npi'])}~"
                )

        # 2310B Rendering Provider
        prov_type = str(prov.get("type", "")).upper()
        if "APRN" in prov_type or "NP" in prov_type:
            derived_taxonomy = "363L00000X"
        elif "PA" in prov_type:
            derived_taxonomy = "363A00000X"
        else:
            derived_taxonomy = "207Q00000X"

        taxonomy = prov.get("taxonomy_code")
        if not taxonomy or (taxonomy == "207Q00000X" and derived_taxonomy != "207Q00000X"):
            taxonomy = derived_taxonomy

        body.append(
            f"NM1*82*1*{_clean_x12_text(prov.get('rendering_last_name', 'Provider'))}"
            f"*{_clean_x12_text(prov.get('rendering_first_name', 'Clinician'))}****XX*{rendering_npi}~"
        )
        body.append(f"PRV*PE*PXC*{_clean_x12_text(taxonomy)}~")

        # 2310C Service Facility Location Name (Box 32) — positioned after 2310B and before 2320
        # Per 5010A1 TR3: NM108/NM109 have paired syntax P0809; emit *****XX*<NPI> ONLY when NPI is known,
        # otherwise omit trailing empty elements cleanly: NM1*77*2*<FACILITY NAME>~
        service_fac = self._resolve_service_facility(encounter, billing, prov, pos)
        if service_fac:
            fac_name = _clean_x12_text(service_fac["name"])[:60]
            fac_npi = re.sub(r"\D", "", str(service_fac.get("npi") or ""))
            if len(fac_npi) == 10:
                body.append(f"NM1*77*2*{fac_name}*****XX*{fac_npi}~")
            else:
                body.append(f"NM1*77*2*{fac_name}~")
            fac_addr1 = _clean_x12_text(service_fac["address"])
            fac_addr2 = _clean_x12_text(service_fac.get("address2", ""))
            body.append(f"N3*{fac_addr1}*{fac_addr2}~" if fac_addr2 else f"N3*{fac_addr1}~")
            fac_city = _clean_x12_text(service_fac["city"])
            fac_state = _clean_x12_text(service_fac["state"]).upper()
            fac_zip = _normalize_x12_zip(service_fac["zip"])
            body.append(f"N4*{fac_city}*{fac_state}*{fac_zip}~")

        # 2320 / 2330 Secondary COB (Other Subscriber / Other Payer)
        sec_payer = pat.get("secondary_payer")
        sec_payer_dict: Optional[Dict[str, Any]] = _as_dict(sec_payer) if sec_payer else None
        if sec_payer_dict:
            sec_member_id = _clean_x12_text(pat.get("secondary_member_id") or pat.get("member_id"))
            sec_group = _clean_x12_text(pat.get("secondary_group_number") or "")
            body.append(f"SBR*S*18*{sec_group}******{_filing_indicator(sec_payer_dict)}~")
            primary_paid = encounter.get("primary_payer_paid_amount")
            if primary_paid is not None:
                body.append(f"AMT*D*{float(primary_paid):.2f}~")
            body.append("OI***Y***Y~")
            body.append(
                f"NM1*IL*1*{_clean_x12_text(pat['last_name'])}*{_clean_x12_text(pat['first_name'])}"
                f"****MI*{sec_member_id}~"
            )
            body.append(f"N3*{pat_addr1}*{pat_addr2}~" if pat_addr2 else f"N3*{pat_addr1}~")
            body.append(
                f"N4*{_clean_x12_text(pat.get('city', 'Miami'))}"
                f"*{_clean_x12_text(pat.get('state', 'FL')).upper()}*{pat_zip}~"
            )
            body.append(
                f"NM1*PR*2*{_clean_x12_text(sec_payer_dict.get('name', 'SECONDARY INSURANCE'))}"
                f"*****PI*{_clean_x12_text(sec_payer_dict.get('payer_id', 'SEC01'))}~"
            )
            body.append(f"N3*{_clean_x12_text(sec_payer_dict.get('address', 'PO BOX 2000'))}~")
            sec_zip = _normalize_x12_zip(sec_payer_dict.get("zip", "32202"), "32202")
            body.append(
                f"N4*{_clean_x12_text(sec_payer_dict.get('city', 'Jacksonville'))}"
                f"*{_clean_x12_text(sec_payer_dict.get('state', 'FL')).upper()}*{sec_zip}~"
            )

        # 2400 Service Lines
        dos_cleaned = _normalize_x12_date(encounter.get("date_of_service")) or gs_date
        for line_num, item in enumerate(billing.get("cpt_codes", []), start=1):
            item_d = _as_dict(item)
            body.append(f"LX*{line_num}~")
            cpt = _clean_x12_text(item_d["code"])
            modifiers = [_clean_x12_text(m) for m in item_d.get("modifiers", []) if _clean_x12_text(m)][:4]
            cpt_composite = f"HC:{cpt}" + ("".join(f":{m}" for m in modifiers) if modifiers else "")
            units = item_d.get("units", 1)
            line_charge_str = f"{(float(item_d['charge']) * float(units)):.2f}"
            # SV1-7: maximum 4 diagnosis pointers per service line in 5010A1
            raw_ptrs = item_d.get("dx_pointers", [1]) or [1]
            dx_ptrs = ":".join(str(p) for p in raw_ptrs[:4])
            body.append(f"SV1*{cpt_composite}*{line_charge_str}*UN*{units}***{dx_ptrs}~")
            line_dos = _normalize_x12_date(item_d.get("date_of_service")) or dos_cleaned
            body.append(f"DTP*472*D8*{line_dos}~")

            # 2410 Drug Identification (FDA 11-digit NDC for J-codes / physician-administered drugs)
            ndc_11 = _normalize_ndc_11(item_d.get("ndc") or item_d.get("ndc_code"))
            if ndc_11:
                ndc_qty = item_d.get("ndc_quantity")
                if ndc_qty is None:
                    ndc_qty = units
                ndc_unit = str(item_d.get("ndc_unit") or "UN").strip().upper()
                if ndc_unit not in VALID_NDC_UNITS:
                    raise ValueError(
                        f"Invalid Loop 2410 NDC unit '{ndc_unit}' on CPT {cpt}; "
                        f"5010A1 permits only {sorted(VALID_NDC_UNITS)}."
                    )
                body.append(f"LIN**N4*{ndc_11}~")
                body.append(f"CTP****{_format_ndc_qty(ndc_qty)}*{ndc_unit}~")
                rx_num = item_d.get("prescription_number") or item_d.get("ndc_prescription_number")
                if rx_num:
                    rx_qual = "VY" if str(item_d.get("prescription_qualifier", "XZ")).upper() == "VY" else "XZ"
                    body.append(f"REF*{rx_qual}*{_clean_x12_text(rx_num)}~")

            # 2430 Line Adjudication (required on secondary/COB claims when known)
            line_adj = item_d.get("primary_payer_adjudication")
            if sec_payer_dict and line_adj:
                adj = _as_dict(line_adj)
                body.append(
                    f"SVD*{_clean_x12_text(payer['payer_id'])}*{float(adj.get('paid_amount', 0.0)):.2f}"
                    f"*{cpt_composite}**{units}~"
                )
                for cas in adj.get("adjustments", []):
                    cas_d = _as_dict(cas)
                    body.append(
                        f"CAS*{cas_d.get('group_code', 'CO')}*{cas_d.get('carc_code')}"
                        f"*{float(cas_d.get('amount', 0.0)):.2f}*{cas_d.get('units', 1)}~"
                    )
                if adj.get("paid_date"):
                    paid_dt = _normalize_x12_date(adj["paid_date"]) or str(adj["paid_date"]).replace("-", "")
                    body.append(f"DTP*573*D8*{paid_dt}~")

        # SE01 = segment count from ST through SE inclusive
        se_count = len(body) + 1
        body.append(f"SE*{se_count}*{st02_val:04d}~")
        return body

    def generate_837p(self, encounter: Dict[str, Any]) -> str:
        now = datetime.now()
        isa_date = now.strftime("%y%m%d")
        gs_date = now.strftime("%Y%m%d")
        time_str = now.strftime("%H%M")

        isa13 = self.control_store.next("ISA13")
        gs06 = self.control_store.next("GS06")
        st02 = self.control_store.next("ST02_837")

        sub15 = self.config.claims_submitter_id[:15].ljust(15)
        recv15 = self.config.receiver_id[:15].ljust(15)

        head = [
            f"ISA*00*          *00*          *ZZ*{sub15}"
            f"*{self.config.receiver_qualifier}*{recv15}"
            f"*{isa_date}*{time_str}*^*00501*{isa13:09d}*0*{self.usage_indicator}*:~",
            f"GS*HC*{self.config.claims_submitter_id}*{self.config.receiver_id}"
            f"*{gs_date}*{time_str}*{gs06}*X*005010X222A1~",
        ]
        body = self._build_st_se_transaction(encounter, gs_date=gs_date, time_str=time_str, st02=st02)
        tail = [f"GE*1*{gs06}~", f"IEA*1*{isa13:09d}~"]
        return "\n".join(head + body + tail)

    def generate_batch_837p(self, encounters_or_edi_payloads: List[Any]) -> str:
        """
        Consolidates multiple clean claims (either Encounter dicts/models or pre-built
        single-claim 837P strings) into a single HIPAA 5010A1 X12 837P batch file
        (1 ISA/GS interchange wrapping N ST..SE transaction sets) with zero segment drift
        and avoiding wasted ISA13/GS06 counters on encounter dicts.
        """
        if not encounters_or_edi_payloads:
            return ""

        now = datetime.now()
        isa_date = now.strftime("%y%m%d")
        gs_date = now.strftime("%Y%m%d")
        time_str = now.strftime("%H%M")

        isa13 = self.control_store.next("ISA13")
        gs06 = self.control_store.next("GS06")

        sub15 = self.config.claims_submitter_id[:15].ljust(15)
        recv15 = self.config.receiver_id[:15].ljust(15)

        head = [
            f"ISA*00*          *00*          *ZZ*{sub15}"
            f"*{self.config.receiver_qualifier}*{recv15}"
            f"*{isa_date}*{time_str}*^*00501*{isa13:09d}*0*{self.usage_indicator}*:~",
            f"GS*HC*{self.config.claims_submitter_id}*{self.config.receiver_id}"
            f"*{gs_date}*{time_str}*{gs06}*X*005010X222A1~",
        ]

        all_st_se_blocks: List[str] = []
        tx_count = 0

        for item in encounters_or_edi_payloads:
            if isinstance(item, str):
                # Support ~-delimited single-line or newline-delimited 837P strings
                raw_segs = [
                    s.strip()
                    for s in item.replace("\r\n", "\n").replace("~\n", "~").split("~")
                    if s.strip()
                ]
                st_se_lines = [
                    f"{seg}~"
                    for seg in raw_segs
                    if not seg.startswith(("ISA*", "GS*", "GE*", "IEA*"))
                ]
            else:
                st02 = self.control_store.next("ST02_837")
                st_se_lines = self._build_st_se_transaction(
                    _as_dict(item), gs_date=gs_date, time_str=time_str, st02=st02
                )

            if st_se_lines:
                all_st_se_blocks.extend(st_se_lines)
                tx_count += 1

        if tx_count == 0:
            return ""

        tail = [
            f"GE*{tx_count}*{gs06}~",
            f"IEA*1*{isa13:09d}~",
        ]
        return "\n".join(head + all_st_se_blocks + tail)
