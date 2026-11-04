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
        "advance oa": ("M19.90", "Unspecified osteoarthritis, unspecified site"),
        "knee pain": ("M25.561", "Pain in right knee"),
        "vitamin d deficiency": ("E55.9", "Vitamin D deficiency, unspecified"),
        "fatigue": ("R53.83", "Other fatigue"),
        "hyperlipidemia": ("E78.5", "Hyperlipidemia, unspecified"),
        "hylerlipidemia": ("E78.5", "Hyperlipidemia, unspecified"),
        "functional decline": ("M62.81", "Muscle weakness (generalized)"),
        "increased weakness": ("M62.81", "Muscle weakness (generalized)"),
        "muscle weakness": ("M62.81", "Muscle weakness (generalized)"),
        "gait instability": ("R26.89", "Other abnormalities of gait and mobility"),
        "gait difficulty": ("R26.89", "Other abnormalities of gait and mobility"),
        "unstable gait": ("R26.89", "Other abnormalities of gait and mobility"),
        "difficulty walking": ("R26.2", "Difficulty in walking, not elsewhere classified"),
        "gerd": ("K21.9", "Gastro-esophageal reflux disease without esophagitis"),
        "gastroesophageal reflux": ("K21.9", "Gastro-esophageal reflux disease without esophagitis"),
        "anemia": ("D64.9", "Anemia, unspecified"),
        "obesity": ("E66.9", "Obesity, unspecified"),
        "aki": ("N17.9", "Acute kidney failure, unspecified"),
        "acute kidney injury": ("N17.9", "Acute kidney failure, unspecified"),
        "ckd": ("N18.9", "Chronic kidney disease, unspecified"),
        "chronic kidney disease": ("N18.9", "Chronic kidney disease, unspecified"),
        "hyponatremia": ("E87.1", "Hypo-osmolality and hyponatremia"),
        "hypokalemia": ("E87.6", "Hypokalemia"),
        "hypercalcemia": ("E83.52", "Hypercalcemia"),
        "leukocytosis": ("D72.829", "Other elevated white blood cell count"),
        "deconditioning": ("M62.81", "Muscle weakness (generalized)"),
        "dementia": ("F03.90", "Unspecified dementia, unspecified severity, without behavioral disturbance"),
        "alzheimer": ("G30.9", "Alzheimer's disease, unspecified"),
        "parkinson": ("G20", "Parkinson's disease"),
        "neuropathy": ("G62.9", "Polyneuropathy, unspecified"),
        "congestive heart failure": ("I50.9", "Heart failure, unspecified"),
        "chf": ("I50.9", "Heart failure, unspecified"),
        "coronary artery disease": ("I25.10", "Atherosclerotic heart disease of native coronary artery"),
        "cad": ("I25.10", "Atherosclerotic heart disease of native coronary artery"),
        "pvd": ("I73.9", "Peripheral vascular disease, unspecified"),
        "peripheral vascular disease": ("I73.9", "Peripheral vascular disease, unspecified"),
        "atrial fibrillation": ("I48.91", "Unspecified atrial fibrillation"),
        "afib": ("I48.91", "Unspecified atrial fibrillation"),
        "cellulitis right foot": ("L03.115", "Cellulitis of right lower limb"),
        "cellulitis left foot": ("L03.116", "Cellulitis of left lower limb"),
        "cellulitis": ("L03.90", "Cellulitis, unspecified"),
        "mrsa": ("B95.62", "Methicillin resistant Staphylococcus aureus infection"),
        "depressive disorder": ("F32.A", "Depression, unspecified"),
        "depression": ("F32.A", "Depression, unspecified"),
        "anxiety": ("F41.9", "Anxiety disorder, unspecified"),
        "insomnia": ("G47.00", "Insomnia, unspecified"),
        "total hip arthroplasty": ("Z47.1", "Aftercare following joint replacement surgery"),
        "total knee arthroplasty": ("Z47.1", "Aftercare following joint replacement surgery"),
        "orif": ("Z47.89", "Encounter for other orthopedic aftercare"),
        "hypothyroidism": ("E03.9", "Hypothyroidism, unspecified"),
        "copd": ("J44.9", "Chronic obstructive pulmonary disease, unspecified"),
        "bph": ("N40.0", "Benign prostatic hyperplasia without lower urinary tract symptoms"),
        "dysphagia": ("R13.10", "Dysphagia, unspecified"),
        "constipation": ("K59.00", "Constipation, unspecified")
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
        full_text = f"{note.chief_complaint} {note.history_of_present_illness} {note.assessment_and_plan} {note.raw_text}"

        extracted_diagnoses = self._extract_diagnoses(full_text)[:12]
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

        em_pointers = list(range(1, min(len(extracted_diagnoses), 4) + 1)) if extracted_diagnoses else [1]
        service_lines.append(CPTServiceLine(
            code=em_code,
            modifiers=em_modifiers,
            charge=em_charge,
            units=1,
            dx_pointers=em_pointers
        ))

        for proc in extracted_procedures:
            # Map procedure to primary supporting diagnosis pointer based on anatomical/specialty alignment (Box 24E / SV107)
            proc_code = str(proc.get("code", ""))
            target_pointer = 1
            if re.match(r"^(?:20[0-9]{3}|73[0-9]{3})$", proc_code):  # Musculoskeletal / joint / ortho
                for d in extracted_diagnoses:
                    if d.code.startswith("M") or d.code.startswith("S"):
                        target_pointer = d.pointer
                        break
            elif re.match(r"^17[0-9]{3}$", proc_code):  # Integumentary / dermatology / destruction
                for d in extracted_diagnoses:
                    if d.code.startswith("L") or d.code.startswith("D"):
                        target_pointer = d.pointer
                        break
            elif proc_code == "82306":  # Vitamin D lab
                for d in extracted_diagnoses:
                    if d.code.startswith("E55"):
                        target_pointer = d.pointer
                        break

            service_lines.append(CPTServiceLine(
                code=proc["code"],
                modifiers=[],
                charge=proc["charge"],
                units=1,
                dx_pointers=[target_pointer]
            ))

        total_charge = sum(line.charge * line.units for line in service_lines)

        return BillingDetails(
            place_of_service=pos,
            total_charge=round(total_charge, 2),
            diagnosis_codes=extracted_diagnoses,
            cpt_codes=service_lines
        )

    # Common non-diagnosis tokens that match letter+2 digits (e.g. B12 vitamin, D3, A1c, etc.)
    NON_DIAGNOSIS_TOKENS = {
        "B12", "D03", "A01", "D3", "TID", "BID", "QID", "PRN", "POS", "CPT", "NPI",
        "APRN", "MD", "DO", "PA", "PSA", "A1C", "CO2", "TSH", "CBC", "BMP", "CMP",
        "MRI", "CT", "EKG", "ECG", "EMG", "PFT", "IV", "IM", "SQ", "PO", "PR", "SL",
        "ROM", "WNL", "PERRLA", "HEENT", "NAD", "RR", "HR", "BP", "O2", "WT", "HT", "BMI"
    }

    # Spine / vertebral / disc levels (C1-C7, T1-T12, L1-L5, S1-S5) are anatomical levels, not ICD-10 codes
    SPINE_LEVEL_TOKENS = {
        f"{prefix}{num}"
        for prefix in ("C", "T", "L", "S")
        for num in range(1, 13)
    }

    # Standard billable complete 3-character ICD-10-CM codes without decimal
    VALID_3CHAR_COMPLETE_ICD10 = {
        "I10",  # Essential (primary) hypertension
        "A38",  # Scarlet fever
        "B01",  # Varicella without complication
        "B02",  # Zoster without complication
        "B05",  # Measles without complication
        "B06",  # Rubella without complication
        "B20",  # Human immunodeficiency virus [HIV] disease
        "J00",  # Acute nasopharyngitis [common cold]
        "R05",  # Cough
        "R10",  # Abdominal and pelvic pain
        "R50",  # Fever of other and unknown origin
        "R51",  # Headache
        "R53",  # Malaise and fatigue
    }

    NEGATION_REGEX = re.compile(
        r"\b(?:ruled out|r/o|negative for|denies(?: any)?|no history of|no evidence of|no signs of|no indication of|without (?:any )?(?:evidence|signs|indication) of)\b",
        re.IGNORECASE
    )

    ROOM_LOCATION_REGEX = re.compile(
        r"\b(?:room|rm|bed|suite|unit|floor|clinic)\s*$",
        re.IGNORECASE
    )

    ANATOMICAL_LEVEL_PREFIX_REGEX = re.compile(
        r"\b(?:at|level|vertebra|disc|spine|compression fracture at|fracture at)\s*$",
        re.IGNORECASE
    )

    ORDER_TERMS_REGEX = re.compile(
        r"\b(?:order(?:ed|ing)?|referral(?: for)?|pending|requisition|to be scheduled|schedule for future)\b",
        re.IGNORECASE
    )

    PERFORMED_TERMS_REGEX = re.compile(
        r"\b(?:performed|completed|administered|injected|applied|done|excised|aspirated)\b",
        re.IGNORECASE
    )

    # Symptom codes that must be suppressed if a definitive etiology / underlying condition is coded
    # per ICD-10-CM Official Guidelines Section I.B.5
    SYMPTOM_SUPPRESSION_MAP = {
        "M25.561": ["M23.", "M17."],  # Knee pain suppressed by internal derangement (M23) or osteoarthritis (M17)
        "M25.562": ["M23.", "M17."],
        "M25.569": ["M23.", "M17."],
        "R53.83": ["E55.", "E11.", "I10"]  # Fatigue suppressed if endocrine/chronic disease explains it
    }

    @staticmethod
    def _extract_clause_prefix(text: str, start_pos: int, lookback: int = 70) -> str:
        """Extracts the clause prefix preceding a match, bounded by sentence/section delimiters."""
        limit = max(0, start_pos - lookback)
        sub = text[limit:start_pos]
        delims = list(re.finditer(r"[\.\;\n\:\!\?]", sub))
        clause_start = limit + delims[-1].end() if delims else limit
        return text[clause_start:start_pos]

    def _extract_diagnoses(self, text: str) -> List[DiagnosisCode]:
        diagnoses: List[DiagnosisCode] = []
        seen_codes = set()
        pointer = 1

        # 1. Group occurrences of explicit ICD-10 patterns e.g. M23.201, I10, L57.0
        matches_by_code: Dict[str, List[re.Match]] = {}
        for m in re.finditer(r"\b([A-TV-Z][0-9]{2}(?:\.[0-9A-Z]{1,4})?)\b", text):
            raw_code = m.group(1).upper()
            matches_by_code.setdefault(raw_code, []).append(m)

        for code, matches in matches_by_code.items():
            if code in self.NON_DIAGNOSIS_TOKENS:
                continue

            # Spine / anatomical levels without decimal (e.g. T12, L4, C5) are not ICD codes
            if "." not in code and code in self.SPINE_LEVEL_TOKENS:
                continue

            # 3-character codes without dot must be recognized complete ICD-10 codes
            if "." not in code and code not in self.VALID_3CHAR_COMPLETE_ICD10:
                continue

            # Check if there is at least one non-negated, valid occurrence
            is_valid_occurrence = False
            for m in matches:
                prefix = self._extract_clause_prefix(text, m.start())
                # Reject if preceded by room or facility location
                if self.ROOM_LOCATION_REGEX.search(prefix):
                    continue
                # Reject if preceded by spine or anatomical level designator
                if self.ANATOMICAL_LEVEL_PREFIX_REGEX.search(prefix):
                    continue
                # Reject if preceded by vitamin
                if re.search(r"\b(?:vitamin|vit)\s*$", prefix, re.IGNORECASE):
                    continue
                # Reject if part of a lab ratio e.g. H/H 10.8/31.6
                suffix = text[m.end():m.end() + 4]
                if re.search(r"(?:H/|Hb|Hgb)\s*$", prefix, re.IGNORECASE) or suffix.startswith("/"):
                    continue
                # Reject if negated in the current clause
                if self.NEGATION_REGEX.search(prefix):
                    continue

                is_valid_occurrence = True
                break

            if is_valid_occurrence and code not in seen_codes:
                seen_codes.add(code)
                diagnoses.append(DiagnosisCode(
                    code=code,
                    description=f"Documented clinical diagnosis {code}",
                    pointer=pointer,
                    is_primary=(pointer == 1)
                ))
                pointer += 1

        # 2. Second priority: Keyword ontology matching with clause negation & symptom suppression
        text_lower = text.lower()
        for keyword, (code, desc) in self.DIAGNOSIS_ONTOLOGY.items():
            if keyword in text_lower and code not in seen_codes:
                kw_matches = list(re.finditer(r"\b" + re.escape(keyword) + r"\b", text_lower))
                has_valid_kw = False
                for m in kw_matches:
                    prefix = self._extract_clause_prefix(text, m.start())
                    if self.NEGATION_REGEX.search(prefix):
                        continue
                    has_valid_kw = True
                    break

                if not has_valid_kw:
                    continue

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

        # Note: If no diagnoses are documented, return empty list rather than fabricating Z00.00
        # (Tier 1 Scrutiny will flag for human review as un-coded visit)
        return diagnoses

    def _extract_procedures(self, text: str) -> List[Dict[str, Any]]:
        procedures: List[Dict[str, Any]] = []
        seen_cpts = set()
        text_lower = text.lower()

        # 1. Extract explicitly documented CPT procedure codes e.g. CPT 73560 or CPT 20610
        # Must verify if the procedure was actually PERFORMED vs merely ordered or referred out!
        explicit_cpts = re.finditer(r"\b(?:CPT|Procedure)[:\s]+([0-9]{5})\b", text, re.IGNORECASE)
        for match in explicit_cpts:
            cpt = match.group(1)
            if cpt.startswith("992"):
                continue  # E/M code handled separately

            clause_prefix = self._extract_clause_prefix(text, match.start(), lookback=60)
            after_context = text[match.end():min(len(text), match.end() + 40)]
            local_context = f"{clause_prefix} {cpt} {after_context}"

            is_performed = bool(self.PERFORMED_TERMS_REGEX.search(local_context))
            is_ordered = bool(self.ORDER_TERMS_REGEX.search(local_context))

            if is_ordered and not is_performed:
                # Ordered diagnostic test or external referral: Do NOT bill on clinic HCFA claim
                continue

            if cpt not in seen_cpts:
                seen_cpts.add(cpt)
                procedures.append({"code": cpt, "charge": 95.00, "description": f"Documented procedure CPT {cpt}"})

        # 2. Keyword ontology procedures (e.g. cryotherapy, joint injection)
        for keyword, (cpt, charge, desc) in self.PROCEDURE_ONTOLOGY.items():
            if keyword in text_lower and cpt not in seen_cpts:
                kw_matches = list(re.finditer(r"\b" + re.escape(keyword) + r"\b", text_lower))
                for m in kw_matches:
                    prefix = self._extract_clause_prefix(text, m.start(), lookback=50)
                    after = text_lower[m.end():min(len(text_lower), m.end() + 30)]
                    local_ctx = f"{prefix} {after}"

                    is_perf = bool(self.PERFORMED_TERMS_REGEX.search(local_ctx))
                    is_ord = bool(self.ORDER_TERMS_REGEX.search(local_ctx))

                    if is_ord and not is_perf:
                        continue  # Ordered/scheduled for future, not performed today

                    seen_cpts.add(cpt)
                    procedures.append({"code": cpt, "charge": charge, "description": desc})
                    break

        return procedures

    def _score_em_level(self, note: ClinicalNoteParsed, num_diagnoses: int) -> Tuple[str, float]:
        # 1. Check if clinician explicitly documented an E/M code
        all_text = f"{note.chief_complaint} {note.history_of_present_illness} {note.assessment_and_plan}"
        explicit_em = re.search(r"\b(992[01][2-5]|9930[4-9]|99310)\b", all_text)
        if explicit_em:
            code = explicit_em.group(1)
            rates = {
                "99212": 75.00, "99213": 115.00, "99214": 175.00, "99215": 230.00,
                "99202": 95.00, "99203": 140.00, "99204": 210.00, "99205": 275.00,
                "99304": 125.00, "99305": 165.00, "99306": 215.00,
                "99307": 85.00, "99308": 115.00, "99309": 160.00, "99310": 225.00
            }
            return code, rates.get(code, 175.00)

        # 2. Check Place of Service: Nursing Facility / SNF (POS 31/32)
        pos = note.place_of_service_detected or "11"
        is_nursing_facility = pos in ["31", "32"]

        text = note.assessment_and_plan.lower()
        has_rx_management = any(w in text for w in ["prescription", "prescribed", "refilled", "started on", "medication", "ibuprofen", "tid", "bid", "mg"])
        time_spent = note.time_spent_minutes

        if is_nursing_facility:
            # Subsequent Nursing Facility Care (CPT 99307 - 99310), CPT 2023+ time thresholds:
            # 99307 >= 10 min, 99308 >= 20, 99309 >= 30, 99310 >= 45 (total time on the date of service).
            if time_spent >= 45:
                return "99310", 225.00
            if time_spent >= 30:
                return "99309", 160.00
            if time_spent >= 20:
                return "99308", 115.00
            if time_spent >= 10:
                return "99307", 85.00
            # No documented time: diagnosis count is only a conservative suggestion and is
            # capped at 99309. High MDM (99310) must be supported by documented time or MDM
            # and is never inferred from diagnosis count; a coder must confirm (EM_LEVEL_HEURISTIC).
            if num_diagnoses >= 2:
                return "99309", 160.00
            elif num_diagnoses >= 1:
                return "99308", 115.00
            else:
                return "99307", 85.00

        # Office or Outpatient Care (CPT 99212 - 99215)
        if time_spent >= 40:
            return "99215", 230.00
        elif (num_diagnoses >= 2 and has_rx_management) or (time_spent >= 30):
            return "99214", 175.00
        elif num_diagnoses >= 1 or (time_spent >= 20):
            return "99213", 115.00
        else:
            return "99212", 75.00
