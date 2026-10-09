# ============================================
# FILE: medical_billing_ai/coding/clinical_coder.py
# PURPOSE: Clinical NLP & E/M Medical Decision Making (MDM) Coding Engine
# ============================================

import re
from typing import List, Dict, Tuple, Any

try:
    from models.encounter import ClinicalNoteParsed, DiagnosisCode, CPTServiceLine, BillingDetails
except ImportError:
    from ..models.encounter import ClinicalNoteParsed, DiagnosisCode, CPTServiceLine, BillingDetails

class ClinicalCodingEngine:
    """
    Translates unstructured clinical notes into ICD-10-CM diagnosis codes,
    CPT procedure codes, calculates AMA 2021/2023 E/M MDM levels, and attaches modifiers.
    """

    DIAGNOSIS_ONTOLOGY = {
        "diabetes": ("E11.9", "Type 2 diabetes mellitus without complications"),
        "t2dm": ("E11.9", "Type 2 diabetes mellitus without complications"),
        "hypertension": ("I10", "Essential (primary) hypertension"),
        "htn": ("I10", "Essential (primary) hypertension"),
        "actinic keratosis": ("L57.0", "Actinic keratosis"),
        "osteoarthritis": ("M17.0", "Bilateral primary osteoarthritis of knee"),
        "knee pain": ("M25.561", "Pain in right knee"),
        "vitamin d deficiency": ("E55.9", "Vitamin D deficiency, unspecified"),
        "fatigue": ("R53.83", "Other fatigue"),
        "hyperlipidemia": ("E78.5", "Hyperlipidemia, unspecified")
    }

    PROCEDURE_ONTOLOGY = {
        "cryotherapy": ("17000", 120.00, "Destruction premalignant lesions; first lesion"),
        "cryosurgery": ("17000", 120.00, "Destruction premalignant lesions; first lesion"),
        "liquid nitrogen": ("17000", 120.00, "Destruction premalignant lesions; first lesion"),
        "joint injection": ("20610", 160.00, "Aspiration/injection intermediate or major joint"),
        "arthrocentesis": ("20610", 160.00, "Aspiration/injection intermediate or major joint"),
        "vitamin d test": ("82306", 65.00, "Vitamin D; 25 hydroxy, includes fraction(s)")
    }

    def generate_coding(self, note: ClinicalNoteParsed, is_telehealth: bool = False) -> BillingDetails:
        full_text = f"{note.chief_complaint} {note.history_of_present_illness} {note.assessment_and_plan}"

        extracted_diagnoses = self._extract_diagnoses(full_text)
        extracted_procedures = self._extract_procedures(full_text)
        em_code, em_charge = self._score_em_level(note, len(extracted_diagnoses))

        # Determine POS
        if is_telehealth:
            pos = "02"
        elif note.place_of_service_detected:
            pos = note.place_of_service_detected
        else:
            pos = "11"

        is_virtual = pos in ["02", "10"] or is_telehealth

        service_lines: List[CPTServiceLine] = []

        em_modifiers = []
        if is_virtual:
            em_modifiers.append("95")
        # Check if surgical/minor procedures (CPT 10000-69999) were performed that warrant Modifier -25
        # (Modifier -25 is ONLY for significant, separately identifiable E/M on the same day as a minor surgical/procedure,
        # NOT for diagnostic radiology 70000s, pathology/labs 80000s, or medicine diagnostic services)
        has_surgical_procedure = any(
            re.match(r"^[1-6][0-9]{4}$", str(proc.get("code", "")))
            for proc in extracted_procedures
        )
        if has_surgical_procedure:
            em_modifiers.append("25")

        service_lines.append(CPTServiceLine(
            code=em_code,
            modifiers=em_modifiers,
            charge=em_charge,
            units=1,
            dx_pointers=[1, 2] if len(extracted_diagnoses) >= 2 else [1]
        ))

        for proc in extracted_procedures:
            # Map procedure to primary supporting diagnosis pointer 1
            service_lines.append(CPTServiceLine(
                code=proc["code"],
                modifiers=[],
                charge=proc["charge"],
                units=1,
                dx_pointers=[1]
            ))

        total_charge = sum(line.charge * line.units for line in service_lines)

        return BillingDetails(
            place_of_service=pos,
            total_charge=round(total_charge, 2),
            diagnosis_codes=extracted_diagnoses,
            cpt_codes=service_lines
        )

    # Common non-diagnosis tokens that match letter+2 digits (e.g. B12 vitamin, D3, A1c, etc.)
    NON_DIAGNOSIS_TOKENS = {"B12", "D03", "A01", "D3", "TID", "BID", "QID", "PRN", "POS", "CPT", "NPI", "APRN", "MD", "DO"}

    # Symptom codes that must be suppressed if a definitive etiology / underlying condition is coded
    # per ICD-10-CM Official Guidelines Section I.B.5
    SYMPTOM_SUPPRESSION_MAP = {
        "M25.561": ["M23.", "M17."],  # Knee pain suppressed by internal derangement (M23) or osteoarthritis (M17)
        "M25.562": ["M23.", "M17."],
        "M25.569": ["M23.", "M17."],
        "R53.83": ["E55.", "E11.", "I10"]  # Fatigue suppressed if endocrine/chronic disease explains it
    }

    def _extract_diagnoses(self, text: str) -> List[DiagnosisCode]:
        diagnoses: List[DiagnosisCode] = []
        seen_codes = set()
        pointer = 1
        text_lower = text.lower()
        negation_terms = ["denies", "no history of", "negative for", "without", "ruled out", "no signs of", "r/o", "no evidence of"]

        # 1. First priority: Extract explicitly documented ICD-10 codes in note e.g. M23.201, I10, L57.0
        explicit_codes = re.findall(r"\b([A-TV-Z][0-9]{2}(?:\.[0-9A-Z]{1,4})?)\b", text)
        for code in explicit_codes:
            code_upper = code.upper()
            if code_upper in self.NON_DIAGNOSIS_TOKENS:
                continue

            # Check if this exact code is preceded by negation in the clinical text
            code_idx = text_lower.find(code.lower())
            if code_idx != -1:
                prefix_context = text_lower[max(0, code_idx - 50):code_idx]
                if any(neg in prefix_context for neg in negation_terms):
                    continue

                # Also verify it is not preceded by "vitamin" or "vit"
                if re.search(r"\b(?:vitamin|vit)\s*$", prefix_context.strip()):
                    continue

            # Standard ICD-10 validation: Category letter + 2 digits, optional dot + alphanumeric
            if re.match(r"^[A-Z][0-9]{2}(\.[0-9A-Z]{1,4})?$", code) and code not in seen_codes:
                seen_codes.add(code)
                diagnoses.append(DiagnosisCode(
                    code=code,
                    description=f"Documented clinical diagnosis {code}",
                    pointer=pointer,
                    is_primary=(pointer == 1)
                ))
                pointer += 1

        # 2. Second priority: Keyword ontology matching with negation & symptom suppression
        for keyword, (code, desc) in self.DIAGNOSIS_ONTOLOGY.items():
            if keyword in text_lower and code not in seen_codes:
                # Check for negation in the 50 characters preceding keyword
                idx = text_lower.find(keyword)
                context = text_lower[max(0, idx - 50):idx]
                if any(neg in context for neg in negation_terms):
                    continue  # Negated condition, do not code

                # ICD-10-CM Guideline I.B.5: Do not code symptoms if definitive diagnosis is already documented
                if code in self.SYMPTOM_SUPPRESSION_MAP:
                    suppressor_prefixes = self.SYMPTOM_SUPPRESSION_MAP[code]
                    if any(any(sc.startswith(pref) for pref in suppressor_prefixes) for sc in seen_codes):
                        continue  # Suppressed by confirmed definitive diagnosis!

                seen_codes.add(code)
                diagnoses.append(DiagnosisCode(
                    code=code,
                    description=desc,
                    pointer=pointer,
                    is_primary=(pointer == 1)
                ))
                pointer += 1

        if not diagnoses:
            diagnoses.append(DiagnosisCode(
                code="Z00.00",
                description="Encounter for general adult medical examination without abnormal findings",
                pointer=1,
                is_primary=True
            ))
        return diagnoses

    def _extract_procedures(self, text: str) -> List[Dict[str, Any]]:
        procedures: List[Dict[str, Any]] = []
        seen_cpts = set()
        text_lower = text.lower()
        order_or_referral_terms = ["ordered", "order", "referral", "schedule", "pending", "send out", "sent to", "requisition", "recommended"]

        # 1. Extract explicitly documented CPT procedure codes e.g. CPT 73560 or CPT 99214
        # Must verify if the procedure was actually PERFORMED vs merely ordered or referred out!
        explicit_cpts = re.finditer(r"\b(?:CPT|Procedure)[:\s]+([0-9]{5})\b", text, re.IGNORECASE)
        for match in explicit_cpts:
            cpt = match.group(1)
            if cpt.startswith("992"):
                continue  # E/M code handled separately
            
            # Check context before and after CPT for "ordered" / "referral"
            start_pos = match.start()
            end_pos = match.end()
            line_start = text.rfind("\n", 0, start_pos)
            line_start = 0 if line_start == -1 else line_start
            line_end = text.find("\n", end_pos)
            line_end = len(text) if line_end == -1 else line_end
            line_context = text_lower[line_start:line_end]

            if any(term in line_context for term in order_or_referral_terms):
                # Ordered test or external referral: Do NOT bill on clinic HCFA claim
                continue

            if cpt not in seen_cpts:
                seen_cpts.add(cpt)
                procedures.append({"code": cpt, "charge": 95.00, "description": f"Documented procedure CPT {cpt}"})

        # 2. Keyword ontology procedures (e.g. cryotherapy, joint injection)
        for keyword, (cpt, charge, desc) in self.PROCEDURE_ONTOLOGY.items():
            if keyword in text_lower and cpt not in seen_cpts:
                idx = text_lower.find(keyword)
                context = text_lower[max(0, idx - 40):min(len(text_lower), idx + len(keyword) + 40)]
                if any(term in context for term in order_or_referral_terms):
                    continue  # Ordered/scheduled for future, not performed today

                seen_cpts.add(cpt)
                procedures.append({"code": cpt, "charge": charge, "description": desc})

        return procedures

    def _score_em_level(self, note: ClinicalNoteParsed, num_diagnoses: int) -> Tuple[str, float]:
        # 1. Check if clinician explicitly documented an E/M code
        all_text = f"{note.chief_complaint} {note.history_of_present_illness} {note.assessment_and_plan}"
        explicit_em = re.search(r"\b(992[01][2-5])\b", all_text)
        if explicit_em:
            code = explicit_em.group(1)
            rates = {
                "99212": 75.00, "99213": 115.00, "99214": 175.00, "99215": 230.00,
                "99202": 95.00, "99203": 140.00, "99204": 210.00, "99205": 275.00
            }
            return code, rates.get(code, 175.00)

        text = note.assessment_and_plan.lower()
        has_rx_management = any(w in text for w in ["prescription", "prescribed", "refilled", "started on", "medication", "ibuprofen", "tid", "bid", "mg"])
        time_spent = note.time_spent_minutes

        if time_spent >= 40:
            return "99215", 230.00
        elif (num_diagnoses >= 2 and has_rx_management) or (time_spent >= 30):
            return "99214", 175.00
        elif num_diagnoses >= 1 or (time_spent >= 20):
            return "99213", 115.00
        else:
            return "99212", 75.00
