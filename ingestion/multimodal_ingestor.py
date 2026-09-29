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

    def _parse_pdf(self, file_path: str) -> str:
        if not os.path.exists(file_path):
            return str(file_path)
        reader = PdfReader(file_path)
        text_pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(text_pages).strip()

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
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
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
        dos_match = re.search(r"(?:Date of Service|DOS|Service Date|Encounter Date|Visit Date|Date of Visit)[:\s]+([A-Za-z0-9/\-]+(?:,\s*\d{4})?)", text, re.IGNORECASE)
        if dos_match:
            results["dos"] = self._normalize_date_str(dos_match.group(1))

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
        - 02: Telehealth Provided Other than in Patient's Home
        - 10: Telehealth Provided in Patient's Home
        """
        text_lower = text.lower()
        if any(term in text_lower for term in ["assisted living", "alf", "senior living facility", "group home", "memory care facility"]):
            return "13"
        elif any(term in text_lower for term in ["home visit", "patient residence", "patient home", "domiciliary visit", "house call"]):
            return "12"
        elif any(term in text_lower for term in ["telehealth in home", "virtual visit at home", "telemedicine at home"]):
            return "10"
        elif any(term in text_lower for term in ["telehealth", "telemedicine", "virtual visit", "video visit", "audio/video encounter", "doxy.me", "zoom medical"]):
            return "02"
        else:
            return "11"

    def _extract_soap_structure(self, text: str, source_type: DocumentSourceType) -> ClinicalNoteParsed:
        cc = ""
        hpi = ""
        ros = ""
        pe = ""
        ap = ""
        time_spent = 0

        cc_match = re.search(r"(?:Chief Complaint|CC|Reason for Visit)[:\s]+(.*?)(?=\n[A-Z]|History|HPI|Exam|Assessment|$)", text, re.IGNORECASE | re.DOTALL)
        if cc_match:
            cc = cc_match.group(1).strip()
        elif "complains of" in text.lower() or "c/o" in text.lower():
            lines = [l for l in text.split("\n") if "complains of" in l.lower() or "c/o" in l.lower()]
            cc = lines[0] if lines else "Medical Consultation"

        hpi_match = re.search(r"(?:HPI|History of Present Illness)[:\s]+(.*?)(?=\n[A-Z]|ROS|Exam|Assessment|A&P|$)", text, re.IGNORECASE | re.DOTALL)
        if hpi_match:
            hpi = hpi_match.group(1).strip()
        else:
            hpi = text[:300].strip()

        ap_match = re.search(r"(?:Assessment and Plan|Assessment & Plan|A&P|Plan|Impression)[:\s]+(.*?)(?=\n[A-Z]|Signature|Time|$)", text, re.IGNORECASE | re.DOTALL)
        if ap_match:
            ap = ap_match.group(1).strip()
        else:
            ap = text[-400:].strip() if len(text) > 400 else text

        time_match = re.search(r"(\d+)\s*(?:minutes|mins|min)", text, re.IGNORECASE)
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
