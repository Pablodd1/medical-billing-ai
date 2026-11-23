# ============================================
# FILE: medical_billing_ai/remittance/era_835_parser.py
# PURPOSE: ANSI X12 835 (005010X221A1 Electronic Remittance Advice / EOB)
#          Ingestion, Service Line (SVC/CAS/LQ/DTM) Extraction, Crossover (NM1*TT),
#          Provider-Level Adjustments (PLB), & Dynamic Denial Learning
# ============================================

from typing import Dict, List, Any, Optional


class ERA_835_Parser:
    """
    Parses standard ANSI X12 5010A1 835 (005010X221A1) ERA files received from Office Ally.
    Supports both single-line ('~' delimited) and newline-delimited 835 streams,
    extracts BPR payment financials, TRN check/EFT trace, CLP claim headers (including
    status 19/20/21 Medicare-to-Medicaid crossover indicators), NM1*TT crossover carrier,
    SVC service lines, claim & line-level CAS adjustments, LQ*HE RARC remark codes,
    and PLB provider-level adjustments (e.g., Medicare 2% sequestration WO/72).
    """

    CARC_DESCRIPTIONS = {
        "1": "Deductible Amount",
        "2": "Coinsurance Amount",
        "3": "Copayment Amount",
        "4": "The procedure code is inconsistent with the modifier used or a required modifier is missing.",
        "16": "Claim/service lacks information or has submission/billing error(s) needed for adjudication.",
        "18": "Exact duplicate claim/service.",
        "22": "This care may be covered by another payer per coordination of benefits.",
        "24": "Charges are covered under a capitation agreement/managed care plan (e.g., Medicare Advantage).",
        "29": "The time limit for filing has expired.",
        "45": "Charge exceeds fee schedule/maximum allowable or contracted/legislated fee arrangement.",
        "50": "Non-covered services because this is not deemed a 'medical necessity' by the payer.",
        "96": "Non-covered charge(s).",
        "97": "The benefit for this service is included in the payment/allowance for another service/procedure.",
        "109": "Claim/service not covered by this payer/contractor. You must send the claim/service to the correct payer/contractor.",
        "167": "This (these) diagnosis(es) is (are) not covered.",
        "197": "Precertification/authorization absent, superseded, or exceeded.",
        "253": "Sequestration - reduction in federal payment.",
    }

    CLP_STATUS_DESCRIPTIONS = {
        "1": "Processed as Primary",
        "2": "Processed as Secondary",
        "3": "Processed as Tertiary",
        "4": "Denied",
        "19": "Processed as Primary, Forwarded to Additional Payer(s) (Crossover)",
        "20": "Processed as Secondary, Forwarded to Additional Payer(s) (Crossover)",
        "21": "Processed as Tertiary, Forwarded to Additional Payer(s) (Crossover)",
        "22": "Reversal of Previous Payment",
        "23": "Not Our Claim, Forwarded to Additional Payer(s)",
        "25": "Predetermination Pricing Only - No Payment",
    }

    def __init__(self):
        self.learned_denial_rules: List[Dict[str, Any]] = []

    @staticmethod
    def _segments(era_raw_text: str) -> List[List[str]]:
        text = era_raw_text.replace("\r\n", "~").replace("\n", "~")
        segs: List[List[str]] = []
        for raw_seg in text.split("~"):
            seg = raw_seg.strip()
            if seg:
                segs.append(seg.split("*"))
        return segs

    @staticmethod
    def _safe_float(val: str, default: float = 0.0) -> float:
        try:
            return float(val) if val != "" else default
        except (ValueError, TypeError):
            return default

    def parse_835(self, era_raw_text: str) -> Dict[str, Any]:
        segs = self._segments(era_raw_text)

        parsed_era: Dict[str, Any] = {
            "payer_name": "",
            "payee_name": "",
            "payee_npi": "",
            "check_trace_number": "",
            "payment_method": "",
            "total_payment_amount": 0.0,
            "payment_effective_date": "",
            "provider_adjustments": [],
            "claims": [],
        }

        current_claim: Optional[Dict[str, Any]] = None
        current_svc: Optional[Dict[str, Any]] = None

        for parts in segs:
            seg_id = parts[0]

            if seg_id == "BPR" and len(parts) > 2:
                parsed_era["total_payment_amount"] = self._safe_float(parts[2])
                parsed_era["payment_method"] = parts[4] if len(parts) > 4 else ""
                if len(parts) > 16:
                    parsed_era["payment_effective_date"] = parts[16]

            elif seg_id == "TRN" and len(parts) > 2 and current_claim is None:
                parsed_era["check_trace_number"] = parts[2]

            elif seg_id == "N1" and len(parts) > 2:
                if parts[1] == "PR":
                    parsed_era["payer_name"] = parts[2]
                elif parts[1] == "PE":
                    parsed_era["payee_name"] = parts[2]
                    if len(parts) > 4 and parts[3] == "XX":
                        parsed_era["payee_npi"] = parts[4]

            elif seg_id == "CLP":
                if current_claim is not None:
                    parsed_era["claims"].append(current_claim)
                current_svc = None

                status_code = parts[2] if len(parts) > 2 else ""
                status_desc = self.CLP_STATUS_DESCRIPTIONS.get(status_code, f"Status {status_code}")

                current_claim = {
                    "patient_control_number": parts[1] if len(parts) > 1 else "",
                    "status_code": status_code,
                    "status_description": status_desc,
                    "is_crossover": status_code in ("19", "20", "21"),
                    "crossover_carrier": "",
                    "total_charge": self._safe_float(parts[3]) if len(parts) > 3 else 0.0,
                    "total_paid": self._safe_float(parts[4]) if len(parts) > 4 else 0.0,
                    "patient_responsibility": self._safe_float(parts[5]) if len(parts) > 5 else 0.0,
                    "claim_filing_indicator": parts[6] if len(parts) > 6 else "",
                    "payer_claim_control_number": parts[7] if len(parts) > 7 else "",
                    "patient_name": "",
                    "member_id": "",
                    "remark_codes": [],
                    "adjustments": [],
                    "service_lines": [],
                }

            elif seg_id == "NM1" and current_claim is not None and len(parts) > 2:
                ent = parts[1]
                if ent == "QC":
                    last = parts[3] if len(parts) > 3 else ""
                    first = parts[4] if len(parts) > 4 else ""
                    current_claim["patient_name"] = f"{last}, {first}".strip(", ")
                    if len(parts) > 9:
                        current_claim["member_id"] = parts[9]
                elif ent == "TT":
                    # Crossover Carrier Name (e.g., FLORIDA MEDICAID when Medicare crosses over automatically)
                    current_claim["crossover_carrier"] = parts[3] if len(parts) > 3 else ""

            elif seg_id == "MOA" and current_claim is not None:
                for rem in parts[3:8]:
                    if rem:
                        current_claim["remark_codes"].append(rem)

            elif seg_id == "SVC" and current_claim is not None:
                comp = (parts[1] if len(parts) > 1 else "").split(":")
                proc_code = comp[1] if len(comp) > 1 else comp[0]
                modifiers = [m for m in comp[2:] if m]
                current_svc = {
                    "procedure_code": proc_code,
                    "modifiers": modifiers,
                    "charge_amount": self._safe_float(parts[2]) if len(parts) > 2 else 0.0,
                    "paid_amount": self._safe_float(parts[3]) if len(parts) > 3 else 0.0,
                    "units_paid": self._safe_float(parts[5], 1.0) if len(parts) > 5 else 1.0,
                    "service_date": "",
                    "adjustments": [],
                    "remark_codes": [],
                }
                current_claim["service_lines"].append(current_svc)

            elif seg_id == "DTM" and len(parts) > 2:
                if parts[1] in ("472", "150") and current_svc is not None:
                    current_svc["service_date"] = parts[2]

            elif seg_id == "CAS" and current_claim is not None:
                group_code = parts[1] if len(parts) > 1 else "CO"
                idx = 2
                while idx < len(parts):
                    carc = parts[idx].strip() if idx < len(parts) else ""
                    if not carc:
                        break
                    amount = self._safe_float(parts[idx + 1]) if (idx + 1) < len(parts) else 0.0
                    qty = self._safe_float(parts[idx + 2], 1.0) if (idx + 2) < len(parts) and parts[idx + 2] else 1.0
                    adjustment = {
                        "group_code": group_code,
                        "carc_code": carc,
                        "description": self.CARC_DESCRIPTIONS.get(carc, "Payer Specific Adjustment"),
                        "amount": amount,
                        "quantity": qty,
                        "level": "LINE" if current_svc is not None else "CLAIM",
                    }
                    # Attach to service line if inside Loop 2110, AND surface in claim['adjustments']
                    # so downstream DenialAppealsEngine & PatientBalanceCollector see both claim & line CAS!
                    if current_svc is not None:
                        current_svc["adjustments"].append(adjustment)
                    current_claim["adjustments"].append(adjustment)
                    self._ingest_denial_feedback(current_claim, adjustment, parsed_era.get("payer_name", "UNKNOWN"))
                    idx += 3

            elif seg_id == "LQ" and len(parts) > 2 and parts[1] == "HE":
                rarc = parts[2]
                if current_svc is not None:
                    current_svc["remark_codes"].append(rarc)
                if current_claim is not None and rarc not in current_claim["remark_codes"]:
                    current_claim["remark_codes"].append(rarc)

            elif seg_id == "PLB" and len(parts) > 3:
                if current_claim is not None:
                    parsed_era["claims"].append(current_claim)
                    current_claim = None
                    current_svc = None
                idx = 3
                while idx < len(parts):
                    reason_comp = parts[idx].split(":")
                    reason_code = reason_comp[0]
                    ref_id = reason_comp[1] if len(reason_comp) > 1 else ""
                    amt = self._safe_float(parts[idx + 1]) if (idx + 1) < len(parts) else 0.0
                    parsed_era["provider_adjustments"].append({
                        "reason_code": reason_code,
                        "reference_id": ref_id,
                        "amount": amt,
                    })
                    idx += 2

        if current_claim is not None:
            parsed_era["claims"].append(current_claim)

        return parsed_era

    def _ingest_denial_feedback(self, claim: Dict[str, Any], adjustment: Dict[str, Any], payer_name: str):
        carc = adjustment["carc_code"]
        if carc in ["4", "16", "24", "29", "50", "97", "109", "197"]:
            rule = {
                "payer": payer_name,
                "carc": carc,
                "carc_description": adjustment["description"],
                "claim_id": claim["patient_control_number"],
                "remedy": self._determine_remedy(carc),
            }
            self.learned_denial_rules.append(rule)

    def _determine_remedy(self, carc: str) -> str:
        if carc == "97":
            return "AUTO-FIX: Procedure was bundled by payer. Enforce Modifier -25 or -59 on encounter note verification before resubmission."
        elif carc == "50":
            return "AUTO-AUDIT: Payer denied for Medical Necessity. Cross-reference LCD/NCD diagnosis code list and verify ABN on file."
        elif carc == "4":
            return "AUTO-FIX: Modifier inconsistent with CPT code. Review NCCI modifier indicator table."
        elif carc == "24" or carc == "109":
            return "PAYER-ROUTING BLOCK: Patient enrolled in Medicare Advantage MCO or alternate carrier; re-verify 270/271 eligibility and rebill MCO Payer ID."
        elif carc == "197":
            return "UPSTREAM BLOCK: Prior Authorization was required. Connect to 278 real-time PA check in Magnolia intake."
        return "ROUTE_TO_HUMAN: Review encounter note with billing specialist."
