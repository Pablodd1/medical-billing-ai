# ============================================
# FILE: medical_billing_ai/remittance/ack_parsers.py
# PURPOSE: Real parsers for X12 acknowledgments received from Office Ally
#          /outbound: TA1 (interchange), 999 (functional), 277CA (claim).
#
# These parsers extract machine-actionable results from the actual EDI
# files. No acknowledgment status is ever assumed or fabricated: a claim
# is only ACCEPTED when a real 999 AK9*A and a 277CA acceptance STC
# (category A1/A2) for its CLM01 have been parsed from a real file.
# ============================================

from typing import Dict, List, Any, Optional


def _segments(raw: str) -> List[List[str]]:
    """Split raw X12 text into element lists. Tolerates LF or ~ delimiters."""
    text = raw.replace("\r\n", "~").replace("\n", "~")
    segs = []
    for seg in text.split("~"):
        seg = seg.strip()
        if seg:
            segs.append(seg.split("*"))
    return segs


def _el(parts: List[str], idx: int, default: str = "") -> str:
    return parts[idx] if len(parts) > idx else default


class AcknowledgmentParser:
    """Parses TA1 / 999 / 277CA files and classifies file type by content."""

    # 277CA claim status category codes (STC01-1)
    STC_CATEGORY_DESCRIPTIONS = {
        "A0": "Acknowledgment/Forwarded",
        "A1": "Acknowledgment/Receipt - accepted by clearinghouse",
        "A2": "Acknowledgment/Acceptance into adjudication system",
        "A3": "Acknowledgment/Returned as unprocessable claim (REJECTED)",
        "A4": "Acknowledgment/Not Found",
        "A6": "Acknowledgment/Rejected for Missing Information",
        "A7": "Acknowledgment/Rejected for Invalid Information",
        "A8": "Acknowledgment/Rejected for relational field in error",
        "P0": "Pending",
        "F0": "Finalized",
        "F1": "Finalized/Payment",
        "F2": "Finalized/Denial",
        "F3": "Finalized/Revised",
        "E0": "Error",
        "R0": "Requests for additional information - general",
    }

    @staticmethod
    def classify_file(raw: str) -> str:
        """Returns 'TA1', '999', '277CA', '835', or 'UNKNOWN' based on content."""
        segs = _segments(raw)
        has_ta1 = any(s[0] == "TA1" for s in segs)
        st01 = next((_el(s, 1) for s in segs if s[0] == "ST"), "")
        if has_ta1 and st01 != "999":
            return "TA1"
        if st01 == "999":
            return "999"
        if st01 == "277":
            return "277CA"
        if st01 == "835":
            return "835"
        return "UNKNOWN"

    # ---------------- TA1 ----------------
    def parse_ta1(self, raw: str) -> List[Dict[str, Any]]:
        """TA1: Interchange Acknowledgment. TA104: A=accepted, R=rejected, E=accepted w/errors."""
        results = []
        for s in _segments(raw):
            if s[0] != "TA1":
                continue
            code = _el(s, 4)
            results.append({
                "type": "TA1",
                "isa13_acknowledged": _el(s, 1),
                "interchange_date": _el(s, 2),
                "interchange_time": _el(s, 3),
                "ack_code": code,
                "accepted": code in ("A", "E"),
                "note_code": _el(s, 5),
                "error_description": self._ta1_note(_el(s, 5)),
            })
        return results

    @staticmethod
    def _ta1_note(code: str) -> str:
        return {
            "000": "No error",
            "001": "ISA control number does not match IEA control number",
            "003": "Invalid interchange sender ID (ISA06) - check OA submitter ID",
            "007": "Invalid interchange receiver ID (ISA08) - must be 330897513",
            "024": "Invalid test/production indicator (ISA15)",
        }.get(code, f"TA1 note code {code}" if code else "")

    # ---------------- 999 ----------------
    def parse_999(self, raw: str) -> List[Dict[str, Any]]:
        """999: Functional Acknowledgment. Extracts per-group (AK1/AK9) and
        per-transaction (AK2/AK5) results with segment/syntax error detail."""
        results = []
        current: Optional[Dict[str, Any]] = None
        tx: Optional[Dict[str, Any]] = None
        for s in _segments(raw):
            if s[0] == "AK1":
                if current:
                    if tx:
                        current["transactions"].append(tx)
                        tx = None
                    results.append(current)
                current = {
                    "type": "999",
                    "functional_group_id": _el(s, 1),
                    "gs06_acknowledged": _el(s, 2),
                    "transactions": [],
                }
            elif s[0] == "AK2" and current is not None:
                if tx:
                    current["transactions"].append(tx)
                tx = {
                    "st02_acknowledged": _el(s, 2),
                    "status_code": "",
                    "accepted": None,
                    "errors": [],
                }
            elif s[0] in ("IK3", "AK3") and tx is not None:
                tx["errors"].append({
                    "segment_id": _el(s, 1),
                    "segment_position": _el(s, 2),
                    "loop_id": _el(s, 3),
                    "syntax_error_code": _el(s, 4),
                })
            elif s[0] in ("IK4", "AK4") and tx is not None and tx["errors"]:
                tx["errors"][-1].setdefault("element_errors", []).append({
                    "element_position": _el(s, 1),
                    "component_position": _el(s, 2),
                    "syntax_error_code": _el(s, 3),
                    "bad_data": _el(s, 4),
                })
            elif s[0] in ("IK5", "AK5") and tx is not None:
                tx["status_code"] = _el(s, 1)
                tx["accepted"] = _el(s, 1) in ("A", "E")
            elif s[0] == "AK9" and current is not None:
                if tx:
                    current["transactions"].append(tx)
                    tx = None
                current["status_code"] = _el(s, 1)
                current["accepted"] = _el(s, 1) in ("A", "E")
                current["st_count_in_group"] = _el(s, 2)
                results.append(current)
                current = None
        if current:
            if tx:
                current["transactions"].append(tx)
            results.append(current)
        return results

    # ---------------- 277CA ----------------
    def parse_277ca(self, raw: str) -> List[Dict[str, Any]]:
        """277CA: Claim Acknowledgment. Walks the hierarchy; every STC found
        is attached to the most recent TRN (claim key = CLM01 patient
        control number submitted in the 837)."""
        results: List[Dict[str, Any]] = []
        current_claim: Optional[Dict[str, Any]] = None
        current_payer = ""
        for s in _segments(raw):
            if s[0] == "NM1":
                # 2000A/2100A payer, 2100B OA, 2100C billing provider, 2200D subscriber
                if _el(s, 1) in ("PR", "41"):
                    current_payer = _el(s, 3)
            elif s[0] == "TRN":
                # TRN02 in 2200D = patient control number from CLM01
                current_claim = {
                    "type": "277CA",
                    "patient_control_number": _el(s, 2),
                    "payer_name": current_payer,
                    "status_events": [],
                    "accepted": None,
                }
                results.append(current_claim)
            elif s[0] == "STC":
                stc01 = _el(s, 1)
                comp = stc01.split(":")
                category = comp[0] if comp else ""
                status_code = comp[1] if len(comp) > 1 else ""
                event = {
                    "category_code": category,
                    "status_code": status_code,
                    "description": self.STC_CATEGORY_DESCRIPTIONS.get(category, f"Category {category}"),
                    "effective_date": _el(s, 2),
                    "action_code": _el(s, 3),
                    "total_charge": float(_el(s, 5) or 0) if _el(s, 5) else 0.0,
                    "free_text": _el(s, 9),
                }
                if current_claim is not None:
                    current_claim["status_events"].append(event)
                    # A1/A2 = accepted; A3/A6/A7/A8 or E* = rejected
                    if category in ("A1", "A2"):
                        current_claim["accepted"] = True
                    elif category in ("A3", "A6", "A7", "A8") or category.startswith("E"):
                        current_claim["accepted"] = False
        return results
