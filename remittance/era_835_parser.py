# ============================================
# FILE: medical_billing_ai/remittance/era_835_parser.py
# PURPOSE: ANSI X12 835 (Electronic Remittance Advice / EOB) Ingestion & Dynamic Learning
# ============================================

from typing import Dict, List, Any

class ERA_835_Parser:
    """
    Parses standard ANSI X12 835 ERA files received from Office Ally.
    Extracts payment status, claim adjustments (CAS), and feeds CARC/RARC codes back.
    """

    CARC_DESCRIPTIONS = {
        "1": "Deductible Amount",
        "2": "Coinsurance Amount",
        "3": "Copayment Amount",
        "4": "The procedure code is inconsistent with the modifier used or a required modifier is missing.",
        "16": "Claim/service lacks information or has submission/billing error(s) needed for adjudication.",
        "50": "Non-covered services because this is not deemed a 'medical necessity' by the payer.",
        "97": "The benefit for this service is included in the payment/allowance for another service/procedure.",
        "197": "Precertification/authorization absent, superseded, or exceeded."
    }

    def __init__(self):
        self.learned_denial_rules = []

    def parse_835(self, era_raw_text: str) -> Dict[str, Any]:
        lines = [line.strip().rstrip("~") for line in era_raw_text.split("\n") if line.strip()]
        
        parsed_era = {
            "payer_name": "",
            "check_trace_number": "",
            "claims": []
        }

        current_claim = None

        for line in lines:
            parts = line.split("*")
            seg_id = parts[0]

            if seg_id == "N1" and len(parts) > 2 and parts[1] == "PR":
                parsed_era["payer_name"] = parts[2]

            elif seg_id == "TRN" and len(parts) > 2:
                parsed_era["check_trace_number"] = parts[2]

            elif seg_id == "CLP":
                if current_claim:
                    parsed_era["claims"].append(current_claim)
                
                status_code = parts[2]
                status_desc = {
                    "1": "Processed as Primary",
                    "2": "Processed as Secondary",
                    "3": "Processed as Tertiary",
                    "4": "Denied",
                    "22": "Reversal of Previous Payment"
                }.get(status_code, f"Status {status_code}")

                current_claim = {
                    "patient_control_number": parts[1],
                    "status_code": status_code,
                    "status_description": status_desc,
                    "total_charge": float(parts[3]) if len(parts) > 3 else 0.0,
                    "total_paid": float(parts[4]) if len(parts) > 4 else 0.0,
                    "patient_responsibility": float(parts[5]) if len(parts) > 5 else 0.0,
                    "adjustments": [],
                    "service_lines": []
                }

            elif seg_id == "CAS" and current_claim:
                group_code = parts[1]
                idx = 2
                while idx < len(parts):
                    carc = parts[idx]
                    amount = float(parts[idx + 1]) if (idx + 1) < len(parts) and parts[idx + 1] else 0.0
                    adjustment = {
                        "group_code": group_code,
                        "carc_code": carc,
                        "description": self.CARC_DESCRIPTIONS.get(carc, "Payer Specific Adjustment"),
                        "amount": amount
                    }
                    current_claim["adjustments"].append(adjustment)
                    self._ingest_denial_feedback(current_claim, adjustment, parsed_era.get("payer_name", "UNKNOWN"))
                    idx += 3

        if current_claim:
            parsed_era["claims"].append(current_claim)

        return parsed_era

    def _ingest_denial_feedback(self, claim: Dict[str, Any], adjustment: Dict[str, Any], payer_name: str):
        carc = adjustment["carc_code"]
        if carc in ["4", "16", "50", "97", "197"]:
            rule = {
                "payer": payer_name,
                "carc": carc,
                "carc_description": adjustment["description"],
                "claim_id": claim["patient_control_number"],
                "remedy": self._determine_remedy(carc)
            }
            self.learned_denial_rules.append(rule)

    def _determine_remedy(self, carc: str) -> str:
        if carc == "97":
            return "AUTO-FIX: Procedure was bundled by payer. Enforce Modifier -25 or -59 on encounter note verification before resubmission."
        elif carc == "50":
            return "AUTO-AUDIT: Payer denied for Medical Necessity. Cross-reference LCD/NCD diagnosis code list and verify ABN on file."
        elif carc == "4":
            return "AUTO-FIX: Modifier inconsistent with CPT code. Review NCCI modifier indicator table."
        elif carc == "197":
            return "UPSTREAM BLOCK: Prior Authorization was required. Connect to 278 real-time PA check in Magnolia intake."
        return "ROUTE_TO_HUMAN: Review encounter note with billing specialist."
