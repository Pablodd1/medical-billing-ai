# ============================================
# FILE: medical_billing_ai/models/encounter.py
# PURPOSE: Pydantic Data Models for Multimodal Encounter, Patient, Dual NPI Provider, Attestation, Secondary COB, & POS
# ============================================

from enum import Enum
from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field, model_validator

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

class PayerInfo(BaseModel):
    name: str
    payer_id: str  # Clearinghouse Electronic Payer ID (e.g. 00590 for FL Blue)
    type: str = "COMMERCIAL"  # MEDICARE, MEDICAID, COMMERCIAL, MEDIGAP
    address: str = "PO BOX 1000"
    city: str = "Jacksonville"
    state: str = "FL"
    zip: str = "32202"
    requires_prior_auth: bool = False
    requires_referral: bool = False
    timely_filing_days: int = 180  # Default: 180 days (Medicaid=90-180, Medicare=365)

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
    phone: str = "3055550199"
    email: str = "patient@example.com"
    # Secondary / Supplemental Insurance (COB)
    secondary_payer: Optional[PayerInfo] = None
    secondary_member_id: Optional[str] = None
    secondary_group_number: Optional[str] = None

class ProviderInfo(BaseModel):
    organization_name: str
    rendering_first_name: str
    rendering_last_name: str
    type: ProviderType
    state: str = "FL"
    specialty: str
    fl_autonomous_registered: bool = False
    has_collaborative_protocol: bool = False
    # Dual NPI Separation:
    rendering_npi_type_1: str = ""  # Individual Doctor/APRN NPI (Box 24J, Loop 2310B)
    billing_npi_type_2: str = ""    # Clinic / Group Entity NPI (Box 33a, Loop 2010AA)
    npi: str = ""                   # Fallback / General NPI
    tax_id: str
    taxonomy_code: str = "207Q00000X"
    phone: str = "8005551212"
    address: str = "1200 Brickell Ave Ste 400"
    city: str = "Miami"
    zip: str = "33131"

    @model_validator(mode="after")
    def populate_and_verify_npis(self):
        if not self.rendering_npi_type_1 and self.npi:
            self.rendering_npi_type_1 = self.npi
        if not self.billing_npi_type_2:
            self.billing_npi_type_2 = self.npi or self.rendering_npi_type_1
        if not self.npi:
            self.npi = self.rendering_npi_type_1
        return self

class ProviderAttestation(BaseModel):
    """
    Physician 1-Click Legal Attestation & False Claims Act Shield (Box 31 / CLM06).
    Transfers clinical liability to the licensed physician/practitioner.
    """
    is_attested: bool = False
    attested_by_name: Optional[str] = None
    attested_by_npi: Optional[str] = None
    attested_at: Optional[str] = None
    signature_hash: Optional[str] = None
    legal_certification_text: str = (
        "I certify that the services listed were personally rendered by me or under my direct personal supervision, "
        "are medically reasonable and necessary, and are accurately represented by the diagnosis and procedure codes indicated."
    )

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
    # Multi-Date Disambiguation:
    date_of_service_detected: Optional[str] = None
    registration_date_detected: Optional[str] = None
    signature_date_detected: Optional[str] = None
    # Place of Service Disambiguation:
    place_of_service_detected: str = "11"  # 11 = Office, 12 = Home, 13 = ALF, 02/10 = Telehealth

class BillingDetails(BaseModel):
    place_of_service: str = "11"  # 11 = Office, 12 = Home, 13 = Assisted Living Facility, 02/10 = Telehealth
    facility_name: Optional[str] = None
    total_charge: float = 0.0
    patient_copay_collected: float = 0.0
    prior_authorization_number: Optional[str] = None
    incident_to: bool = False
    diagnosis_codes: List[DiagnosisCode] = Field(default_factory=list)
    cpt_codes: List[CPTServiceLine] = Field(default_factory=list)

class TimelyFilingAssessment(BaseModel):
    date_of_service: str
    days_elapsed: int
    payer_deadline_days: int
    days_remaining: int
    status: str  # ON_TIME, WARNING_NEARING_DEADLINE, EXPIRED

class PatientBalanceStatement(BaseModel):
    """
    Patient Out-Of-Pocket Balance & Collections Model (Post-835 Remittance).
    Captures the remaining 30-40% revenue (Deductible, Coinsurance, Copay).
    """
    statement_id: str
    encounter_id: str
    patient_name: str
    patient_phone: str
    patient_email: str
    date_of_service: str
    total_billed: float
    insurance_paid: float
    contractual_adjustment: float
    patient_responsibility: float
    copay_amount: float = 0.0
    deductible_amount: float = 0.0
    coinsurance_amount: float = 0.0
    payment_link_url: str
    sms_message: str
    status: str = "PENDING_COLLECTION"  # PENDING_COLLECTION, PAID, SENT

class EncounterPayload(BaseModel):
    encounter_id: str
    date_of_service: str  # YYYY-MM-DD
    patient: PatientInfo
    provider: ProviderInfo
    payer: PayerInfo
    clinical_note: ClinicalNoteParsed
    billing: BillingDetails
    attestation: Optional[ProviderAttestation] = None
    timely_filing: Optional[TimelyFilingAssessment] = None
    claim_fingerprint_hash: Optional[str] = None
    raw_document_metadata: Optional[Dict[str, Any]] = None
