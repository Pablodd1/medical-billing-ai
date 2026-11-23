# ============================================
# FILE: medical_billing_ai/eligibility/eligibility_engine.py
# PURPOSE: Real ANSI X12 270/271 Eligibility Verification via Office Ally
#          (Supports both SFTP Batch /inbound -> /outbound and CAQH CORE 2.2.0 SOAP rtx.svc)
#
# Office Ally contract for eligibility (per OA 270/271 Companion Guide):
#   - Eligibility uses a DIFFERENT submitter ID than claims
#     (OA_ELIGIBILITY_SUBMITTER_ID), and ISA03/ISA04 carry the
#     eligibility password (<=10 chars) when assigned, or '00' + 10 spaces
#     when authenticated via CAQH CORE WS-Security / SFTP transport.
#   - Receiver: ISA07='30', ISA08/GS03='330897513'
#   - Every X12 segment MUST terminate with '~' (ISA..IEA inclusive)
#   - BHT*0022*13*<RefID>*<CCYYMMDD>*<HHMM>~ (BHT04 Date and BHT05 Time are separate elements!)
#   - Batch path: 270 file uploaded to /inbound; 271 returned in /outbound
#   - Real-time SOAP path: CAQH CORE 2.2.0 envelope posted to
#     https://wsd.officeally.com/TransactionService/rtx.svc
#   - ISA15 follows OA_USAGE_INDICATOR (T until OA confirms production; batch test files
#     must also contain 'OATEST' in filename)
#
# A real 271 is REQUIRED to report coverage. Without SFTP/SOAP credentials the
# engine runs in explicit simulation mode (simulated=True) and never
# presents simulated benefits as verified coverage.
# ============================================

import re
import time
import uuid
import xml.etree.ElementTree as ET
from html import escape as xml_escape
from typing import Dict, Any, List, Optional

try:
    import requests
except ImportError:
    requests = None

try:
    from config import OfficeAllyConfig
    from models.encounter import PatientInfo, PayerInfo
    from transmission.control_numbers import get_default_store
    from transmission.sftp_client import OfficeAllySFTPClient
except ImportError:
    from ..config import OfficeAllyConfig
    from ..models.encounter import PatientInfo, PayerInfo
    from ..transmission.control_numbers import get_default_store
    from ..transmission.sftp_client import OfficeAllySFTPClient


# ---------------- 271 parser ----------------
EB01_COVERAGE = {
    "1": "Active Coverage", "2": "Active - Full Risk Capitation",
    "3": "Active - Services Capitated", "4": "Active - Services Capitated to PCP",
    "5": "Active - Pending Investigation", "6": "Inactive",
    "7": "Inactive - Pending Eligibility Update", "8": "Inactive - Pending Investigation",
    "A": "Co-Insurance", "B": "Co-Payment", "C": "Deductible",
    "CB": "Coverage Basis", "F": "Limitations", "G": "Out of Pocket (Stop Loss)",
    "I": "Non-Covered", "R": "Other or Additional Payer", "U": "Contact Following Entity",
}

AAA03_REJECTION_REASONS = {
    "15": "Required application data missing",
    "41": "Authorization/Access Restrictions (Trading Partner not authorized for payer)",
    "42": "Unable to Respond at Current Time (Payer system timeout/down)",
    "43": "Invalid/Missing Provider Identification (Check NPI in NM1*1P)",
    "51": "Provider Not on File",
    "71": "Patient Birth Date Does Not Match That for the Patient on the Database",
    "72": "Invalid/Missing Subscriber/Insured ID (Check MBI / Member ID)",
    "73": "Invalid/Missing Subscriber/Insured Name",
    "75": "Subscriber/Insured Not Found",
    "79": "Invalid Participant Identification (Check Office Ally Payer ID)",
}


