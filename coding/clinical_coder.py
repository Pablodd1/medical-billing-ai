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
        full_text = f"{note.chief_complaint} {note.history_of_present_illness} {note.assessment_and_plan}".lower()

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
            service_lines.append(CPTServiceLine(
                code=proc["code"],
                modifiers=[],
                charge=proc["charge"],
                units=1,
                dx_pointers=[len(extracted_diagnoses)] if extracted_diagnoses else [1]
            ))

        total_charge = sum(line.charge * line.units for line in service_lines)

        return BillingDetails(
            place_of_service=pos,
            total_charge=round(total_charge, 2),
            diagnosis_codes=extracted_diagnoses,
            cpt_codes=service_lines
        )

    def _extract_diagnoses(self, text: str) -> List[DiagnosisCode]:
        diagnoses = []
        pointer = 1
        for keyword, (code, desc) in self.DIAGNOSIS_ONTOLOGY.items():
            if keyword in text:
                diagnoses.append(DiagnosisCode(
                    code=code,
                    description=desc,
                    pointer=pointer,
                    is_primary=(pointer == 1)
                ))
                pointer += 1
        if not diagnoses:
            diagnoses.append(DiagnosisCode(code="Z00.00", description="General adult medical examination without abnormal findings", pointer=1, is_primary=True))
        return diagnoses

    def _extract_procedures(self, text: str) -> List[Dict[str, Any]]:
        procedures = []
        for keyword, (cpt, charge, desc) in self.PROCEDURE_ONTOLOGY.items():
            if keyword in text:
                procedures.append({"code": cpt, "charge": charge, "description": desc})
        return procedures

    def _score_em_level(self, note: ClinicalNoteParsed, num_diagnoses: int) -> Tuple[str, float]:
        text = note.assessment_and_plan.lower()
        has_rx_management = any(w in text for w in ["prescription", "prescribed", "refilled", "started on", "medication"])
        time_spent = note.time_spent_minutes

        if (num_diagnoses >= 2 and has_rx_management) or (time_spent >= 30):
            return "99214", 175.00
        elif num_diagnoses >= 1 or (time_spent >= 20):
            return "99213", 115.00
        else:
            return "99212", 75.00
