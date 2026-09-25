# ============================================
# FILE: medical_billing_ai/ingestion/multimodal_ingestor.py
# PURPOSE: Multimodal Document Ingestor (Camera, OCR, PDF, DOCX, Handwritten Notes)
# ============================================

import os
import re
import sys
from typing import Dict, Any, Union
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

        return ClinicalNoteParsed(
            raw_text=text,
            source_type=source_type,
            chief_complaint=cc or "General consultation and management",
            history_of_present_illness=hpi,
            review_of_systems=ros,
            physical_exam=pe,
            assessment_and_plan=ap,
            time_spent_minutes=time_spent,
            patient_status="established" if "follow-up" in text.lower() or "f/u" in text.lower() else "new"
        )