def parse_271(raw: str) -> Dict[str, Any]:
    """Parse a real 005010X279A1 271 benefit response into structured coverage facts."""
    text = raw.replace("\r\n", "~").replace("\n", "~")
    segs = [s.strip().split("*") for s in text.split("~") if s.strip()]

    result: Dict[str, Any] = {
        "is_active": False,
        "is_active_coverage": False,
        "coverage_description": "",
        "plan_name": "",
        "copay": 0.0,
        "copay_amount": 0.0,
        "coinsurance_percent": 0.0,
        "remaining_deductible": 0.0,
        "out_of_pocket_remaining": 0.0,
        "coverage_dates": {"start": "", "end": ""},
        "other_payers": [],
        "benefits": [],
        "rejections": [],
        "messages": [],
    }

    def el(parts: List[str], i: int, d: str = "") -> str:
        return parts[i] if len(parts) > i else d

    current_eb_code = ""

    for s in segs:
        seg_id = s[0]
        if seg_id == "AAA":
            reason_code = el(s, 3)
            result["rejections"].append({
                "valid_request": el(s, 1),
                "agency_qualifier": el(s, 2),
                "code": reason_code,
                "follow_up_action": el(s, 4),
                "description": AAA03_REJECTION_REASONS.get(reason_code, f"AAA Reject {reason_code}"),
            })
        elif seg_id == "NM1":
            if el(s, 1) == "PR" and current_eb_code != "R":
                result["plan_name"] = result["plan_name"] or el(s, 3)
            elif el(s, 1) in ("PR", "PRP") and current_eb_code == "R":
                # Loop 2120C Benefit Related Entity (e.g., Medicare Advantage MCO or Secondary Payer!)
                result["other_payers"].append({
                    "name": el(s, 3),
                    "payer_id": el(s, 9),
                })
        elif seg_id == "DTP" and el(s, 1) in ("346", "291"):
            val = el(s, 3)
            if "-" in val:
                start_dt, _, end_dt = val.partition("-")
                result["coverage_dates"]["start"] = result["coverage_dates"]["start"] or start_dt
                result["coverage_dates"]["end"] = result["coverage_dates"]["end"] or end_dt
            elif el(s, 1) == "346":
                result["coverage_dates"]["start"] = val
        elif seg_id == "DTP" and el(s, 1) == "347":
            result["coverage_dates"]["end"] = el(s, 3)
        elif seg_id == "EB":
            code = el(s, 1)
            current_eb_code = code
            desc = EB01_COVERAGE.get(code, code)
            amount = float(el(s, 7) or 0) if el(s, 7) else 0.0
            pct = float(el(s, 8) or 0) if el(s, 8) else 0.0
            entry = {
                "code": code,
                "coverage_level": el(s, 2),
                "description": desc,
                "service_type": el(s, 3),
                "insurance_type": el(s, 4),
                "plan_coverage": el(s, 5),
                "time_period": el(s, 6),
                "amount": amount,
                "percent": pct,
                "auth_required": el(s, 11),
                "in_network": el(s, 12) in ("Y", "W", ""),
            }
            result["benefits"].append(entry)
            if code in ("1", "2", "3", "4"):
                result["is_active"] = True
                result["is_active_coverage"] = True
                result["coverage_description"] = desc
            elif code in ("6", "7", "8") and not result["is_active"]:
                result["is_active"] = False
                result["is_active_coverage"] = False
                result["coverage_description"] = desc
            elif code == "B" and el(s, 3) in ("30", "98", ""):
                result["copay"] = max(result["copay"], amount)
                result["copay_amount"] = result["copay"]
            elif code == "A":
                result["coinsurance_percent"] = max(result["coinsurance_percent"], pct)
            elif code == "C":
                # Prefer remaining deductible (EB06='29') when present
                if el(s, 6) == "29" or result["remaining_deductible"] == 0.0:
                    result["remaining_deductible"] = amount if el(s, 6) == "29" else max(result["remaining_deductible"], amount)
            elif code == "G":
                if el(s, 6) == "29" or result["out_of_pocket_remaining"] == 0.0:
                    result["out_of_pocket_remaining"] = amount if el(s, 6) == "29" else max(result["out_of_pocket_remaining"], amount)
        elif seg_id == "MSG":
            result["messages"].append(el(s, 1))
    return result


