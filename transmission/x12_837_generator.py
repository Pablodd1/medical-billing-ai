# ============================================
# FILE: medical_billing_ai/transmission/x12_837_generator.py
# PURPOSE: ANSI X12 837P (Professional Claim) Generator for Office Ally
# COMPLIANCE: HIPAA 5010 (005010X222A1) + OA 837P Companion Guide
# KEY RULES IMPLEMENTED:
#   - Envelope: ISA07='30', ISA08/GS03='330897513', ISA15 from config
#   - Persistent ISA13/GS06/ST02 control numbers (no hardcoded values)
#   - CLM01 = provider patient control number (reconciliation key),
#     NOT the insurance member ID
#   - SV1-7 = actual diagnosis pointers from the coding engine
#   - REF*F8 prior auth, Loop 2310A referring provider when present
#   - Loop 2320/2330/2430 secondary COB with AMT*D / SVD / CAS / DTP*573
#   - SE01 computed as the true ST..SE inclusive segment count
# ============================================

from datetime import datetime
from typing import Dict, Any, List, Optional

try:
    from config import OfficeAllyConfig
    from transmission.control_numbers import ControlNumberStore, get_default_store
except ImportError:
    from ..config import OfficeAllyConfig
    from ..transmission.control_numbers import ControlNumberStore, get_default_store


