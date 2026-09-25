# ============================================
# FILE: medical_billing_ai/models/encounter.py
# PURPOSE: Pydantic Data Models for Multimodal Encounter, Patient, Provider, & Billing Data
# ============================================

from enum import Enum
from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field

class DocumentSourceType(str, Enum):
    CAMERA_IMAGE = "CAMERA_IMAGE"
    OCR_TEXT = "OCR_TEXT"
    PDF_FILE = "PDF_FILE"
    DOCX_FILE = "DOCX_FILE"
    HANDWRITTEN_NOTE = "HANDWRITTEN_NOTE"
    STRUCTURED_JSON = "STRUCTURED_JSON"

class ProviderType(str, Enum):
    MD = "MD"
    DO = "DO"
    APRN = "APRN"
    PA = "PA"

class PatientInfo(BaseModel):
    first_name: str
    last_name: str
    dob: str  # YYYY-MM-DD
    gender: str = "U"  # M, F, U
    member_id: str
    group_number: Optional[str] = None
    address: str = "100 Main St"
    city: str = "Miami"
    state: str = "FL"
    zip: str = "33101"

class ProviderInfo(BaseModel):
    organization_name: str
    rendering_first_name: str
    rendering_last_name: str
    type: ProviderType
    state: str = "FL"
    specialty: str
    fl_autonomous_registered: bool = False
    has_collaborative_protocol: bool = False
    npi: str
    tax_id: str
    taxonomy_code: str = "207Q00000X"
    phone: str = "8005551212"
    address: str = "1200 Brickell Ave Ste 400"
    city: str = "Miami"
    zip: str = "33131"

class PayerInfo(BaseModel):
    name: str
    payer_id: str  # Clearinghouse Electronic Payer ID (e.g. 00590 for FL Blue)
    type: str = "COMMERCIAL"  # MEDICARE, MEDICAID, COMMERCIAL
    address: str = "PO BOX 1000"
    city: str = "Jacksonville"
    state: str = "FL"
    zip: str = "32202"
    requires_prior_auth: bool = False
    requires_referral: bool = False

class DiagnosisCode(BaseModel):
    code: str  # ICD-10-CM e.g. E11.9
    description: str
    pointer: int = 1
    is_primary: bool = False

class CPTServiceLine(BaseModel):
    code: str  # CPT / HCPCS e.g. 99214, 17000
    modifiers: List[str] = Field(default_factory=list)
    charge: float
    units: int = 1
    dx_pointers: List[int] = Field(default_factory=lambda: [1])

class ClinicalNoteParsed(BaseModel):
    raw_text: str = ""
    source_type: DocumentSourceType = DocumentSourceType.STRUCTURED_JSON
    chief_complaint: str = ""
    history_of_present_illness: str = ""
    review_of_systems: str = ""
    physical_exam: str = ""
    assessment_and_plan: str = ""
    time_spent_minutes: int = 0
    patient_status: str = "established"  # new, established
    direct_physician_supervision_present: bool = False
    has_new_medical_problem: bool = False
    diagnoses: List[DiagnosisCode] = Field(default_factory=list)

class BillingDetails(BaseModel):
    place_of_service: str = "11"  # 11 = Office, 02 = Telehealth
    total_charge: float = 0.0
    patient_copay_collected: float = 0.0
    prior_authorization_number: Optional[str] = None
    incident_to: bool = False
    diagnosis_codes: List[DiagnosisCode] = Field(default_factory=list)
    cpt_codes: List[CPTServiceLine] = Field(default_factory=list)

class EncounterPayload(BaseModel):
    encounter_id: str
    date_of_service: str  # YYYY-MM-DD
    patient: PatientInfo
    provider: ProviderInfo
    payer: PayerInfo
    clinical_note: ClinicalNoteParsed
    billing: BillingDetails
    raw_document_metadata: Optional[Dict[str, Any]] = None
