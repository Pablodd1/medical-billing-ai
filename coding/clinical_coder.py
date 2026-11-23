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
        "diabetic neuropathy": ("E11.40", "Type 2 diabetes mellitus with diabetic neuropathy, unspecified"),
        "diabetic polyneuropathy": ("E11.42", "Type 2 diabetes mellitus with diabetic polyneuropathy"),
        "diabetes mellitus type 2": ("E11.9", "Type 2 diabetes mellitus without complications"),
        "diabetes": ("E11.9", "Type 2 diabetes mellitus without complications"),
        "t2dm": ("E11.9", "Type 2 diabetes mellitus without complications"),
        "dm ii": ("E11.9", "Type 2 diabetes mellitus without complications"),
        "dm2": ("E11.9", "Type 2 diabetes mellitus without complications"),
        "hypertension": ("I10", "Essential (primary) hypertension"),
        "htn": ("I10", "Essential (primary) hypertension"),
        "hyperlipidemia": ("E78.5", "Hyperlipidemia, unspecified"),
        "hld": ("E78.5", "Hyperlipidemia, unspecified"),
        "dyslipidemia": ("E78.5", "Hyperlipidemia, unspecified"),
        "actinic keratosis": ("L57.0", "Actinic keratosis"),
        "seborrheic keratosis": ("L82.0", "Inflamed seborrheic keratosis"),
        "osteoarthritis of left hip": ("M16.12", "Unilateral primary osteoarthritis, left hip"),
        "osteoarthritis of the left hip": ("M16.12", "Unilateral primary osteoarthritis, left hip"),
        "left hip osteoarthritis": ("M16.12", "Unilateral primary osteoarthritis, left hip"),
        "osteoarthritis left hip": ("M16.12", "Unilateral primary osteoarthritis, left hip"),
        "osteoarthritis of right hip": ("M16.11", "Unilateral primary osteoarthritis, right hip"),
        "osteoarthritis of the right hip": ("M16.11", "Unilateral primary osteoarthritis, right hip"),
        "right hip osteoarthritis": ("M16.11", "Unilateral primary osteoarthritis, right hip"),
        "osteoarthritis right hip": ("M16.11", "Unilateral primary osteoarthritis, right hip"),
        "bilateral hip osteoarthritis": ("M16.0", "Bilateral primary osteoarthritis of hip"),
        "osteoarthritis of bilateral hips": ("M16.0", "Bilateral primary osteoarthritis of hip"),
        "osteoarthritis of both hips": ("M16.0", "Bilateral primary osteoarthritis of hip"),
        "osteoarthritis of hip": ("M16.9", "Osteoarthritis of hip, unspecified"),
        "osteoarthritis of the hip": ("M16.9", "Osteoarthritis of hip, unspecified"),
        "hip osteoarthritis": ("M16.9", "Osteoarthritis of hip, unspecified"),
        "osteoarthritis hip": ("M16.9", "Osteoarthritis of hip, unspecified"),
        "right knee osteoarthritis": ("M17.11", "Unilateral primary osteoarthritis, right knee"),
        "osteoarthritis of right knee": ("M17.11", "Unilateral primary osteoarthritis, right knee"),
        "osteoarthritis of the right knee": ("M17.11", "Unilateral primary osteoarthritis, right knee"),
        "osteoarthritis right knee": ("M17.11", "Unilateral primary osteoarthritis, right knee"),
        "left knee osteoarthritis": ("M17.12", "Unilateral primary osteoarthritis, left knee"),
        "osteoarthritis of left knee": ("M17.12", "Unilateral primary osteoarthritis, left knee"),
        "osteoarthritis of the left knee": ("M17.12", "Unilateral primary osteoarthritis, left knee"),
        "osteoarthritis left knee": ("M17.12", "Unilateral primary osteoarthritis, left knee"),
        "bilateral knee osteoarthritis": ("M17.0", "Bilateral primary osteoarthritis of knee"),
        "osteoarthritis of bilateral knees": ("M17.0", "Bilateral primary osteoarthritis of knee"),
        "osteoarthritis of both knees": ("M17.0", "Bilateral primary osteoarthritis of knee"),
        "osteoarthritis of knee": ("M17.9", "Osteoarthritis of knee, unspecified"),
        "osteoarthritis of the knee": ("M17.9", "Osteoarthritis of knee, unspecified"),
        "osteoarthritis knee": ("M17.9", "Osteoarthritis of knee, unspecified"),
        "knee osteoarthritis": ("M17.9", "Osteoarthritis of knee, unspecified"),
        "osteoarthritis": ("M19.90", "Unspecified osteoarthritis, unspecified site"),
        "advance oa": ("M19.90", "Unspecified osteoarthritis, unspecified site"),
        "knee pain": ("M25.561", "Pain in right knee"),
        "lumbar radiculopathy": ("M54.16", "Radiculopathy, lumbar region"),
        "low back pain": ("M54.50", "Low back pain, unspecified"),
        "cervicalgia": ("M54.2", "Cervicalgia"),
        "rotator cuff tear": ("M75.101", "Unspecified rotator cuff tear or rupture of right shoulder"),
        "osteoporosis": ("M81.0", "Age-related osteoporosis without current pathological fracture"),
        "myalgia": ("M79.18", "Myalgia, other site"),
        "vitamin d deficiency": ("E55.9", "Vitamin D deficiency, unspecified"),
        "hypothyroidism": ("E03.9", "Hypothyroidism, unspecified"),
        "hylerlipidemia": ("E78.5", "Hyperlipidemia, unspecified"),
        "functional decline": ("M62.81", "Muscle weakness (generalized)"),
        "increased weakness": ("M62.81", "Muscle weakness (generalized)"),
        "gait instability": ("R26.89", "Other abnormalities of gait and mobility"),
        "gait difficulty": ("R26.89", "Other abnormalities of gait and mobility"),
        "unstable gait": ("R26.89", "Other abnormalities of gait and mobility"),
        "unsteady gait": ("R26.89", "Other abnormalities of gait and mobility"),
        "difficulty walking": ("R26.2", "Difficulty in walking, not elsewhere classified"),
        "abnormality of gait": ("R26.9", "Unspecified abnormalities of gait and mobility"),
        "generalized weakness": ("M62.81", "Muscle weakness (generalized)"),
        "muscle weakness": ("M62.81", "Muscle weakness (generalized)"),
        "debility": ("R53.81", "Other malaise"),
        "deconditioning": ("M62.81", "Muscle weakness (generalized)"),
        "fatigue": ("R53.83", "Other fatigue"),
        "gerd": ("K21.9", "Gastro-esophageal reflux disease without esophagitis"),
        "gastroesophageal reflux": ("K21.9", "Gastro-esophageal reflux disease without esophagitis"),
        "anemia": ("D64.9", "Anemia, unspecified"),
        "chronic kidney disease stage 3b": ("N18.32", "Chronic kidney disease, stage 3b"),
        "chronic kidney disease 3b": ("N18.32", "Chronic kidney disease, stage 3b"),
        "ckd stage 3b": ("N18.32", "Chronic kidney disease, stage 3b"),
        "ckd 3b": ("N18.32", "Chronic kidney disease, stage 3b"),
        "chronic kidney disease stage 3a": ("N18.31", "Chronic kidney disease, stage 3a"),
        "chronic kidney disease 3a": ("N18.31", "Chronic kidney disease, stage 3a"),
        "ckd stage 3a": ("N18.31", "Chronic kidney disease, stage 3a"),
        "ckd 3a": ("N18.31", "Chronic kidney disease, stage 3a"),
        "chronic kidney disease stage 3": ("N18.30", "Chronic kidney disease, stage 3 unspecified"),
        "chronic kidney disease 3": ("N18.30", "Chronic kidney disease, stage 3 unspecified"),
        "ckd stage 3": ("N18.30", "Chronic kidney disease, stage 3 unspecified"),
        "ckd 3": ("N18.30", "Chronic kidney disease, stage 3 unspecified"),
        "chronic kidney disease stage 4": ("N18.4", "Chronic kidney disease, stage 4 (severe)"),
        "chronic kidney disease 4": ("N18.4", "Chronic kidney disease, stage 4 (severe)"),
        "ckd stage 4": ("N18.4", "Chronic kidney disease, stage 4 (severe)"),
        "ckd 4": ("N18.4", "Chronic kidney disease, stage 4 (severe)"),
        "chronic kidney disease stage 5": ("N18.5", "Chronic kidney disease, stage 5"),
        "chronic kidney disease 5": ("N18.5", "Chronic kidney disease, stage 5"),
        "ckd stage 5": ("N18.5", "Chronic kidney disease, stage 5"),
        "ckd 5": ("N18.5", "Chronic kidney disease, stage 5"),
        "chronic kidney disease": ("N18.9", "Chronic kidney disease, unspecified"),
        "ckd": ("N18.9", "Chronic kidney disease, unspecified"),
        "acute kidney injury": ("N17.9", "Acute kidney failure, unspecified"),
        "aki": ("N17.9", "Acute kidney failure, unspecified"),
        "hyponatremia": ("E87.1", "Hypo-osmolality and hyponatremia"),
        "hypokalemia": ("E87.6", "Hypokalemia"),
        "hypercalcemia": ("E83.52", "Hypercalcemia"),
        "leukocytosis": ("D72.829", "Other elevated white blood cell count"),
        "dementia": ("F03.90", "Unspecified dementia, unspecified severity, without behavioral disturbance"),
        "alzheimer": ("G30.9", "Alzheimer's disease, unspecified"),
        "parkinson's disease with dyskinesia": ("G20.B1", "Parkinson's disease with dyskinesia, without mention of fluctuations"),
        "parkinson disease with dyskinesia": ("G20.B1", "Parkinson's disease with dyskinesia, without mention of fluctuations"),
        "parkinsonism": ("G20.C", "Parkinsonism, unspecified"),
        "parkinson": ("G20.A1", "Parkinson's disease without dyskinesia, without mention of fluctuations"),
        "congestive heart failure": ("I50.9", "Heart failure, unspecified"),
        "chf": ("I50.9", "Heart failure, unspecified"),
        "coronary artery disease": ("I25.10", "Atherosclerotic heart disease of native coronary artery"),
        "cad": ("I25.10", "Atherosclerotic heart disease of native coronary artery"),
        "pvd": ("I73.9", "Peripheral vascular disease, unspecified"),
        "peripheral vascular disease": ("I73.9", "Peripheral vascular disease, unspecified"),
        "atrial fibrillation": ("I48.91", "Unspecified atrial fibrillation"),
        "a-fib": ("I48.91", "Unspecified atrial fibrillation"),
        "afib": ("I48.91", "Unspecified atrial fibrillation"),
        "copd": ("J44.9", "Chronic obstructive pulmonary disease, unspecified"),
        "cellulitis right foot": ("L03.115", "Cellulitis of right lower limb"),
        "cellulitis left foot": ("L03.116", "Cellulitis of left lower limb"),
        "cellulitis of right lower": ("L03.115", "Cellulitis of right lower limb"),
        "cellulitis of left lower": ("L03.116", "Cellulitis of left lower limb"),
        "cellulitis": ("L03.90", "Cellulitis, unspecified"),
        "mrsa": ("B95.62", "Methicillin resistant Staphylococcus aureus infection"),
        "urinary tract infection": ("N39.0", "Urinary tract infection, site not specified"),
        "uti": ("N39.0", "Urinary tract infection, site not specified"),
        "constipation": ("K59.00", "Constipation, unspecified"),
        "depressive disorder": ("F32.A", "Depression, unspecified"),
        "depression": ("F32.A", "Depression, unspecified"),
        "anxiety": ("F41.9", "Anxiety disorder, unspecified"),
        "insomnia": ("G47.00", "Insomnia, unspecified"),
        "neuropathy": ("G62.9", "Polyneuropathy, unspecified"),
        "peripheral neuropathy": ("G62.9", "Polyneuropathy, unspecified"),
        "bph": ("N40.0", "Benign prostatic hyperplasia without lower urinary tract symptoms"),
        "obesity": ("E66.9", "Obesity, unspecified"),
        "morbid obesity": ("E66.01", "Morbid (severe) obesity due to excess calories"),
        "dysphagia": ("R13.10", "Dysphagia, unspecified"),
        "edema": ("R60.9", "Edema, unspecified"),
        "pressure ulcer": ("L89.90", "Pressure ulcer of unspecified site, unspecified stage"),
        "aftercare following joint replacement": ("Z47.1", "Aftercare following joint replacement surgery"),
        "total knee arthroplasty": ("Z47.1", "Aftercare following joint replacement surgery"),
        "total hip arthroplasty": ("Z47.1", "Aftercare following joint replacement surgery"),
        "orif": ("Z47.89", "Encounter for other orthopedic aftercare"),
    }

    PROCEDURE_ONTOLOGY = {
        "cryotherapy": ("17000", 120.00, "Destruction premalignant lesions; first lesion"),
        "cryosurgery": ("17000", 120.00, "Destruction premalignant lesions; first lesion"),
        "liquid nitrogen": ("17000", 120.00, "Destruction premalignant lesions; first lesion"),
        "shave biopsy": ("11102", 135.00, "Tangential biopsy of skin; single lesion"),
        "punch biopsy": ("11104", 165.00, "Punch biopsy of skin; single lesion"),
        "wound debridement": ("11042", 195.00, "Debridement, subcutaneous tissue; first 20 sq cm or less"),
        "joint injection": ("20610", 160.00, "Aspiration/injection intermediate or major joint"),
        "arthrocentesis": ("20610", 160.00, "Aspiration/injection intermediate or major joint"),
        "trigger point injection": ("20552", 115.00, "Injection(s); single or multiple trigger points, 1 or 2 muscle(s)"),
        "therapeutic exercise": ("97110", 72.00, "Therapeutic procedure, 1 or more areas, each 15 minutes; therapeutic exercises"),
        "manual therapy": ("97140", 68.00, "Manual therapy techniques, 1 or more regions, each 15 minutes"),
        "vitamin d test": ("82306", 65.00, "Vitamin D; 25 hydroxy, includes fraction(s)")
    }

    PROCEDURE_DX_AFFINITY: Dict[str, List[str]] = {
        "20610": ["M16.", "M17.", "M23.", "M25.5", "M19.90", "M", "S"],
        "20605": ["M16.", "M17.", "M23.", "M25.5", "M19.90", "M", "S"],
        "20552": ["M54.", "M79.", "M"],
        "20553": ["M54.", "M79.", "M"],
        "17000": ["L57.0", "L82.0", "L", "D", "C"],
        "17003": ["L57.0", "L82.0", "L", "D", "C"],
        "11042": ["L89.", "L97.", "L98.", "S", "L"],
        "11102": ["L", "D", "C"],
        "11104": ["L", "D", "C"],
        "82306": ["E55."],
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

        em_pointers = list(range(1, min(len(extracted_diagnoses), 4) + 1)) or [1]
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
            affinity_prefixes = self.PROCEDURE_DX_AFFINITY.get(proc_code)
            if affinity_prefixes:
                matched_ptr = None
                for pref in affinity_prefixes:
                    for d in extracted_diagnoses:
                        if d.code == pref or d.code.startswith(pref):
                            matched_ptr = d.pointer
                            break
                    if matched_ptr is not None:
                        break
                if matched_ptr is not None:
                    target_pointer = matched_ptr
            elif re.match(r"^(?:20[0-9]{3}|73[0-9]{3}|97[0-9]{3})$", proc_code):  # Musculoskeletal / joint / ortho / PT
                for d in extracted_diagnoses:
                    if d.code.startswith("M") or d.code.startswith("S"):
                        target_pointer = d.pointer
                        break
            elif re.match(r"^(?:11[0-9]{3}|17[0-9]{3})$", proc_code):  # Integumentary / dermatology / destruction / biopsy
                for d in extracted_diagnoses:
                    if d.code.startswith("L") or d.code.startswith("D") or d.code.startswith("C"):
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
        "M54.50": ["M54.16", "M51."],  # Low back pain suppressed by lumbar radiculopathy or disc disorder
        "R53.83": ["E55.", "E11.", "I10", "E03."]  # Fatigue suppressed if endocrine/chronic disease explains it
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
                # Reject lab ratios like "H/H 10.8/31.6" where "H31.6" is matched
                if m.start() > 0 and text[m.start() - 1] == "/":
                    continue
                if m.end() < len(text) and text[m.end()] == "/":
                    continue
                if re.search(r"\b(?:h/h|hgb|hct|wbc|plt|bun|cr)\b", prefix, re.IGNORECASE):
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

        # 2. Second priority: Keyword ontology matching with OCR typo normalization, clause negation,
        # historical exclusion for acute transient conditions, and ICD-10-CM specificity/combination guards.
        norm_text = re.sub(
            r"\bi?diabetes\s*mell?itus\s*(?:type\s*2|ii)\b|\bdiabetes\s*mell?itus\b",
            "diabetes mellitus type 2",
            text,
            flags=re.IGNORECASE,
        )
        norm_text = re.sub(r"\bdsteoporosis\b|(?m:^\s*steoporosis\b)", "osteoporosis", norm_text, flags=re.IGNORECASE)
        norm_text = re.sub(r"(?m:^\s*hronic\s+kidney\s+disease\b)", "chronic kidney disease", norm_text, flags=re.IGNORECASE)
        text_lower = norm_text.lower()

        acute_historical_re = re.compile(
            r"(?:^|\b)(?:s/p|status\s+post|h/o|history\s+of|resolved|recurrent)\s*(?:acute\s*)?$",
            re.IGNORECASE,
        )

        for keyword, (code, desc) in self.DIAGNOSIS_ONTOLOGY.items():
            if keyword in text_lower and code not in seen_codes:
                kw_matches = list(re.finditer(r"\b" + re.escape(keyword) + r"\b", text_lower))
                has_valid_kw = False
                for m in kw_matches:
                    prefix = self._extract_clause_prefix(norm_text, m.start())
                    if self.NEGATION_REGEX.search(prefix):
                        continue
                    # ICD-10-CM Section III / IV: Do not code historical ('s/p', 'history of', 'resolved') or HPI 'recurrent'
                    # mentions of acute transient conditions (AKI N17.9, acute UTI N39.0) as active acute diagnoses.
                    if code in ("N17.9", "N39.0") and acute_historical_re.search(prefix):
                        continue
                    has_valid_kw = True
                    break

                if not has_valid_kw:
                    continue

                # Specificity & Combination guards (ICD-10-CM Official Guidelines Section I.B.2 & I.B.9):
                # 1) Diabetes with manifestation (e.g. E11.40 diabetic neuropathy) suppresses uncomplicated E11.9 & standalone G62.9
                if code == "E11.9" and any(sc.startswith("E11.") and sc != "E11.9" for sc in seen_codes):
                    continue
                if code == "G62.9" and any(sc in ("E11.40", "E11.42") for sc in seen_codes):
                    continue
                # 2) Staged CKD (N18.30/N18.31/N18.32/N18.4/N18.5) suppresses unspecified CKD (N18.9)
                if code == "N18.9" and any(sc.startswith("N18.") and sc != "N18.9" for sc in seen_codes):
                    continue
                # 3) Osteoarthritis anatomical site/laterality specificity guard:
                # Do not emit unspecified hip (M16.9), unspecified knee (M17.9), or unspecified site (M19.90)
                # when a more specific anatomical/laterality OA diagnosis has already been extracted.
                if code == "M16.9" and any(sc.startswith("M16.") and sc != "M16.9" for sc in seen_codes):
                    continue
                if code == "M17.9" and any(sc.startswith("M17.") and sc != "M17.9" for sc in seen_codes):
                    continue
                if code == "M19.90" and any(
                    sc.startswith(("M15.", "M16.", "M17.", "M18.", "M19.")) and sc != "M19.90"
                    for sc in seen_codes
                ):
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
            if re.match(r"^(?:992[01][1-5]|9930[4-9]|99310|9934[1-5]|9934[7-9]|99350)$", cpt):
                continue  # E/M code handled separately in _score_em_level

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

    INITIAL_NF_VISIT_RE = re.compile(
        r"(?:consult(?:ation|ion)\s*report|"
        r"physiatr?ist\s*consult|pm&r\s*consultation|"
        r"\binitial\s+(?:nursing\s+facility|snf|evaluation|consult(?:ation)?|visit|assessment)\b|"
        r"\badmission\s+(?:evaluation|consult(?:ation)?|history\s+and\s+physical|h&p)\b|"
        r"evaluation\s*and\s*treatment\s*courtesy\s*of\s*dr)",
        re.IGNORECASE,
    )

    FOLLOWUP_NF_VISIT_RE = re.compile(
        r"\b(?:routine\s+follow[\s\-]*up|"
        r"follow[\s\-]*up\s+(?:visit|note|exam|evaluation|assessment|snf|nursing\s+facility)|"
        r"subsequent\s+(?:nursing\s+facility|snf|visit|care|evaluation|note)|"
        r"f/u\s+(?:visit|note|exam|evaluation))\b",
        re.IGNORECASE,
    )

    @classmethod
    def _is_initial_nursing_facility_visit(cls, note: Any) -> bool:
        """Determines whether a POS 31/32 encounter represents Initial Nursing Facility Care
        (CPT 99304-99306, including initial specialist consultations per CMS IOM Pub. 100-04,
        Ch. 12, § 30.6.13 / MLN MM6740) vs Subsequent Nursing Facility Care (CPT 99307-99310)."""
        explicit_type = str(getattr(note, "visit_type", "") or "").strip().lower()
        if explicit_type == "initial":
            return True
        if explicit_type in ("subsequent", "followup", "follow-up"):
            return False

        narrative = " ".join(
            str(getattr(note, attr, "") or "")
            for attr in ("chief_complaint", "history_of_present_illness", "assessment_and_plan", "raw_text")
        )
        if cls.FOLLOWUP_NF_VISIT_RE.search(narrative):
            return False
        if cls.INITIAL_NF_VISIT_RE.search(narrative):
            return True
        return False

    def _score_em_level(self, note: ClinicalNoteParsed, num_diagnoses: int) -> Tuple[str, float]:
        # 1. Check if clinician explicitly documented an E/M code
        all_text = (
            f"{getattr(note, 'chief_complaint', '')} "
            f"{getattr(note, 'history_of_present_illness', '')} "
            f"{getattr(note, 'assessment_and_plan', '')} "
            f"{getattr(note, 'raw_text', '')}"
        )
        explicit_em = re.search(r"\b(992[01][2-5]|9930[4-9]|99310|9934[1245]|9934[7-9]|99350)\b", all_text)
        if explicit_em:
            code = explicit_em.group(1)
            rates = {
                "99212": 75.00, "99213": 115.00, "99214": 175.00, "99215": 230.00,
                "99202": 95.00, "99203": 140.00, "99204": 210.00, "99205": 275.00,
                "99304": 125.00, "99305": 165.00, "99306": 215.00,
                "99307": 85.00, "99308": 115.00, "99309": 160.00, "99310": 225.00,
                "99341": 95.00, "99342": 135.00, "99344": 195.00, "99345": 245.00,
                "99347": 90.00, "99348": 130.00, "99349": 185.00, "99350": 240.00,
            }
            return code, rates.get(code, 175.00)

        # 2. Check Place of Service: Nursing Facility / SNF (POS 31/32)
        pos = getattr(note, "place_of_service_detected", None) or "11"
        is_nursing_facility = pos in ["31", "32"]

        text = (getattr(note, "assessment_and_plan", "") or "").lower()
        has_rx_management = any(w in text for w in ["prescription", "prescribed", "refilled", "started on", "medication", "ibuprofen", "tid", "bid", "mg"])
        time_spent = getattr(note, "time_spent_minutes", 0) or 0

        if is_nursing_facility:
            if self._is_initial_nursing_facility_visit(note):
                # Initial Nursing Facility Care (CPT 99304 - 99306), including Medicare specialist
                # initial consultations (CMS IOM Pub. 100-04, Ch. 12, § 30.6.13 / MLN MM6740; no Modifier -AI
                # for consulting specialists). Time thresholds: 99304 >= 25m, 99305 >= 35m, 99306 >= 45m.
                if time_spent >= 45:
                    return "99306", 215.00
                if time_spent >= 35:
                    return "99305", 165.00
                if time_spent >= 25:
                    return "99304", 125.00
                # Without documented time/High MDM, conservative heuristic caps at Moderate MDM (99305);
                # High MDM (99306) is never inferred from diagnosis count alone (EM_LEVEL_HEURISTIC).
                if num_diagnoses >= 2:
                    return "99305", 165.00
                return "99304", 125.00

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