def _as_dict(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(obj)


_FILING_INDICATORS = {"MB", "MA", "MC", "CI", "BL", "HM", "CH", "VA", "WC", "OF"}


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

    def generate_837p(self, encounter: Dict[str, Any]) -> str:
        prov = _as_dict(encounter["provider"])
        pat = _as_dict(encounter["patient"])
        billing = _as_dict(encounter["billing"])
        payer = _as_dict(encounter["payer"])

        # Patient control number = the reconciliation key for 277CA/835.
        pcn = str(
            encounter.get("patient_control_number")
            or encounter.get("claim_id")
            or encounter.get("encounter_id")
            or pat.get("member_id")
        )

        now = datetime.now()
        isa_date = now.strftime("%y%m%d")
        gs_date = now.strftime("%Y%m%d")
        time_str = now.strftime("%H%M")

        isa13 = self.control_store.next("ISA13")
        gs06 = self.control_store.next("GS06")
        st02 = self.control_store.next("ST02_837")

        sub15 = self.config.claims_submitter_id.ljust(15)
        recv15 = self.config.receiver_id.ljust(15)

        head: List[str] = []
        head.append(
            f"ISA*00*          *00*          *ZZ*{sub15}"
            f"*{self.config.receiver_qualifier}*{recv15}"
            f"*{isa_date}*{time_str}*^*00501*{isa13:09d}*0*{self.usage_indicator}*:~"
        )
        head.append(
            f"GS*HC*{self.config.claims_submitter_id}*{self.config.receiver_id}"
            f"*{gs_date}*{time_str}*{gs06}*X*005010X222A1~"
        )

        body: List[str] = []
        body.append(f"ST*837*{st02:04d}*005010X222A1~")
        body.append(f"BHT*0019*00*{pcn}*{gs_date}*{time_str}*CH~")

        # 1000A Submitter
        contact_name = prov.get("contact_name", "BILLING DEPT")
        contact_phone = str(prov.get("phone", "8005551212")).replace("-", "")
        contact_email = prov.get("contact_email", prov.get("email", ""))
        body.append(f"NM1*41*2*{prov['organization_name']}*****46*{self.config.claims_submitter_id}~")
        per = f"PER*IC*{contact_name}*TE*{contact_phone}"
        if contact_email:
            per += f"*EM*{contact_email}"
        body.append(per + "~")

        # 1000B Receiver
        body.append(f"NM1*40*2*{self.config.receiver_name}*****46*{self.config.receiver_id}~")

        billing_npi = prov.get("billing_npi_type_2") or prov.get("npi")
        rendering_npi = prov.get("rendering_npi_type_1") or prov.get("npi")

        # 2000A Billing Provider
        body.append("HL*1**20*1~")
        body.append(f"PRV*BI*PXC*{prov.get('taxonomy_code', '207Q00000X')}~")
        body.append(f"NM1*85*2*{prov['organization_name']}*****XX*{billing_npi}~")
        body.append(f"N3*{prov.get('address', '1200 Brickell Ave')}~")
        body.append(f"N4*{prov.get('city', 'Miami')}*{prov.get('state', 'FL')}*{prov.get('zip', '33131')}~")
        body.append(f"REF*EI*{prov.get('tax_id', '123456789')}~")

        # 2000B Subscriber
        body.append("HL*2*1*22*0~")
        body.append(f"SBR*P*18*******{_filing_indicator(payer)}~")
        body.append(f"NM1*IL*1*{pat['last_name']}*{pat['first_name']}****MI*{pat['member_id']}~")
        body.append(f"N3*{pat.get('address', '450 Biscayne Blvd')}~")
        body.append(f"N4*{pat.get('city', 'Miami')}*{pat.get('state', 'FL')}*{pat.get('zip', '33132')}~")
        dob_cleaned = str(pat["dob"]).replace("-", "")
        body.append(f"DMG*D8*{dob_cleaned}*{pat.get('gender', 'U')}~")

        # 2010BB Payer
        body.append(f"NM1*PR*2*{payer['name']}*****PI*{payer['payer_id']}~")
        body.append(f"N3*{payer.get('address', 'PO BOX 1000')}~")
        body.append(f"N4*{payer.get('city', 'Jacksonville')}*{payer.get('state', 'FL')}*{payer.get('zip', '32202')}~")

        # 2300 Claim Information
        total_charge_str = f"{float(billing['total_charge']):.2f}"
        pos = billing.get("place_of_service", "11")
        body.append(f"CLM*{pcn}*{total_charge_str}***{pos}:B:1*Y*A*Y*Y~")

        # Box 14 -> DTP*454 (Initial Treatment Date), when known
        clinical_note = _as_dict(encounter.get("clinical_note", {}))
        onset = clinical_note.get("date_of_onset")
        if onset:
            body.append(f"DTP*454*D8*{str(onset).replace('-', '')}~")

        # Box 23 -> REF*F8 (Prior Authorization Number)
        prior_auth = billing.get("prior_authorization_number")
        if prior_auth:
            body.append(f"REF*F8*{prior_auth}~")

        # Diagnosis Codes (HI)
        dx_parts = []
        for i, dx in enumerate(billing.get("diagnosis_codes", [])):
            dx_d = _as_dict(dx)
            qual = "ABK" if i == 0 else "ABF"
            dx_parts.append(f"{qual}:{str(dx_d['code']).replace('.', '')}")
        if dx_parts:
            body.append(f"HI*{'*'.join(dx_parts)}~")

        # 2310A Referring Provider (Box 17) — emitted only when present
        referring = encounter.get("referring_provider")
        if referring:
            ref = _as_dict(referring)
            if ref.get("npi"):
                body.append(
                    f"NM1*DN*1*{ref.get('last_name', ref.get('name', ''))}"
                    f"*{ref.get('first_name', '')}****XX*{ref['npi']}~"
                )

        # 2310B Rendering Provider
        # Derive taxonomy code according to provider credentialing type if not explicitly set
        prov_type = str(prov.get("type", "")).upper()
        default_taxonomy = "363L00000X" if "APRN" in prov_type or "NP" in prov_type else "207Q00000X"
        taxonomy = prov.get("taxonomy_code") or default_taxonomy

        body.append(
            f"NM1*82*1*{prov.get('rendering_last_name', 'Provider')}"
            f"*{prov.get('rendering_first_name', 'Clinician')}****XX*{rendering_npi}~"
        )
        body.append(f"PRV*PE*PXC*{taxonomy}~")

        # 2320 / 2330 Secondary COB (Other Subscriber / Other Payer)
        sec_payer = pat.get("secondary_payer")
        sec_payer_dict: Optional[Dict[str, Any]] = _as_dict(sec_payer) if sec_payer else None
        if sec_payer_dict:
            sec_member_id = pat.get("secondary_member_id") or pat.get("member_id")
            body.append(f"SBR*S*18*******{_filing_indicator(sec_payer_dict)}~")
            # Loop 2320 required OI (Other Insurance) segment: claim filing indicator / benefits assignment
            body.append("OI***Y***Y~")
            # AMT*D: primary payer paid amount (claim level), when known
            primary_paid = encounter.get("primary_payer_paid_amount")
            if primary_paid is not None:
                body.append(f"AMT*D*{float(primary_paid):.2f}~")
            body.append(f"NM1*IL*1*{pat['last_name']}*{pat['first_name']}****MI*{sec_member_id}~")
            body.append(f"N3*{pat.get('address', '450 Biscayne Blvd')}~")
            body.append(f"N4*{pat.get('city', 'Miami')}*{pat.get('state', 'FL')}*{pat.get('zip', '33132')}~")
            body.append(
                f"NM1*PR*2*{sec_payer_dict.get('name', 'SECONDARY INSURANCE')}"
                f"*****PI*{sec_payer_dict.get('payer_id', 'SEC01')}~"
            )
            body.append(f"N3*{sec_payer_dict.get('address', 'PO BOX 2000')}~")
            body.append(
                f"N4*{sec_payer_dict.get('city', 'Jacksonville')}"
                f"*{sec_payer_dict.get('state', 'FL')}*{sec_payer_dict.get('zip', '32202')}~"
            )

        # 2400 Service Lines
        dos_cleaned = str(encounter.get("date_of_service", gs_date)).replace("-", "")
        for line_num, item in enumerate(billing.get("cpt_codes", []), start=1):
            item_d = _as_dict(item)
            body.append(f"LX*{line_num}~")
            cpt = item_d["code"]
            modifiers = item_d.get("modifiers", [])
            cpt_composite = f"HC:{cpt}" + ("".join(f":{m}" for m in modifiers) if modifiers else "")
            units = item_d.get("units", 1)
            line_charge_str = f"{(float(item_d['charge']) * units):.2f}"
            # SV1-7: real diagnosis pointers from the coding engine (was hardcoded '1')
            dx_ptrs = ":".join(str(p) for p in item_d.get("dx_pointers", [1]))
            body.append(f"SV1*{cpt_composite}*{line_charge_str}*UN*{units}***{dx_ptrs}~")
            body.append(f"DTP*472*D8*{dos_cleaned}~")

            # 2430 Line Adjudication (required on secondary/COB claims when known)
            line_adj = item_d.get("primary_payer_adjudication")
            if sec_payer_dict and line_adj:
                adj = _as_dict(line_adj)
                body.append(
                    f"SVD*{payer['payer_id']}*{float(adj.get('paid_amount', 0.0)):.2f}"
                    f"*{cpt_composite}**{units}~"
                )
                for cas in adj.get("adjustments", []):
                    cas_d = _as_dict(cas)
                    body.append(
                        f"CAS*{cas_d.get('group_code', 'CO')}*{cas_d.get('carc_code')}"
                        f"*{float(cas_d.get('amount', 0.0)):.2f}*{cas_d.get('units', 1)}~"
                    )
                if adj.get("paid_date"):
                    body.append(f"DTP*573*D8*{str(adj['paid_date']).replace('-', '')}~")

        # SE01 = segment count from ST through SE inclusive
        se_count = len(body) + 1
        body.append(f"SE*{se_count}*{st02:04d}~")

        tail = [f"GE*1*{gs06}~", f"IEA*1*{isa13:09d}~"]
        return "\n".join(head + body + tail)
