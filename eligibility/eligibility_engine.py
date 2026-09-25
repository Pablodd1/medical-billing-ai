# ============================================
# FILE: medical_billing_ai/eligibility/eligibility_engine.py
# PURPOSE: ANSI X12 270/271 Real-Time Eligibility & Prior Authorization Verification
# ============================================

from typing import Dict, Any

try:
    from models.encounter import PatientInfo, PayerInfo
except ImportError:
    from ..models.encounter import PatientInfo, PayerInfo

class RealTimeEligibilityEngine:
    """
    Simulates and generates ANSI X12 270 Real-Time Eligibility Inquiries
    and parses 271 Benefit Responses from Office Ally / Payers.
    """

    def check_eligibility(self, patient: PatientInfo, payer: PayerInfo) -> Dict[str, Any]:
        req_270 = self._build_270_inquiry(patient, payer)
        res_271 = self._simulate_271_response(patient, payer)

        return {
            "x12_270_inquiry": req_270,
            "is_active_coverage": res_271["is_active"],
            "plan_name": res_271["plan_name"],
            "copay_amount": res_271["copay"],
            "remaining_deductible": res_271["remaining_deductible"],
            "prior_auth_required_services": res_271["prior_auth_required_services"],
            "coverage_dates": res_271["coverage_dates"]
        }

    def _build_270_inquiry(self, patient: PatientInfo, payer: PayerInfo) -> str:
        dob_clean = str(patient.dob).replace("-", "")
        return (
            f"ISA*00*          *00*          *ZZ*MAGNOLIA_HEALTH*ZZ*{payer.payer_id.ljust(15)}*260925*1400*^*00501*000000001*0*T*:~\n"
            f"GS*HS*MAGNOLIA_HEALTH*{payer.payer_id}*20260925*1400*1*X*005010X279A1~\n"
            f"ST*270*0001*005010X279A1~\n"
            f"BHT*0022*13*REQ001*20260925*1400~\n"
            f"HL*1**20*1~\n"
            f"NM1*PR*2*{payer.name}*****PI*{payer.payer_id}~\n"
            f"HL*2*1*21*1~\n"
            f"NM1*1P*2*SUNSHINE MEDICAL GROUP LLC*****XX*1234567890~\n"
            f"HL*3*2*22*0~\n"
            f"NM1*IL*1*{patient.last_name}*{patient.first_name}****MI*{patient.member_id}~\n"
            f"DMG*D8*{dob_clean}*{patient.gender}~\n"
            f"DTP*291*D8*20260925~\n"
            f"EQ*30~\n"
            f"SE*13*0001~\n"
            f"GE*1*1~\n"
            f"IEA*1*000000001~"
        )

    def _simulate_271_response(self, patient: PatientInfo, payer: PayerInfo) -> Dict[str, Any]:
        is_medicare = "medicare" in payer.name.lower()
        is_active = bool(patient.member_id and len(patient.member_id) > 5)

        return {
            "is_active": is_active,
            "plan_name": f"{payer.name} Comprehensive Health",
            "copay": 0.0 if is_medicare else 25.00,
            "remaining_deductible": 0.0 if is_medicare else 150.00,
            "prior_auth_required_services": ["MRI", "CT Scan", "Biologic Infusion", "Elective Surgery"],
            "coverage_dates": {"start": "2026-01-01", "end": "2026-12-31"}
        }
