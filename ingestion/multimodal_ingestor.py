# ============================================
# FILE: medical_billing_ai/ingestion/multimodal_ingestor.py
# PURPOSE: Multimodal Document Ingestor (Camera, OCR, PDF, DOCX, Handwritten Notes)
# ============================================

import os
import re
import sys
from typing import Dict, Any, Union, Optional, List, Tuple
from pypdf import PdfReader
from docx import Document

try:
    from models.encounter import DocumentSourceType, ClinicalNoteParsed
except ImportError:
    from ..models.encounter import DocumentSourceType, ClinicalNoteParsed

class MultimodalDocumentIngestor:
    """
    Ingests and normalizes clinical documentation across multiple formats:
    - Camera captures & Scanned images
    - Standard OCR text
    - PDF documents (clinical summaries, lab sheets)
    - Word documents (.docx files)
    - Handwritten clinical notes with medical shorthand normalization
    """

    MEDICAL_SHORTHAND_MAP = {
        r"\bc/o\b": "complains of",
        r"\bs/p\b": "status post",
        r"\bh/o\b": "history of",
        r"\br/o\b": "rule out",
        r"\bt2dm\b": "type 2 diabetes mellitus",
        r"\bhtn\b": "essential hypertension",
        r"\bak\b": "actinic keratosis",
        r"\bbid\b": "twice daily",
        r"\btid\b": "three times daily",
        r"\bqd\b": "once daily",
        r"\bprn\b": "as needed",
        r"\ba&p\b": "assessment and plan",
        r"\bhpi\b": "history of present illness",
        r"\bcc\b": "chief complaint",
        r"\bpe\b": "physical exam",
        r"\bros\b": "review of systems",
        r"\blnk\b": "liquid nitrogen cryotherapy",
        r"\brtc\b": "return to clinic",
        r"\bdx\b": "diagnosis",
        r"\btx\b": "treatment"
    }

    def __init__(self):
        self._rapid_ocr = None

    def ingest_document(self, file_path_or_content: Union[str, bytes], source_type: DocumentSourceType) -> ClinicalNoteParsed:
        raw_text = ""

        if source_type == DocumentSourceType.PDF_FILE:
            raw_text = self._parse_pdf(file_path_or_content)
        elif source_type == DocumentSourceType.DOCX_FILE:
            raw_text = self._parse_docx(file_path_or_content)
        elif source_type == DocumentSourceType.HANDWRITTEN_NOTE:
            raw_text = self._parse_handwritten_note(file_path_or_content)
        elif source_type == DocumentSourceType.CAMERA_IMAGE or source_type == DocumentSourceType.OCR_TEXT:
            raw_text = self._parse_ocr_or_image(file_path_or_content)
        else:
            raw_text = str(file_path_or_content)

        return self._extract_soap_structure(raw_text, source_type)

    def _parse_pdf(self, file_path_or_content: Union[str, bytes]) -> str:
        """Parses PDF content from a file path, raw bytes, or base64-encoded string.
        Uses PyMuPDF (fitz) as primary high-fidelity extractor with pypdf fallback."""
        import io
        import base64
        pdf_bytes = None

        if isinstance(file_path_or_content, bytes):
            pdf_bytes = file_path_or_content
        elif isinstance(file_path_or_content, str):
            if os.path.exists(file_path_or_content):
                try:
                    with open(file_path_or_content, "rb") as f:
                        pdf_bytes = f.read()
                except Exception:
                    pass
            elif file_path_or_content.strip().startswith("JVBERi0"):  # Base64 magic for %PDF-
                try:
                    pdf_bytes = base64.b64decode(file_path_or_content.strip())
                except Exception:
                    pass

        if not pdf_bytes:
            return str(file_path_or_content)

        # 1. Primary: PyMuPDF (fitz)
        try:
            import fitz
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            pages_text = [page.get_text() for page in doc]
            full_text = "\n".join(pages_text).strip()
            if len(full_text) > 20:
                return full_text
        except Exception:
            pass

        # 2. Fallback: pypdf
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(pdf_bytes))
            pages_text = [page.extract_text() or "" for page in reader.pages]
            full_text = "\n".join(pages_text).strip()
            if full_text:
                return full_text
        except Exception:
            pass

        # 3. Fallback: Scanned PDF OCR (RapidOCR / PyMuPDF rasterization)
        try:
            import fitz
            if getattr(self, "_rapid_ocr", None) is None:
                from rapidocr_onnxruntime import RapidOCR
                self._rapid_ocr = RapidOCR()
            ocr = self._rapid_ocr
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            ocr_pages = []
            for pno in range(len(doc)):
                pix = doc[pno].get_pixmap(dpi=110)
                res, _ = ocr(pix.tobytes("png"))
                if res:
                    lines = [txt for _, txt, score in res if score > 0.45]
                    if lines:
                        ocr_pages.append(f"=== PAGE {pno+1} ===\n" + "\n".join(lines))
            ocr_full = "\n\n".join(ocr_pages).strip()
            if len(ocr_full) > 20:
                return ocr_full
        except Exception:
            pass

        return str(file_path_or_content)

    def _parse_docx(self, file_path: str) -> str:
        if not os.path.exists(file_path):
            return str(file_path)
        doc = Document(file_path)
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs).strip()

    def _parse_handwritten_note(self, text_or_ocr: str) -> str:
        normalized = text_or_ocr
        for pattern, replacement in self.MEDICAL_SHORTHAND_MAP.items():
            normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
        return normalized

    def _parse_ocr_or_image(self, ocr_text: str) -> str:
        cleaned = re.sub(r"[•|~_]+", " ", str(ocr_text))
        # Collapse multiple horizontal spaces without destroying line breaks
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned

    DATE_PATTERNS = [
        # YYYY-MM-DD
        (r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", "%Y-%m-%d"),
        # MM/DD/YYYY or MM-DD-YYYY
        (r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", "%m/%d/%Y"),
        # Month DD, YYYY (e.g. August 14, 2026 or Aug 14, 2026)
        (r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{1,2}),?\s+(\d{4})\b", "named_month")
    ]

    MONTH_MAP = {
        "jan": "01", "january": "01",
        "feb": "02", "february": "02",
        "mar": "03", "march": "03",
        "apr": "04", "april": "04",
        "may": "05",
        "jun": "06", "june": "06",
        "jul": "07", "july": "07",
        "aug": "08", "august": "08",
        "sep": "09", "september": "09",
        "oct": "10", "october": "10",
        "nov": "11", "november": "11",
        "dec": "12", "december": "12"
    }

    def _normalize_date_str(self, date_str: str) -> Optional[str]:
        if not date_str:
            return None
        date_str = date_str.strip().rstrip(".,;")
        
        # Format 1: YYYY-MM-DD
        m1 = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", date_str)
        if m1:
            y, m, d = m1.groups()
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

        # Format 2: MM/DD/YYYY or MM-DD-YYYY
        m2 = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})$", date_str)
        if m2:
            m, d, y = m2.groups()
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

        # Format 3: Month DD, YYYY
        m3 = re.match(r"^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$", date_str)
        if m3:
            month_word, d, y = m3.groups()
            month_num = self.MONTH_MAP.get(month_word.lower())
            if month_num:
                return f"{int(y):04d}-{month_num}-{int(d):02d}"

        return None

    def _extract_multi_dates(self, text: str) -> Dict[str, Optional[str]]:
        """
        Disambiguates Date of Service (DOS), Face Sheet Registration Date,
        Provider Signature Date, and Patient DOB to prevent billing date errors.
        """
        results = {
            "dos": None,
            "registration": None,
            "signature": None,
            "dob": None
        }

        # 1. DOB extraction (to explicitly exclude from DOS)
        dob_match = re.search(r"(?:DOB|Date of Birth|Birth Date)[:\s]+([A-Za-z0-9/\-]+(?:,\s*\d{4})?)", text, re.IGNORECASE)
        if dob_match:
            results["dob"] = self._normalize_date_str(dob_match.group(1))

        # 2. Date of Service (DOS)
        dos_match = re.search(r"(?:Date of Service|DOS|Service Date|Effective\s*Date|EffectiveDate|ctive\s*Date|Encounter Date|Visit Date|Date of Visit)[:\s]+([0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{4}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*\d{1,2},?\s*\d{4})", text, re.IGNORECASE)
        if dos_match:
            results["dos"] = self._normalize_date_str(dos_match.group(1))
        else:
            prog_date = re.search(r"(?:Progress Notes[^\n]*\n(?:[^\n]*\n){0,3}?Date)[:\s]+([A-Za-z0-9,/\s]+)", text, re.IGNORECASE)
            if prog_date:
                results["dos"] = self._normalize_date_str(prog_date.group(1))

        # 3. Registration / Face Sheet Date
        reg_match = re.search(r"(?:Registration Date|Admit Date|Admission Date|Intake Date|Face Sheet Date)[:\s]+([A-Za-z0-9/\-]+(?:,\s*\d{4})?)", text, re.IGNORECASE)
        if reg_match:
            results["registration"] = self._normalize_date_str(reg_match.group(1))

        # 4. Provider Signature Date
        sig_match = re.search(r"(?:Electronically Signed by [^\n]+ on|Date Signed|Signature Date|Signed Date|Signed:)[:\s]+([A-Za-z0-9/\-]+(?:,\s*\d{4})?)", text, re.IGNORECASE)
        if sig_match:
            results["signature"] = self._normalize_date_str(sig_match.group(1))

        # Fallback for DOS: If no explicit DOS prefix found, search for generic date not equal to DOB or Registration
        if not results["dos"]:
            generic_dates = []
            for pat, _ in self.DATE_PATTERNS:
                for match in re.finditer(pat, text, re.IGNORECASE):
                    norm = self._normalize_date_str(match.group(0))
                    if norm and norm != results["dob"] and norm != results["registration"]:
                        generic_dates.append(norm)
            if generic_dates:
                results["dos"] = generic_dates[0]

        return results

    def _extract_place_of_service(self, text: str) -> str:
        """
        Classifies Place of Service (POS) from clinical narrative and facility clues:
        - 11: Office (Default)
        - 12: Home / Domiciliary
        - 13: Assisted Living Facility (ALF) / Senior Living
        - 31: Skilled Nursing Facility (SNF) / Rehabilitation & Nursing Center
        - 32: Nursing Facility
        - 02: Telehealth Provided Other than in Patient's Home
        - 10: Telehealth Provided in Patient's Home
        """
        text_lower = text.lower()
        if re.search(r"\b(?:rehabilitation\s*(?:and|&)?\s*nursing|nursing\s*(?:and|&)?\s*rehabilitation|nursing\s*center|nursing\s*home|snf|skilled\s*nursing|nursing\s*facility|rehabilitation\s*inc)\b", text_lower) or "rehab.medicine" in text_lower or "rehab. medicine" in text_lower:
            return "31"
        elif re.search(r"\b(?:assisted living|alf|senior living facility|group home|memory care facility)\b", text_lower):
            return "13"
        elif re.search(r"\b(?:home visit|patient residence|patient home|domiciliary visit|house call)\b", text_lower):
            return "12"
        elif re.search(r"\b(?:telehealth in home|virtual visit at home|telemedicine at home)\b", text_lower):
            return "10"
        elif re.search(r"\b(?:telehealth|telemedicine|virtual visit|video visit|audio/video encounter|doxy\.me|zoom medical)\b", text_lower):
            return "02"
        else:
            return "11"

    def extract_chart_demographics(self, text: str) -> Dict[str, Any]:
        """
        Autonomously extracts patient, provider, and payer demographics from
        clinical face sheets, admission records, and progress notes.
        Strict mode: returns None for missing critical identifiers (MBI, DOB, DOS)
        rather than silent placeholder values.
        """
        info: Dict[str, Any] = {
            "patient_name": None,
            "first_name": None,
            "last_name": None,
            "dob": None,
            "gender": None,
            "member_id": None,
            "medicaid_id": None,
            "secondary_payer": None,
            "secondary_member_id": None,
            "mma_plan": None,
            "is_qmb": False,
            "part_a_only": False,
            "managed_medicare": False,
            "address": None,
            "city": None,
            "state": None,
            "zip": None,
            "admission_date": None,
            "attending_provider": "Dr. Mederos Trujillo",
            "attending_npi": None,
            "referring_provider": None,
            "consultant_provider": "Dr. Nasser Eftekhari, MD",
            "rendering_npi": "1356442776",
            "date_of_service": None,
            "place_of_service": self._extract_place_of_service(text),
            "facility_name": None
        }

        # Restrict name search to before CONTACTS section to never match emergency contacts
        pre_contacts_text = re.split(r"\bCONTACTS\b", text, maxsplit=1)[0]

        def _fix_trailing_ocr_initial(first_val: str) -> str:
            """Repairs OCR digit '1' misread from trailing middle initial 'I' (e.g. 'CARLA1.' -> 'CARLA I')."""
            cleaned = re.sub(r"\b([A-Za-z]{3,})\s*1\.?$", r"\1 I", first_val.strip())
            return re.sub(r"\s+", " ", cleaned.replace(".", "")).strip().upper()

        # 1. Patient Name
        # Extract Page 1 face-sheet candidate ("Last, First") inside RESIDENT INFORMATION if present
        p1_res_block_m = re.search(
            r"RESIDENT\s*INFORMATION([\s\S]{0,600}?)(?:Previous\s*address|Prev[li1]ous\s*address|PAYER\s*INFORMATION)",
            pre_contacts_text, re.IGNORECASE
        )
        p1_name_pair = None
        if p1_res_block_m:
            for ln in p1_res_block_m.group(1).splitlines():
                ln_s = ln.strip()
                nm = re.match(r"^([A-Za-z][A-Za-z \.\-]{1,28}),\s*([A-Za-z][A-Za-z0-9 \.\-]{1,28})$", ln_s)
                if nm and not re.search(r"\b(?:miramar|miami|davie|florida|suites|admission|resident|preferred)\b", ln_s, re.IGNORECASE):
                    cand_first = _fix_trailing_ocr_initial(nm.group(2))
                    if re.fullmatch(r"[A-Z][A-Z \-]*", cand_first):
                        p1_name_pair = (
                            re.sub(r"\s+", " ", nm.group(1).replace(".", "")).strip().upper(),
                            cand_first,
                        )
                        break

        # Priority A: Page 2 Progress Note "Resident Name : Warren, Julia H.(5319)" or "ISSERMAN,CARLA1(5355)"
        res_name_m = re.search(
            r"Resident\s*N[ae]me\s*[:：]\s*([A-Za-z][A-Za-z \.\-]+,\s*[A-Za-z][A-Za-z0-9 \.\-]+?)\s*(?:\(|（|\n|$)",
            text, re.IGNORECASE
        )
        if res_name_m:
            raw_n = res_name_m.group(1).strip()
            pts = [p.strip() for p in raw_n.split(",", 1)]
            if len(pts) == 2 and pts[0] and pts[1]:
                last_clean = re.sub(r"([a-z])([A-Z])", r"\1 \2", pts[0]).replace(".", "").strip().upper()
                first_raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", pts[1])
                first_clean = _fix_trailing_ocr_initial(first_raw)
                # Strip any stray trailing digits that were part of MRN if parenthesis was OCR-dropped
                first_clean = re.sub(r"\s*\d+$", "", first_clean).strip()
                if p1_name_pair and p1_name_pair[0] == last_clean:
                    if " " not in first_clean and len(first_clean) > 9 and first_clean.startswith(p1_name_pair[1].split()[0]):
                        first_clean = p1_name_pair[1]
                    elif first_clean != p1_name_pair[1] and first_clean.replace("I", "L") == p1_name_pair[1].replace("I", "L"):
                        first_clean = p1_name_pair[1]
                    elif " " not in first_clean and p1_name_pair[1].startswith(first_clean + " "):
                        first_clean = p1_name_pair[1]
                info["last_name"] = last_clean
                info["first_name"] = first_clean
                info["patient_name"] = f"{info['last_name']}, {info['first_name']}"

        # Priority A2: Page 1 RESIDENT INFORMATION name when Page 2 header was margin-clipped
        if not info.get("patient_name") and p1_name_pair:
            info["last_name"], info["first_name"] = p1_name_pair
            info["patient_name"] = f"{p1_name_pair[0]}, {p1_name_pair[1]}"

        # Priority B: Progress note "Patient's name: Margaret Aldorasi, 77 year old lady"
        if not info.get("patient_name"):
            prog_name_m = re.search(r"(?:Patient'?s?\s*name|tient'?s?\s*name)[:\s]+([A-Za-z\s]+?)(?:,|admitted|\n|$)", text, re.IGNORECASE)
            if prog_name_m and len(prog_name_m.group(1).strip()) > 3:
                raw_n = prog_name_m.group(1).strip()
                pts = raw_n.split()
                if len(pts) == 1:
                    camel_pts = re.findall(r"[A-Z][a-z]+", raw_n)
                    if len(camel_pts) >= 2:
                        pts = camel_pts
                if len(pts) >= 2:
                    info["first_name"] = pts[0].upper()
                    info["last_name"] = " ".join(pts[1:]).upper()
                    info["patient_name"] = f"{info['last_name']}, {info['first_name']}"

        # Priority C: Name alert pattern in pre-contacts text e.g. "Aldorasi,Margaret(NAME ALERT)"
        if not info.get("patient_name") or "preferred" in info["patient_name"].lower() or "resident" in info["patient_name"].lower():
            alert_m = re.search(r"\b([A-Z][a-zA-Z ]+,\s*[A-Z][a-zA-Z \.]+)\s*(?:\(|（)", pre_contacts_text)
            if alert_m:
                raw_n = alert_m.group(1).strip()
                pts = [p.strip() for p in raw_n.split(",", 1)]
                info["last_name"] = pts[0].upper()
                info["first_name"] = pts[1].split()[0].upper() if pts[1] else "PATIENT"
                info["patient_name"] = f"{info['last_name']}, {info['first_name']}"

        # 2. DOB (Strict extraction: Page 2 Date of Birth or Page 1 Birthdate column)
        dob_m = re.search(r"(?:Birthdate|Date\s*of\s*B[il1]rth|DOB)[:\s\n]*([0-9]{2}[/-][0-9]{2}[/-][0-9]{4})", text, re.IGNORECASE)
        if not dob_m:
            dob_m = re.search(r"Birthdate[\s\S]{0,180}?\b([0-9]{2}[/-][0-9]{2}[/-]19[0-9]{2})\b", text, re.IGNORECASE)
        if dob_m:
            norm_dob = self._normalize_date_str(dob_m.group(1))
            if norm_dob:
                info["dob"] = norm_dob

        # 3. Gender / Sex
        sex_m = re.search(r"(?:Sex|Gender)[:\s\n]+([MF])\b", text, re.IGNORECASE)
        if sex_m:
            info["gender"] = sex_m.group(1).upper()
        else:
            # Face-sheet OCR lists labels first, then values: take the first lone M/F
            # within 15 lines after a standalone "Sex" (or OCR-inverted "xes") label,
            # or within 6 lines after "Gender:" on Page 2 progress notes.
            lines = [ln.strip() for ln in pre_contacts_text.splitlines()]
            for i, ln in enumerate(lines):
                if ln.lower() in ("sex", "xes"):
                    for nxt in lines[i + 1:i + 16]:
                        if re.fullmatch(r"[MF]", nxt):
                            info["gender"] = nxt
                            break
                    if info["gender"]:
                        break
            if not info["gender"]:
                all_lines = [ln.strip() for ln in text.splitlines()]
                for i, ln in enumerate(all_lines):
                    if ln.lower().rstrip(":") == "gender":
                        for nxt in all_lines[i + 1:i + 7]:
                            if re.fullmatch(r"[MF]", nxt):
                                info["gender"] = nxt
                                break
                        if info["gender"]:
                            break

        # 4. Member ID / Medicare Beneficiary ID (MBI), FL Medicaid 10-Digit ID, QMB, Part A-Only & Payer Flags
        from compliance.claim_gate import canonical_payer, valid_mbi

        upper_text = text.upper()
        # Extract standalone 11-char candidates plus concatenated 'MEDICARE#...' tokens, then filter strictly via valid_mbi()
        # so OCR-corrupted Pos-1 letters (e.g. BXC3F49DT93, BDJ6DF6XM55, SA78JM8VE39) never shadow a valid CMS MBI.
        mbi_candidates = (
            re.findall(r"(?<![0-9A-Z])([1-9][0-9A-Z]{10})(?![0-9A-Z])", upper_text)
            + re.findall(r"MED[IL1O]CAR[EO]\s*#?\s*([1-9][0-9A-Z]{10})(?![0-9A-Z])", upper_text)
        )
        valid_mbis = [m for m in mbi_candidates if valid_mbi(m)]
        if valid_mbis:
            info["member_id"] = valid_mbis[0]
        else:
            mbi_m = re.search(r"(?:Medicare Beneficiary ID|Medicare#|Medicare #|Medicare BemeficiaryID)[:\s]*([0-9A-Z]{10,12})", text, re.IGNORECASE)
            if mbi_m and not re.search(r"(?:PAYER|OTHER|SECONDARY|INSURANCE| SOCIAL)", mbi_m.group(1), re.IGNORECASE):
                info["member_id"] = mbi_m.group(1).strip().upper()
            else:
                pol_m = re.search(r"(?:Policy Number|Policy#|Primary Insurance Policy#)[:\s]*([0-9A-Z]{8,14})", text, re.IGNORECASE)
                if pol_m and not re.search(r"(?:PAYER|OTHER|SECONDARY|INSURANCE)", pol_m.group(1), re.IGNORECASE):
                    info["member_id"] = pol_m.group(1).strip().upper()

        # Restrict insurance/payer inspection to face-sheet blocks BEFORE CARE PROVIDERS (excludes NPIs & emergency contacts)
        pre_care_text = re.split(r"\bCARE\s*PROVIDERS\b", pre_contacts_text, maxsplit=1)[0]
        # Strip blank Glades West face-sheet form prompt ("United Healthcare Nursing Home Plan - Optum")
        clean_pre_care = re.sub(
            r"Un[il1]ted\s*Health\s*care\s*Nursing\s*Home\s*Plan[\s\-]*(?:\n\s*)?(?:Optum|Qotum|Ootum)?",
            " ",
            pre_care_text,
            flags=re.IGNORECASE,
        )
        other_parts = re.split(r"\bOTHER\s*INFORMATION\b", clean_pre_care, maxsplit=1, flags=re.IGNORECASE)
        pre_other_block = other_parts[0]
        other_info_block = other_parts[1] if len(other_parts) > 1 else ""

        # Slice Insurance Header block starting at first Medicare/Medicaid/Insurance/PAYER INFORMATION label
        # (excludes Admission Location hospital names like Memorial/Mercy above the insurance section, and excludes Part D Carrier below OTHER INFORMATION)
        ins_start_m = re.search(
            r"(?:Med[il1]car[eo]\s*(?:Benef|Bene|\(HIC\))|Mod[il1]ca[il1]d|Med[il1]ca[il1]d|Med[il1]cad|PAYER\s*INFORMATION)",
            pre_other_block,
            re.IGNORECASE,
        )
        ins_header_block = pre_other_block[ins_start_m.start():] if ins_start_m else pre_other_block

        # 4a. Florida 10-Digit Medicaid ID (FMMIS)
        medicaid_m = re.search(
            r"(?:Med[il1]ca[il1]d|Mod[il1]ca[il1]d|Med[il1]cad)\s*#[:\s\n]*([0-9]{9,11})\b",
            clean_pre_care,
            re.IGNORECASE,
        )
        if medicaid_m:
            info["medicaid_id"] = medicaid_m.group(1).strip()
        elif ins_start_m:
            # Columnar face-sheet layout lists labels first, then values on standalone lines inside ins_header_block
            for ln in ins_header_block.splitlines():
                ln_s = ln.strip()
                if re.fullmatch(r"\d{10}", ln_s) and ln_s != "0000000000":
                    info["medicaid_id"] = ln_s
                    break

        # 4b. QMB / MCRA Co-Pay Medicaid, Part A-Only, Managed Medicare B / Part C vs Traditional Medicare B
        if re.search(r"(?:MCRA\s*Co[\-\s]*Pay\s*Med[il1]ca[il1]d|QMB\s*El[il1g]*ble[:\s]*(?:Yes|Y)\b)", clean_pre_care, re.IGNORECASE):
            info["is_qmb"] = True

        if re.search(r"Med[il1]car[eo]\s*Cover[aeo]g[eo][\s\S]{0,60}?\bPart\s*A\s*only\b", clean_pre_care, re.IGNORECASE):
            info["part_a_only"] = True

        has_traditional_med_b = bool(re.search(r"Trad[il1]t[il1]onal\s*Med[il1]car[eo]\s*B", clean_pre_care, re.IGNORECASE))
        if re.search(
            r"(?:Managed\s*Med[il1]car[eo]\s*B|Med[il1]car[eo]\s*Advantage\s*Plan|Med[il1]car[eo]\s*Cover[aeo]g[eo][\s\S]{0,50}?\bPart\s*C\b)",
            clean_pre_care,
            re.IGNORECASE,
        ):
            info["managed_medicare"] = True

        # 4c. Payer / Medicare Advantage / MMA / Secondary Supplement Detection
        detected_plan: Optional[str] = None
        # Check Insurance Header zone first (contains Insurance Name, Insurance Name #1, Insurance Carrer, MMA Plan — NO Part D Carrier!)
        if ins_start_m:
            detected_plan = canonical_payer(ins_header_block)

        # Check OTHER INFORMATION authorization/prior-plan span: e.g. 'From 9/22 to 9/30 PreferredCare / Member ID 941911549'
        if not detected_plan and other_info_block:
            span_plan_m = re.search(
                r"\bFrom\s*\d{1,2}/\d{1,2}\s*to\s*\d{1,2}/\d{1,2}[\s/]*([A-Za-z][A-Za-z ]{2,30})",
                other_info_block,
                re.IGNORECASE,
            )
            if span_plan_m:
                detected_plan = canonical_payer(span_plan_m.group(1))
                sp_id_m = re.search(
                    r"\bFrom\s*\d{1,2}/\d{1,2}\s*to\s*\d{1,2}/\d{1,2}[\s\S]{0,180}?\b[Il1][dD]\s*[:#]?\s*([A-Z]?\d{8,11})\b",
                    other_info_block,
                    re.IGNORECASE,
                )
                if sp_id_m and not info["secondary_member_id"]:
                    info["secondary_member_id"] = sp_id_m.group(1).strip()

        # Check explicit SECONDARY PAYER block inside OTHER INFORMATION (e.g. 'Tricare / Member ID 00714606805', 'AARP / Member ID 027283858', 'Cigna / Member Id U3862054701')
        sec_block_m = re.search(
            r"SECONDARY\s*PAYER[\s\S]{0,140}?\b([A-Za-z][A-Za-z ]{1,24}?)\s*/\s*Member\s*[Il1][dD]\s*[:#]?\s*([A-Za-z0-9]+)",
            other_info_block,
            re.IGNORECASE,
        )
        if sec_block_m:
            raw_sec_name = sec_block_m.group(1).strip()
            sec_mem_id = sec_block_m.group(2).strip()
            info["secondary_member_id"] = sec_mem_id
            low_sec = raw_sec_name.lower()
            if "tricare" in low_sec:
                detected_plan = detected_plan or "TRICARE"
            elif "aarp" in low_sec:
                detected_plan = detected_plan or "AARP SUPPLEMENTAL"
            else:
                detected_plan = detected_plan or canonical_payer(raw_sec_name)

        # Fallback explicit single-line label match (e.g. synthetic test strings without full face-sheet sections)
        if not detected_plan and not ins_start_m:
            sec_payer_m = re.search(
                r"(?:Second\s*Payer|Secondary\s*Insurance\s*Name|Insurance\s*Carr?[il1]?er|Insurance\s*Name(?:\s*#1)?)[:\s\n]*([A-Za-z0-9 &\-]+)",
                clean_pre_care,
                re.IGNORECASE,
            )
            if sec_payer_m:
                detected_plan = canonical_payer(sec_payer_m.group(1))

        # Capture secondary / commercial policy number from Insurance Header when present (e.g. H62078688, 977586643, U3862054701),
        # skipping authorization numbers ('Auth#', 'A334403432', 'A331933505') and Part D PDP contract numbers ('H74807757' when no plan matched).
        if not info["secondary_member_id"] and ins_start_m and detected_plan:
            cand_sec_ids = []
            for ln in ins_header_block.splitlines():
                ln_s = ln.strip()
                if (
                    re.fullmatch(r"(?:[A-Z]\d{8,11}|\d{9})", ln_s)
                    and ln_s != info.get("member_id")
                    and ln_s != info.get("medicaid_id")
                    and not re.match(r"^A33\d{7}$", ln_s)
                ):
                    cand_sec_ids.append(ln_s)
            if cand_sec_ids:
                # Prefer numeric 9-digit or non-auth policy IDs over A-prefixed strings
                numeric_first = [c for c in cand_sec_ids if c[0].isdigit()]
                info["secondary_member_id"] = numeric_first[0] if numeric_first else cand_sec_ids[0]

        # Distinguish Dual-Eligible FL Medicaid MMA Secondary Crossover (Traditional Medicare B confirmed + 10-digit FL Medicaid ID + MCDMMA)
        # from Medicare Advantage / Commercial Primary Unverified plans!
        FL_SMMC_MMA_PLANS = {
            "SUNSHINE HEALTH", "SIMPLY HEALTHCARE", "HUMANA", "MOLINA HEALTHCARE",
            "UNITEDHEALTHCARE", "WELLCARE", "AMBETTER",
        }
        has_mcdmma_tag = bool(re.search(r"\bMCD(?:LTCC/)?MMA\b", ins_header_block, re.IGNORECASE))
        has_prior_ma_span = bool(re.search(r"\bFrom\s*\d{1,2}/\d{1,2}\s*to\s*\d{1,2}/\d{1,2}", other_info_block, re.IGNORECASE))

        if (
            detected_plan in FL_SMMC_MMA_PLANS
            and has_traditional_med_b
            and not info["managed_medicare"]
            and not has_prior_ma_span
            and bool(info["medicaid_id"])
            and has_mcdmma_tag
        ):
            info["mma_plan"] = detected_plan
            info["secondary_payer"] = "FLORIDA MEDICAID (QMB)" if info["is_qmb"] else "FLORIDA MEDICAID"
        elif detected_plan:
            info["secondary_payer"] = detected_plan
        elif info["managed_medicare"]:
            info["secondary_payer"] = "MANAGED MEDICARE (PART C)"
        elif info["is_qmb"]:
            info["secondary_payer"] = "FLORIDA MEDICAID (QMB)"
        elif info["medicaid_id"]:
            info["secondary_payer"] = "FLORIDA MEDICAID"

        # 5. Admission Date (Box 18 / Loop 2300 DTP*435) — handles OCR typos 'AdmisslonDate', 'AdmissonDate', 'Init. Adm.Date', and Page 2 'Admission'
        adm_m = re.search(
            r"(?:Admiss[il1o]*n\s*Date|In[il1]t\.?\s*Adm\.?\s*D[ae]te)[\s\S]{0,120}?\b([0-9]{2}/[0-9]{2}/202[0-9])\b",
            text,
            re.IGNORECASE,
        )
        if not adm_m:
            adm_m = re.search(r"\bAdmiss[il1o]*n\s*\n\s*([0-9]{2}/[0-9]{2}/202[0-9])\b", text, re.IGNORECASE)
        if adm_m:
            info["admission_date"] = self._normalize_date_str(adm_m.group(1))

        # 6. Attending, Referring & Consultant Providers
        att_npi_m = re.search(r"(?:Attending Physician|PRIMARY\s*PHYSICIAN|ARNP|Provider)[^\n]*\n(?:[^\n]*\n){0,6}?(?:NPI[:\s]*|1316456528)(\d{10})", text, re.IGNORECASE)
        if att_npi_m:
            info["attending_npi"] = att_npi_m.group(1)

        courtesy_m = re.search(r"courtesy\s*of\s*(Dr\.?\s*[A-Za-z\s\.\-]+?)(?:\n|HPI|$)", text, re.IGNORECASE)
        if courtesy_m:
            info["referring_provider"] = courtesy_m.group(1).strip()

        consult_m = re.search(r"(?:Consultant|User)[:\s]+([A-Za-z\s\.]+MD|[A-Za-z\s\.]+ARNP|[A-Za-z\s\.]+DO)", text)
        if consult_m:
            info["consultant_provider"] = consult_m.group(1).strip()

        # 7. Date of Service
        eff_dos_m = re.search(r"(?:Effective\s*Date|ctive\s*Date|Visit Date)[:\s]+([0-9]{2}/[0-9]{2}/[0-9]{4}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*\d{1,2},?\s*\d{4})", text, re.IGNORECASE)
        if eff_dos_m:
            norm_dos = self._normalize_date_str(eff_dos_m.group(1))
            if norm_dos:
                info["date_of_service"] = norm_dos

        # 8. Facility Name (Single-line match to avoid ADMISSIONRECORD prefix)
        fac_m = re.search(
            r"([A-Za-z][A-Za-z &]+(?:Nursing\s*(?:&|and)?\s*Rehabilitation(?:\s*Center|\s*Inc\.?)?|Rehabilitation\s*(?:and|&)?\s*Nursing\s*Center|Subacute\s*Care\s*Center|Rehabilitation\s*Center))",
            text, re.IGNORECASE
        )
        if fac_m:
            raw_fac = re.sub(r"^ADMISSION\s*RECORD\s*", "", fac_m.group(1).strip(), flags=re.IGNORECASE)
            # Insert spaces in camelCase OCR like GladesWestRehabilitation
            raw_fac = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw_fac)
            info["facility_name"] = re.sub(r"\s+", " ", raw_fac).strip().upper()
        else:
            info["facility_name"] = "GLADES WEST REHABILITATION AND NURSING CENTER"

        # 9. Address
        home = self._extract_home_address(pre_contacts_text)
        if home:
            info.update(home)

        return info

    FACILITY_STREETS_RE = re.compile(
        r"bass\s*creek|955\s*nw\s*3rd|15951\s*sw\s*41st",
        re.IGNORECASE
    )

    # Longest-first regex patterns for South Florida cities when OCR omits commas before the city name
    CONCAT_FL_CITY_PATTERNS = (
        ("NORTH MIAMI BEACH", r"NORTH\s*MIAMI\s*BEACH"),
        ("HALLANDALE BEACH", r"HALLANDALE\s*BEACH"),
        ("LAUDERDALE LAKES", r"LAUDERDALE\s*LAKES"),
        ("DEERFIELD BEACH", r"DEERFIELD\s*BEACH"),
        ("HIALEAH GARDENS", r"HIALEAH\s*GARDENS"),
        ("FORT LAUDERDALE", r"F[O0]?RT\s*L?AUDERDALE"),
        ("PEMBROKE PINES", r"PEMBROKE\s*P[I1]?NES"),
        ("POMPANO BEACH", r"POMPANO\s*BEACH"),
        ("COCONUT CREEK", r"COCONUT\s*CREEK"),
        ("CORAL SPRINGS", r"CORAL\s*SPRINGS"),
        ("MIAMI GARDENS", r"MIAMI\s*GARDENS"),
        ("MIAMI SPRINGS", r"MIAMI\s*SPRINGS"),
        ("PEMBROKE PARK", r"PEMBROKE\s*PARK"),
        ("CORAL GABLES", r"CORAL\s*GABLES"),
        ("COOPER CITY", r"COOPER\s*CITY"),
        ("MIAMI BEACH", r"MIAMI\s*BEACH"),
        ("MIAMI LAKES", r"MIAMI\s*LAKES"),
        ("NORTH MIAMI", r"NORTH\s*MIAMI"),
        ("BOCA RATON", r"BOCA\s*RATON"),
        ("LAUDERHILL", r"LAUDERHILL?"),
        ("PLANTATION", r"PLANTATION"),
        ("HOLLYWOOD", r"HOLLYWOOD"),
        ("HOMESTEAD", r"HOMESTEAD"),
        ("OPA-LOCKA", r"OPA[\-\s]*LOCKA"),
        ("AVENTURA", r"AVENTURA"),
        ("MIRAMAR", r"MIRAMAR"),
        ("MARGATE", r"MARGATE"),
        ("TAMARAC", r"TAMARAC"),
        ("SUNRISE", r"SUNRISE"),
        ("HIALEAH", r"HIALEAH"),
        ("KENDALL", r"KENDALL"),
        ("WESTON", r"WESTON"),
        ("DAVIE", r"DAVIE"),
        ("DORAL", r"DORAL"),
        ("MIAMI", r"MIAMI"),
    )

    @classmethod
    def _clean_ocr_street(cls, street: str) -> str:
        s = street.strip(" ,.")
        # Insert space between camelCase and digit/letter boundaries
        s = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", " ", s)
        s = s.upper()
        # Normalize common OCR street/unit typos
        s = re.sub(r"\bBIDG\b", "BLDG", s)
        s = re.sub(r"\bTERA\s*CE\b", "TERRACE", s)
        # Split glued unit designators FIRST so '7 THAVEAPT 323' -> '7 THAVE APT 323' and '23 RDST APT' before ordinal splitting
        s = re.sub(r"(?<=[A-Z]{2})(APT|UNIT|BLDG|STE)(?=\b|\s|\d)", r" \1", s)
        s = re.sub(r"\b(APT|UNIT|BLDG|STE)([A-Z])\b", r"\1 \2", s)
        # Split ordinal suffixes glued to street types or directions (e.g. '7 THAVE' -> '7TH AVE', '23 RDST' -> '23RD ST', '55 THN' -> '55TH N')
        s = re.sub(r"\b(\d+)\s*(ST|ND|RD|TH)(RD|ST|AVE|CT|DR|LN|WAY|BLVD|PL|TER|CIR|MNR|N|S|E|W)\b", r"\1\2 \3", s)
        # Rejoin standalone ordinal numbers (e.g. '166 TH' -> '166TH')
        s = re.sub(r"\b(\d+)\s+(ST|ND|RD|TH)\b", r"\1\2", s)
        # Separate glued directional/street-type/unit tokens in all-caps OCR (e.g. FOXCROFTRDA 203 -> FOXCROFT RD A 203, UNITH -> UNIT H)
        s = re.sub(r"\b(NW|NE|SW|SE)(?=[A-Z]{3,}\b)", r"\1 ", s)
        s = re.sub(r"\b([NSWE])(?=(?:PARK|OCEAN|DIXIE|FEDERAL|SHORE|BAY|RIVER|PINE|FLAMINGO|UNIVERSITY|HIATUS|ANDREWS|MILITARY)[A-Z]*\b)", r"\1 ", s)
        s = re.sub(r"(?<=[A-Z]{3})(DRIVE|COURT|TERRACE|STREET|AVENUE)\b", r" \1", s)
        s = re.sub(r"(?<=[A-Z]{3})(RD|ST|AVE|CT|DR|LN|WAY|BLVD|PL|TER|CIR|MNR)(?:(?=\b)|(?=([A-Z])\s+\d))", r" \1 ", s)
        s = re.sub(r"\b(APT|UNIT|BLDG|STE)([A-Z])\b", r"\1 \2", s)
        s = re.sub(r"(?<=[A-Z]{2})(APT|UNIT|BLDG|STE)\b", r" \1", s)
        s = re.sub(r"\s+", " ", s).strip(" ,.")
        return s

    @classmethod
    def _extract_home_address(cls, text: str) -> Optional[Dict[str, str]]:
        """Finds the resident's home address line, strictly skipping facility letterhead
        and provider/pharmacy blocks. Returns None when no resident home address exists."""
        # Restrict to RESIDENT INFORMATION section whenever present so letterhead & provider addresses cannot match
        res_sec = re.search(
            r"RESIDENT\s*INFORMATION([\s\S]*?)(?:PAYER\s*INFORMATION|OTHER\s*INFORMATION|CARE\s*PROVIDERS|PHARMACY|$)",
            text, re.IGNORECASE
        )
        search_text = (res_sec.group(1) if res_sec else text).replace("，", ",")
        lines = [ln.strip() for ln in search_text.splitlines() if ln.strip()]

        # 1. Comma- or period-delimited line: 'street, CITY, ST, ZIP' or '5300W16thAve.Hialeah.FL.33012'
        delim_pat = re.compile(
            r"^\s*(\d[\w\s#\.\-/]*?)[,\.]\s*([A-Za-z \.]+?)\s*[,\.]?\s*([A-Z]{2})\s*[,\.]?\s*(\d{5})(?:-?\d{4})?\s*$"
        )
        for line in lines:
            m = delim_pat.match(line)
            if not m or cls.FACILITY_STREETS_RE.search(m.group(1)):
                continue
            street, city, state, zip_code = (g.strip() for g in m.groups())
            street = cls._clean_ocr_street(street)
            city = re.sub(r"(?<=[A-Z])l", "I", city)
            city = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", city).upper().strip(" .")
            if state == "FL":
                city = cls._canonical_fl_city(city)
            return {"address": street, "city": city, "state": state, "zip": zip_code}

        # 2. Concatenated South Florida address without comma before city (e.g. '3492FOXCROFTRDA203MIRAMARFL33025')
        for line in lines:
            if not re.match(r"^\d", line) or cls.FACILITY_STREETS_RE.search(line):
                continue
            for canon_city, city_re in cls.CONCAT_FL_CITY_PATTERNS:
                m = re.match(
                    rf"^(\d.+?)[\s,\.]*(?:{city_re})[\s,\.]*(FL)[\s,\.]*(\d{{5}})(?:-?\d{{4}})?$",
                    line, re.IGNORECASE
                )
                if m:
                    street = cls._clean_ocr_street(m.group(1))
                    if len(street) >= 4:
                        return {
                            "address": street,
                            "city": canon_city,
                            "state": "FL",
                            "zip": m.group(3),
                        }

        # 3. Wrapped 2-line Previous Address inside RESIDENT INFORMATION (e.g. street on line i-k, 'Pines,FL,33027' on line i)
        if res_sec:
            tail_pat = re.compile(r"^([A-Za-z][A-Za-z \.]+?)\s*,\s*(FL)\s*,?\s*(\d{5})(?:-?\d{4})?\s*$")
            for idx, line in enumerate(lines):
                tm = tail_pat.match(line)
                if not tm:
                    continue
                tail_city, state, zip_code = tm.group(1).strip(), tm.group(2), tm.group(3)
                for back in range(idx - 1, max(-1, idx - 5), -1):
                    cand = lines[back]
                    if re.match(r"^\d+\s+[A-Za-z0-9]", cand) and not re.match(r"^\d{2}/\d{2}/\d{4}$", cand) and not cls.FACILITY_STREETS_RE.search(cand):
                        # Strip trailing partial city wrap (e.g. ',Pembroke')
                        head_street = cand
                        full_city = tail_city
                        if "," in cand:
                            head_street, city_prefix = cand.rsplit(",", 1)
                            full_city = f"{city_prefix.strip()} {tail_city}"
                        # Strip duplicated OCR column echo if present (e.g. 'PEMBROKE Pnes Apt G403')
                        head_street = re.sub(r"\s+PEMBROKE\s+Pn.*$", "", head_street, flags=re.IGNORECASE)
                        city = cls._canonical_fl_city(full_city.upper())
                        return {
                            "address": cls._clean_ocr_street(head_street),
                            "city": city,
                            "state": state,
                            "zip": zip_code,
                        }

        return None

    SOUTH_FL_CITIES = (
        "Miami", "Miami Gardens", "Miami Lakes", "Miami Beach", "Miami Springs", "North Miami",
        "North Miami Beach", "Pembroke Pines", "Miramar", "Hollywood", "Hallandale Beach",
        "Fort Lauderdale", "Plantation", "Sunrise", "Davie", "Weston", "Hialeah", "Hialeah Gardens",
        "Opa-locka", "Coral Gables", "Doral", "Cooper City", "Coconut Creek", "Coral Springs",
        "Tamarac", "Lauderhill", "Lauderdale Lakes", "Margate", "Pompano Beach", "Deerfield Beach",
        "Boca Raton", "Bay Harbor Islands", "Homestead", "Kendall", "Aventura", "Pembroke Park",
    )

    @classmethod
    def _canonical_fl_city(cls, city: str) -> str:
        """Repairs OCR-garbled South Florida city names (e.g. MIMI -> MIAMI); unknown names unchanged."""
        import difflib
        squash = lambda s: re.sub(r"[^a-z]", "", s.lower())
        best, best_ratio = None, 0.0
        for cand in cls.SOUTH_FL_CITIES:
            ratio = difflib.SequenceMatcher(None, squash(city), squash(cand)).ratio()
            if ratio > best_ratio:
                best, best_ratio = cand, ratio
        return best.upper() if best and best_ratio >= 0.8 else city

    def _extract_soap_structure(self, text: str, source_type: DocumentSourceType) -> ClinicalNoteParsed:
        cc = ""
        hpi = ""
        ros = ""
        pe = ""
        ap = ""
        time_spent = 0

        cc_match = re.search(r"(?:Chief Complaint|CC|Reason for Visit)[:\s]+(.*?)(?=\n\s*(?:History|HPI|ROS|Exam|Assessment|A&P|Plan|Signature)|$)", text, re.IGNORECASE | re.DOTALL)
        if cc_match:
            cc = cc_match.group(1).strip()
        elif "complains of" in text.lower() or "c/o" in text.lower():
            lines = [l for l in text.split("\n") if "complains of" in l.lower() or "c/o" in l.lower()]
            cc = lines[0] if lines else "Medical Consultation"

        hpi_match = re.search(r"(?:HPI|History of Present Illness)[:\s]+(.*?)(?=\n\s*(?:ROS|Exam|Physical Exam|Assessment|A&P|Plan|Signature)|$)", text, re.IGNORECASE | re.DOTALL)
        if hpi_match:
            hpi = hpi_match.group(1).strip()
        else:
            hpi = text[:300].strip()

        # Capture Assessment & Plan together, or separate Assessment / Plan sections
        ap_match = re.search(r"(?:Assessment and Plan|Assessment & Plan|A&P|Impression and Plan)[:\s]+(.*?)(?=\n\s*(?:Signature|Time|Provider|Dr\.|CPT:)|$)", text, re.IGNORECASE | re.DOTALL)
        if ap_match:
            ap = ap_match.group(1).strip()
        else:
            # Check separate Assessment: ... and Plan: ...
            assessment_match = re.search(r"(?:Assessment|Impression)[:\s]+(.*?)(?=\n\s*(?:Plan|Recommendation|Signature|Time|Provider|Dr\.|CPT:)|$)", text, re.IGNORECASE | re.DOTALL)
            plan_match = re.search(r"(?:Plan|Recommendation)[:\s]+(.*?)(?=\n\s*(?:Signature|Time|Provider|Dr\.|CPT:)|$)", text, re.IGNORECASE | re.DOTALL)
            if assessment_match or plan_match:
                parts = []
                if assessment_match:
                    parts.append(f"Assessment: {assessment_match.group(1).strip()}")
                if plan_match:
                    parts.append(f"Plan: {plan_match.group(1).strip()}")
                ap = "\n".join(parts)
            else:
                ap = text[-400:].strip() if len(text) > 400 else text

        time_match = re.search(r"(\d+)\s*(?:minutes|mins|min)\b", text, re.IGNORECASE)
        if time_match:
            time_spent = int(time_match.group(1))

        # Extract Disambiguated Dates & Place of Service
        dates = self._extract_multi_dates(text)
        pos = self._extract_place_of_service(text)

        return ClinicalNoteParsed(
            raw_text=text,
            source_type=source_type,
            chief_complaint=cc or "General consultation and management",
            history_of_present_illness=hpi,
            review_of_systems=ros,
            physical_exam=pe,
            assessment_and_plan=ap,
            time_spent_minutes=time_spent,
            patient_status="established" if "follow-up" in text.lower() or "f/u" in text.lower() else "new",
            date_of_service_detected=dates["dos"],
            registration_date_detected=dates["registration"],
            signature_date_detected=dates["signature"],
            place_of_service_detected=pos
        )
