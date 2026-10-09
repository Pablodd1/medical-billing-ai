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
        # Modifier -25 should only be appended if a separately identifiable procedure was actually performed
        if extracted_procedures:
            em_modifiers.append("25")

        service_lines.append(CPTServiceLine(
            code=em_code,
            modifiers=em_modifiers,
            charge=em_charge,
            units=1,
            dx_pointers=[1, 2] if len(extracted_diagnoses) >= 2 else [1]
        ))

        for proc in extracted_procedures:
            # Map procedure to first relevant diagnosis pointer or primary pointer 1
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

    def _extract_diagnoses(self, text: str) -> List[DiagnosisCode]:
        diagnoses: List[DiagnosisCode] = []
        seen_codes = set()
        pointer = 1

        # 1. First priority: Extract explicitly documented ICD-10 codes in note e.g. M23.201, I10, L57.0
        # Standard ICD-10-CM format: Letter followed by 2 digits, dot, and up to 4 alphanumeric chars
        explicit_codes = re.findall(r"\b([A-TV-Z][0-9]{2}(?:\.[0-9A-Z]{1,4})?)\b", text)
        for code in explicit_codes:
            # Filter out non-ICD false positives (e.g. CPT numbers, dates, times)
            if re.match(r"^[A-Z][0-9]{2}(\.[0-9A-Z]{1,4})?$", code) and code not in seen_codes:
                seen_codes.add(code)
                diagnoses.append(DiagnosisCode(
                    code=code,
                    description=f"Documented clinical diagnosis {code}",
                    pointer=pointer,
                    is_primary=(pointer == 1)
                ))
                pointer += 1

        # 2. Second priority: Keyword ontology matching with basic negation checking
        text_lower = text.lower()
        negation_terms = ["denies", "no history of", "negative for", "without", "ruled out", "no signs of"]

        for keyword, (code, desc) in self.DIAGNOSIS_ONTOLOGY.items():
            if keyword in text_lower and code not in seen_codes:
                # Check for negation in the 40 characters preceding keyword
                idx = text_lower.find(keyword)
                context = text_lower[max(0, idx - 40):idx]
                if any(neg in context for neg in negation_terms):
                    continue  # Negated condition, do not code

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

        # 1. Extract explicitly documented CPT procedure codes e.g. CPT 73560 or CPT 99214
        explicit_cpts = re.findall(r"\b(?:CPT|Procedure)[:\s]+([0-9]{5})\b", text, re.IGNORECASE)
        for cpt in explicit_cpts:
            if cpt.startswith("992"):
                continue  # E/M code handled separately
            if cpt not in seen_cpts:
                seen_cpts.add(cpt)
                procedures.append({"code": cpt, "charge": 95.00, "description": f"Documented procedure CPT {cpt}"})

        # 2. Keyword ontology procedures
        text_lower = text.lower()
        for keyword, (cpt, charge, desc) in self.PROCEDURE_ONTOLOGY.items():
            if keyword in text_lower and cpt not in seen_cpts:
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