class RealTimeEligibilityEngine:
    """Builds 005010X279A1 270 inquiries, supports CAQH CORE SOAP & SFTP transport, and parses 271 responses."""

    def __init__(self, config: Optional[OfficeAllyConfig] = None,
                 control_store=None, sftp_client=None):
        self.config = config or OfficeAllyConfig()
        self.control_store = control_store or get_default_store()
        self._sftp_client = sftp_client

    @property
    def client(self):
        if self._sftp_client is None:
            self._sftp_client = OfficeAllySFTPClient(self.config)
        return self._sftp_client

    # ---------------- 270 build ----------------
    def build_270(
        self,
        patient: PatientInfo,
        payer: PayerInfo,
        provider_npi: str = "",
        provider_name: str = "",
        service_type: str = "30",
        provider_entity_type: str = "2",
        provider_first_name: str = "",
        date_of_service: Optional[str] = None,
    ) -> str:
        """Builds a compliant X12 005010X279A1 270 inquiry with proper '~' segment terminators,
        split BHT04 (CCYYMMDD) & BHT05 (HHMM) elements, and OA ISA03/ISA04 rules."""
        now = time.localtime()
        isa_date = time.strftime("%y%m%d", now)
        gs_date = time.strftime("%Y%m%d", now)
        time_hm = time.strftime("%H%M", now)

        isa13 = self.control_store.next("ISA13")
        gs06 = self.control_store.next("GS06")
        st02 = self.control_store.next("ST02_270")

        submitter = self.config.effective_eligibility_submitter_id
        elig_pw = self.config.eligibility_password
        if elig_pw and elig_pw.get_secret_value():
            isa03 = "01"
            isa04 = elig_pw.get_secret_value()[:10].ljust(10)
        else:
            isa03 = "00"
            isa04 = " " * 10

        dob = re.sub(r"\D", "", str(patient.dob))
        dos = re.sub(r"\D", "", str(date_of_service)) if date_of_service else gs_date
        prov_name_clean = (provider_name or "MEDICAL PRACTICE").strip()
        prov_npi_clean = (provider_npi or "0000000000").strip()

        if str(provider_entity_type) == "1":
            nm1_1p = f"NM1*1P*1*{prov_name_clean}*{provider_first_name}****XX*{prov_npi_clean}~"
        else:
            nm1_1p = f"NM1*1P*2*{prov_name_clean}*****XX*{prov_npi_clean}~"

        body = [
            f"ST*270*{st02:04d}*005010X279A1~",
            f"BHT*0022*13*REQ{st02:06d}*{gs_date}*{time_hm}~",
            "HL*1**20*1~",
            f"NM1*PR*2*{payer.name}*****PI*{payer.payer_id}~",
            "HL*2*1*21*1~",
            nm1_1p,
            "HL*3*2*22*0~",
            f"NM1*IL*1*{patient.last_name}*{patient.first_name}****MI*{patient.member_id}~",
            f"DMG*D8*{dob}*{patient.gender}~",
            f"DTP*291*D8*{dos}~",
            f"EQ*{service_type}~",
        ]
        body.append(f"SE*{len(body) + 1}*{st02:04d}~")

        head = [
            f"ISA*00*{' ' * 10}*{isa03}*{isa04}*ZZ*{submitter[:15].ljust(15)}"
            f"*{self.config.receiver_qualifier}*{self.config.receiver_id[:15].ljust(15)}"
            f"*{isa_date}*{time_hm}*^*00501*{isa13:09d}*0*{self.config.usage_indicator}*:~",
            f"GS*HS*{submitter}*{self.config.receiver_id}"
            f"*{gs_date}*{time_hm}*{gs06}*X*005010X279A1~",
        ]
        tail = [
            f"GE*1*{gs06}~",
            f"IEA*1*{isa13:09d}~",
        ]
        return "\n".join(head + body + tail)

    # ---------------- CAQH CORE 2.2.0 Real-Time SOAP ----------------
    def build_core_soap_envelope(self, x12_270: str, payload_id: Optional[str] = None) -> str:
        """Wraps a 270 X12 transaction inside a CAQH CORE Connectivity Rule v2.2.0 SOAP 1.2
        Envelope with OASIS WS-Security UsernameToken for Office Ally `rtx.svc`."""
        username = self.config.soap_username or self.config.sftp_username or ""
        pw_secret = self.config.soap_password or self.config.sftp_password
        password = pw_secret.get_secret_value() if pw_secret else ""
        pid = payload_id or str(uuid.uuid4())
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        single_line_270 = x12_270.replace("\r", "").replace("\n", "")
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<soapenv:Envelope xmlns:soapenv="http://www.w3.org/2003/05/soap-envelope"\n'
            '                  xmlns:cor="http://www.caqh.org/SOAP/WSDL/CORERule2.2.0.xsd"\n'
            '                  xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">\n'
            '  <soapenv:Header>\n'
            '    <wsse:Security soapenv:mustUnderstand="true">\n'
            '      <wsse:UsernameToken>\n'
            f'        <wsse:Username>{xml_escape(username)}</wsse:Username>\n'
            f'        <wsse:Password>{xml_escape(password)}</wsse:Password>\n'
            '      </wsse:UsernameToken>\n'
            '    </wsse:Security>\n'
            '  </soapenv:Header>\n'
            '  <soapenv:Body>\n'
            '    <cor:COREEnvelopeRealTimeRequest>\n'
            '      <PayloadType>X12_270_Request_005010X279A1</PayloadType>\n'
            '      <ProcessingMode>RealTime</ProcessingMode>\n'
            f'      <PayloadID>{xml_escape(pid)}</PayloadID>\n'
            f'      <TimeStamp>{ts}</TimeStamp>\n'
            f'      <SenderID>{xml_escape(username or self.config.effective_eligibility_submitter_id)}</SenderID>\n'
            f'      <ReceiverID>{xml_escape(self.config.core_receiver_id)}</ReceiverID>\n'
            f'      <CORERuleVersion>{xml_escape(self.config.core_rule_version)}</CORERuleVersion>\n'
            f'      <Payload>{xml_escape(single_line_270)}</Payload>\n'
            '    </cor:COREEnvelopeRealTimeRequest>\n'
            '  </soapenv:Body>\n'
            '</soapenv:Envelope>'
        )

    @staticmethod
    def parse_core_soap_response(soap_xml: str) -> Dict[str, Any]:
        """Extracts `<Payload>`, `<ErrorCode>`, and `<ErrorMessage>` from an Office Ally
        CAQH CORE `COREEnvelopeRealTimeResponse` XML payload."""
        root = ET.fromstring(soap_xml)
        fields: Dict[str, str] = {}
        for elem in root.iter():
            local_tag = elem.tag.split("}")[-1]
            if local_tag in ("PayloadType", "PayloadID", "ErrorCode", "ErrorMessage", "Payload") and elem.text:
                fields[local_tag] = elem.text.strip()
        payload = fields.get("Payload", "")
        parsed = parse_271(payload) if payload.startswith("ISA*") else {}
        return {
            "error_code": fields.get("ErrorCode", ""),
            "error_message": fields.get("ErrorMessage", ""),
            "payload_type": fields.get("PayloadType", ""),
            "payload_id": fields.get("PayloadID", ""),
            "raw_271": payload,
            "parsed_271": parsed,
        }

    def check_eligibility_soap(
        self,
        patient: PatientInfo,
        payer: PayerInfo,
        provider_npi: str = "",
        provider_name: str = "",
        service_type: str = "30",
        timeout: int = 20,
    ) -> Dict[str, Any]:
        """Executes a synchronous CAQH CORE 2.2.0 SOAP 270/271 inquiry against `wsd.officeally.com`."""
        if requests is None:
            raise RuntimeError("requests is required for CAQH CORE SOAP eligibility checks.")
        if not self.config.has_soap_credentials:
            raise RuntimeError("Office Ally SOAP credentials are not configured (set OA_SOAP_USERNAME / OA_SOAP_PASSWORD).")
        req_270 = self.build_270(patient, payer, provider_npi, provider_name, service_type)
        soap_env = self.build_core_soap_envelope(req_270)
        headers = {"Content-Type": 'application/soap+xml; charset=utf-8; action="RealTimeTransaction"'}
        resp = requests.post(self.config.soap_realtime_url, data=soap_env.encode("utf-8"), headers=headers, timeout=timeout)
        resp.raise_for_status()
        decoded = self.parse_core_soap_response(resp.text)
        if decoded.get("error_code") not in ("", "Success"):
            return {
                "simulation": False,
                "verified": False,
                "status": "CORE_ERROR",
                "error_code": decoded.get("error_code"),
                "error_message": decoded.get("error_message"),
                "x12_270_inquiry": req_270,
            }
        return {
            "simulation": False,
            "verified": True,
            "transport": "CAQH_CORE_SOAP",
            "x12_270_inquiry": req_270,
            "raw_271": decoded["raw_271"],
            **decoded["parsed_271"],
        }

    # ---------------- verification ----------------
    def check_eligibility(self, patient: PatientInfo, payer: PayerInfo,
                          provider_npi: str = "", provider_name: str = "",
                          service_type: str = "30") -> Dict[str, Any]:
        req_270 = self.build_270(patient, payer, provider_npi, provider_name, service_type)

        if self.config.has_sftp_credentials:
            return self._real_check(req_270)

        # Explicit simulation fallback (dev/test only)
        sim = self._simulate_271(patient, payer)
        return {
            "simulation": True,
            "warning": "SIMULATION MODE: no 270 was sent to Office Ally; "
                       "benefits below are NOT verified coverage.",
            "x12_270_inquiry": req_270,
            **sim,
        }

    def _real_check(self, req_270: str) -> Dict[str, Any]:
        filename = (
            f"OATEST_270_{self.config.effective_eligibility_submitter_id}_{int(time.time())}.edi"
            if self.config.usage_indicator == "T"
            else f"270_{self.config.effective_eligibility_submitter_id}_{int(time.time())}.edi"
        )
        transport = None
        try:
            transport, sftp = self.client._connect()
            remote = f"{self.config.inbound_dir.rstrip('/')}/{filename}"
            with sftp.file(remote, "w") as f:
                f.write(req_270)
        finally:
            if transport:
                sftp.close()
                transport.close()
        return {
            "simulation": False,
            "x12_270_inquiry": req_270,
            "submitted_filename": filename,
            "status": "270_SUBMITTED_AWAITING_271",
            "next_step": "Poll /outbound for the 271 and pass it to parse_271().",
        }

    def check_and_parse(self, patient: PatientInfo, payer: PayerInfo,
                        response_271_text: str, **kwargs) -> Dict[str, Any]:
        """Combine: build/submit 270, then parse a real 271 response file."""
        check = self.check_eligibility(patient, payer, **kwargs)
        if check.get("simulation"):
            return check
        parsed = parse_271(response_271_text)
        return {"simulation": False, "verified": True, **parsed}

    # ---------------- simulation ----------------
    def _simulate_271(self, patient: PatientInfo, payer: PayerInfo) -> Dict[str, Any]:
        is_medicare = "medicare" in payer.name.lower()
        is_active = bool(patient.member_id and len(patient.member_id) > 5)
        return {
            "is_active_coverage": is_active,
            "plan_name": f"{payer.name} Comprehensive Health",
            "copay_amount": 0.0 if is_medicare else 25.00,
            "remaining_deductible": 0.0 if is_medicare else 150.00,
            "prior_auth_required_services": ["MRI", "CT Scan", "Biologic Infusion", "Elective Surgery"],
            "coverage_dates": {"start": "2026-01-01", "end": "2026-12-31"},
        }
