# ============================================
# FILE: medical_billing_ai/transmission/x12_837_generator.py
# PURPOSE: ANSI X12 837P (Professional Claim) Generator for Office Ally Transmission
# ============================================

from datetime import datetime
from typing import Dict, Any

class X12_837P_Generator:
    """
    Generates standard HIPAA 5010 ANSI X12 837P electronic claim files
    formatted specifically for Office Ally clearinghouse SFTP ingestion.
    """

    def __init__(self, submitter_id: str = "MAGNOLIA_HEALTH", receiver_id: str = "OFFICEALLY"):
        self.submitter_id = submitter_id.ljust(15)
        self.receiver_id = receiver_id.ljust(15)

    def generate_837p(self, encounter: Dict[str, Any], control_number: int = 10001) -> str:
        prov = encounter["provider"]
        pat = encounter["patient"]
        billing = encounter["billing"]
        payer = encounter["payer"]

        now = datetime.now()
        date_str = now.strftime("%y%m%d")
        time_str = now.strftime("%H%M")
        full_date = now.strftime("%Y%m%d")
        ctrl_str = f"{control_number:09d}"

        segments = []

        # ISA
        segments.append(f"ISA*00*          *00*          *ZZ*{self.submitter_id}*ZZ*{self.receiver_id}*{date_str}*{time_str}*^*00501*{ctrl_str}*0*T*:~")
        # GS
        segments.append(f"GS*HC*{self.submitter_id.strip()}*{self.receiver_id.strip()}*{full_date}*{time_str}*1*X*005010X222A1~")
        # ST
        segments.append(f"ST*837*0001*005010X222A1~")
        # BHT
        segments.append(f"BHT*0019*00*{ctrl_str}*{full_date}*{time_str}*CH~")
        # 1000A Submitter
        segments.append(f"NM1*41*2*{prov['organization_name']}*****46*{self.submitter_id.strip()}~")
        segments.append(f"PER*IC*BILLING DEPT*TE*8005551212*EM*billing@clinic.com~")
        # 1000B Receiver
        segments.append(f"NM1*40*2*OFFICE ALLY*****46*{self.receiver_id.strip()}~")

        billing_npi = prov.get('billing_npi_type_2') or prov.get('npi')
        rendering_npi = prov.get('rendering_npi_type_1') or prov.get('npi')

        # 2000A Billing Provider
        segments.append("HL*1**20*1~")
        segments.append(f"PRV*BI*PXC*{prov.get('taxonomy_code', '207Q00000X')}~")
        segments.append(f"NM1*85*2*{prov['organization_name']}*****XX*{billing_npi}~")
        segments.append(f"N3*{prov.get('address', '1200 Brickell Ave')}~")
        segments.append(f"N4*{prov.get('city', 'Miami')}*{prov.get('state', 'FL')}*{prov.get('zip', '33131')}~")
        segments.append(f"REF*EI*{prov.get('tax_id', '123456789')}~")

        # 2000B Subscriber
        segments.append("HL*2*1*22*0~")
        segments.append("SBR*P*18*******CI~")
        segments.append(f"NM1*IL*1*{pat['last_name']}*{pat['first_name']}****MI*{pat['member_id']}~")
        segments.append(f"N3*{pat.get('address', '450 Biscayne Blvd')}~")
        segments.append(f"N4*{pat.get('city', 'Miami')}*{pat.get('state', 'FL')}*{pat.get('zip', '33132')}~")
        dob_cleaned = str(pat['dob']).replace("-", "")
        segments.append(f"DMG*D8*{dob_cleaned}*{pat.get('gender', 'U')}~")

        # 2010BB Payer
        segments.append(f"NM1*PR*2*{payer['name']}*****PI*{payer['payer_id']}~")
        segments.append(f"N3*{payer.get('address', 'PO BOX 1000')}~")
        segments.append(f"N4*{payer.get('city', 'Jacksonville')}*{payer.get('state', 'FL')}*{payer.get('zip', '32202')}~")

        # 2300 Claim
        total_charge_str = f"{billing['total_charge']:.2f}"
        pos = billing.get("place_of_service", "11")
        segments.append(f"CLM*{pat['member_id']}*{total_charge_str}***{pos}:B:1*Y*A*Y*Y~")

        # Diagnosis Codes (HI)
        dx_parts = []
        for i, dx in enumerate(billing.get("diagnosis_codes", [])):
            qual = "BK" if i == 0 else "BF"
            code_clean = (dx.get("code") if isinstance(dx, dict) else dx.code).replace(".", "")
            dx_parts.append(f"{qual}:{code_clean}")
        if dx_parts:
            segments.append(f"HI*{'*'.join(dx_parts)}~")

        # 2310B Rendering Provider
        segments.append(f"NM1*82*1*{prov.get('rendering_last_name', 'Provider')}*{prov.get('rendering_first_name', 'Clinician')}****XX*{rendering_npi}~")
        segments.append(f"PRV*PE*PXC*{prov.get('taxonomy_code', '207Q00000X')}~")

        # 2320 / 2330 Other Subscriber & Other Payer (Secondary COB)
        sec_payer = pat.get("secondary_payer")
        if sec_payer:
            sec_payer_dict = sec_payer if isinstance(sec_payer, dict) else sec_payer.model_dump()
            sec_payer_name = sec_payer_dict.get("name", "SECONDARY INSURANCE")
            sec_payer_id = sec_payer_dict.get("payer_id", "SEC01")
            sec_member_id = pat.get("secondary_member_id") or pat.get("member_id")
            
            # Loop 2320: Other Subscriber Info
            segments.append("SBR*S*18*******CI~")
            
            # Loop 2330A: Other Subscriber Name
            segments.append(f"NM1*IL*1*{pat['last_name']}*{pat['first_name']}****MI*{sec_member_id}~")
            segments.append(f"N3*{pat.get('address', '450 Biscayne Blvd')}~")
            segments.append(f"N4*{pat.get('city', 'Miami')}*{pat.get('state', 'FL')}*{pat.get('zip', '33132')}~")

            # Loop 2330B: Other Payer Name
            segments.append(f"NM1*PR*2*{sec_payer_name}*****PI*{sec_payer_id}~")
            segments.append(f"N3*{sec_payer_dict.get('address', 'PO BOX 2000')}~")
            segments.append(f"N4*{sec_payer_dict.get('city', 'Jacksonville')}*{sec_payer_dict.get('state', 'FL')}*{sec_payer_dict.get('zip', '32202')}~")

        # 2400 Service Lines
        dos_cleaned = str(encounter.get("date_of_service", full_date)).replace("-", "")
        for line_num, item in enumerate(billing.get("cpt_codes", []), start=1):
            item_dict = item if isinstance(item, dict) else item.model_dump()
            segments.append(f"LX*{line_num}~")
            cpt = item_dict["code"]
            modifiers = item_dict.get("modifiers", [])
            cpt_composite = f"HC:{cpt}" + ("".join(f":{m}" for m in modifiers) if modifiers else "")
            line_charge_str = f"{(item_dict['charge'] * item_dict.get('units', 1)):.2f}"
            units = item_dict.get("units", 1)

            segments.append(f"SV1*{cpt_composite}*{line_charge_str}*UN*{units}***1~")
            segments.append(f"DTP*472*D8*{dos_cleaned}~")

        # Trailers
        seg_count = len(segments) - 1
        segments.append(f"SE*{seg_count + 1}*0001~")
        segments.append(f"GE*1*1~")
        segments.append(f"IEA*1*{ctrl_str}~")

        return "\n".join(segments)
