# ============================================
# FILE: medical_billing_ai/eligibility/eligibility_engine.py
# PURPOSE: Real ANSI X12 270/271 Eligibility Verification via Office Ally
#
# Office Ally contract for eligibility (per OA companion guide):
#   - Eligibility uses a DIFFERENT submitter ID than claims
#     (OA_ELIGIBILITY_SUBMITTER_ID), and ISA03/ISA04 carry the
#     eligibility password (<=10 chars).
#   - Receiver: ISA07='30', ISA08/GS03='330897513'
#   - Batch path: 270 file uploaded to /inbound; 271 returned in /outbound
#   - ISA15 follows OA_USAGE_INDICATOR (T until OA confirms production)
#
# A real 271 is REQUIRED to report coverage. Without SFTP credentials the
# engine runs in explicit simulation mode (simulated=True) and never
# presents simulated benefits as verified coverage.
# ============================================

import time
from typing import Dict, Any, List, Optional

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


def parse_271(raw: str) -> Dict[str, Any]:
    """Parse a real 271 benefit response into coverage facts."""
    text = raw.replace("\r\n", "~").replace("\n", "~")
    segs = [s.strip().split("*") for s in text.split("~") if s.strip()]

    result: Dict[str, Any] = {
        "is_active": False,
        "coverage_description": "",
        "plan_name": "",
        "copay": 0.0,
        "remaining_deductible": 0.0,
        "out_of_pocket_remaining": 0.0,
        "coverage_dates": {"start": "", "end": ""},
        "benefits": [],
        "rejections": [],
        "messages": [],
    }

    def el(parts, i, d=""):
        return parts[i] if len(parts) > i else d

    for s in segs:
        if s[0] == "AAA":
            result["rejections"].append({
                "qualifier": el(s, 1), "code": el(s, 3),
                "description": el(s, 4),
            })
        elif s[0] == "NM1" and el(s, 1) == "PR":
            result["plan_name"] = result["plan_name"] or el(s, 3)
        elif s[0] == "DTP" and el(s, 1) == "346":
            result["coverage_dates"]["start"] = el(s, 3)
        elif s[0] == "DTP" and el(s, 1) == "347":
            result["coverage_dates"]["end"] = el(s, 3)
        elif s[0] == "EB":
            code = el(s, 1)
            desc = EB01_COVERAGE.get(code, code)
            amount = float(el(s, 7) or 0) if el(s, 7) else 0.0
            pct = float(el(s, 8) or 0) if el(s, 8) else 0.0
            entry = {
                "code": code, "description": desc,
                "service_type": el(s, 3), "plan_coverage": el(s, 4),
                "time_period": el(s, 6), "amount": amount, "percent": pct,
                "in_network": el(s, 11) in ("Y", "W"),
            }
            result["benefits"].append(entry)
            if code == "1":
                result["is_active"] = True
                result["coverage_description"] = desc
            elif code in ("6", "7", "8"):
                result["is_active"] = False
                result["coverage_description"] = desc
            elif code == "B" and el(s, 3) in ("30", "98", ""):
                result["copay"] = max(result["copay"], amount)
            elif code == "C":
                result["remaining_deductible"] = max(result["remaining_deductible"], amount)
            elif code == "G":
                result["out_of_pocket_remaining"] = max(result["out_of_pocket_remaining"], amount)
        elif s[0] == "MSG":
            result["messages"].append(el(s, 1))
    return result


class RealTimeEligibilityEngine:
    """Builds real 270 inquiries and parses real 271 responses."""

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
    def build_270(self, patient: PatientInfo, payer: PayerInfo,
                  provider_npi: str, provider_name: str,
                  service_type: str = "30") -> str:
        """005010X279A1 270 with OA eligibility envelope (separate submitter
        ID + ISA04 password per OA companion guide)."""
        now = time.localtime()
        isa13 = self.control_store.next("ISA13")
        gs06 = self.control_store.next("GS06")
        st02 = self.control_store.next("ST02_270")
        submitter = self.config.effective_eligibility_submitter_id
        elig_pw = self.config.eligibility_password
        isa03, isa04 = ("01", elig_pw.get_secret_value().ljust(10)) if elig_pw else ("00", " " * 10)
        dob = str(patient.dob).replace("-", "")
        dos = time.strftime("%Y%m%d", now)

        body = [
            f"ST*270*{st02:04d}*005010X279A1",
            f"BHT*0022*13*REQ{st02:06d}*{time.strftime('%Y%m%d%H%M', now)}",
            "HL*1**20*1",
            f"NM1*PR*2*{payer.name}*****PI*{payer.payer_id}",
            "HL*2*1*21*1",
            f"NM1*1P*2*{provider_name}*****XX*{provider_npi}",
            "HL*3*2*22*0",
            f"NM1*IL*1*{patient.last_name}*{patient.first_name}****MI*{patient.member_id}",
            f"DMG*D8*{dob}*{patient.gender}",
            f"DTP*291*D8*{dos}",
            f"EQ*{service_type}",
        ]
        body.append(f"SE*{len(body) + 1}*{st02:04d}")
        return "\n".join([
            f"ISA*00*{' ' * 10}*{isa03}*{isa04}*ZZ*{submitter.ljust(15)}"
            f"*30*{self.config.receiver_id.ljust(15)}"
            f"*{time.strftime('%y%m%d%H%M', now)[:6]}*{time.strftime('%H%M', now)}"
            f"*^*00501*{isa13:09d}*0*{self.config.usage_indicator}*:",
            f"GS*HS*{submitter}*{self.config.receiver_id}"
            f"*{time.strftime('%Y%m%d', now)}*{time.strftime('%H%M', now)}*{gs06}*X*005010X279A1",
            *body,
            f"GE*1*{gs06}",
            f"IEA*1*{isa13:09d}",
        ]) + "~"

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
        filename = f"OATEST_270_{self.config.effective_eligibility_submitter_id}_{int(time.time())}.edi" \
            if self.config.usage_indicator == "T" else \
            f"270_{self.config.effective_eligibility_submitter_id}_{int(time.time())}.edi"
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
        # 271 arrives asynchronously in /outbound; caller polls and runs
        # parse_271() on the returned file.
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
