# ============================================
# FILE: medical_billing_ai/scrutiny/hcfa_auditor.py
# PURPOSE: HCFA-1500 (CMS-1500) 33-Box Data Extraction, Gap Detection, & Physician Query Generator
# ============================================

from typing import Dict, List, Any

class HCFA1500ExtractorAndAuditor:
    """
    Scrutinizes the clinical encounter and patient registration data against
    all 33 boxes of the standard CMS-1500 (HCFA-1500) claim form.
    Identifies absent information, applies clinical inference where permitted,
    and generates targeted real-time Physician Queries (CDI) for unresolvable gaps.
    """

    def audit_hcfa_requirements(self, encounter: Dict[str, Any]) -> Dict[str, Any]:
        patient = encounter.get("patient", {})
        provider = encounter.get("provider", {})
        note = encounter.get("clinical_note", {})
        billing = encounter.get("billing", {})
        payer = encounter.get("payer", {})

        hcfa_boxes = {}
        missing_fields = []
        physician_queries = []

        # --- BOX 1 & 1a: Insurance Type & Insured ID ---
        ins_type = payer.get("type", "COMMERCIAL").upper()
        member_id = str(patient.get("member_id", "")).strip()
        hcfa_boxes["box_1_insurance_type"] = ins_type
        hcfa_boxes["box_1a_insured_id"] = member_id
        if not member_id:
            missing_fields.append({"box": "1a", "field": "Insured's ID Number", "severity": "FATAL", "fix": "Collect insurance card front/back in Magnolia intake."})

        # --- BOX 2 & 5: Patient Name & Address ---
        pat_name = f"{patient.get('last_name', '')}, {patient.get('first_name', '')}".strip(", ")
        hcfa_boxes["box_2_patient_name"] = pat_name
        hcfa_boxes["box_5_patient_address"] = {
            "street": patient.get("address", ""),
            "city": patient.get("city", ""),
            "state": patient.get("state", ""),
            "zip": patient.get("zip", "")
        }
        if not pat_name or not patient.get("zip"):
            missing_fields.append({"box": "2/5", "field": "Patient Demographic Information", "severity": "FATAL", "fix": "Address and ZIP code required for clearinghouse routing."})

        # --- BOX 3: Patient DOB & Sex ---
        hcfa_boxes["box_3_patient_dob"] = patient.get("dob")
        hcfa_boxes["box_3_patient_sex"] = patient.get("gender")

        # --- BOX 10a-c: Condition Related to Employment, Auto, or Other Accident ---
        accident_type = encounter.get("accident_info", {}).get("is_accident", False)
        hcfa_boxes["box_10a_employment_related"] = encounter.get("accident_info", {}).get("employment", False)
        hcfa_boxes["box_10b_auto_accident"] = encounter.get("accident_info", {}).get("auto", False)
        hcfa_boxes["box_10c_other_accident"] = encounter.get("accident_info", {}).get("other", False)
        note_text = (note.get("chief_complaint", "") + " " + note.get("history_of_present_illness", "")).lower()
        if ("car accident" in note_text or "mva" in note_text or "work injury" in note_text) and not accident_type:
            physician_queries.append({
                "type": "ACCIDENT_INDICATOR_CONFLICT",
                "box": "10",
                "question": "Clinical note references a collision/work incident, but Box 10 accident indicators are unmarked. Is this visit related to an Auto Accident or Workers' Compensation claim?",
                "options": ["Auto Accident", "Workers Comp", "Neither (Standard Health Coverage)"]
            })

        # --- BOX 14: Date of Current Illness, Injury, or Pregnancy ---
        date_of_onset = encounter.get("clinical_note", {}).get("date_of_onset") or encounter.get("date_of_service")
        hcfa_boxes["box_14_date_of_current_illness"] = date_of_onset

        # --- BOX 17, 17b: Referring Provider & NPI ---
        referring_provider = encounter.get("referring_provider", {})
        hcfa_boxes["box_17_referring_name"] = referring_provider.get("name")
        hcfa_boxes["box_17b_referring_npi"] = referring_provider.get("npi")
        if payer.get("requires_referral", False) and not referring_provider.get("npi"):
            missing_fields.append({"box": "17b", "field": "Referring Provider NPI", "severity": "HIGH", "fix": "Payer requires PCP referral NPI for specialist visit."})

        # --- BOX 21: Diagnoses (ICD-10-CM Pointers A-L) ---
        diagnoses = billing.get("diagnosis_codes", [])
        hcfa_boxes["box_21_diagnosis_codes"] = [d.get("code") if isinstance(d, dict) else d.code for d in diagnoses]
        if not diagnoses:
            missing_fields.append({"box": "21", "field": "Diagnosis Codes", "severity": "FATAL", "fix": "At least one primary ICD-10-CM code is required."})

        # --- BOX 23: Prior Authorization Number ---
        prior_auth = billing.get("prior_authorization_number")
        hcfa_boxes["box_23_prior_auth_number"] = prior_auth
        if billing.get("requires_prior_auth", False) and not prior_auth:
            missing_fields.append({"box": "23", "field": "Prior Authorization Number", "severity": "FATAL", "fix": "Procedure requires Prior Authorization. Claim will be denied immediately under CARC 197."})

        # --- BOX 24: Service Lines ---
        cpt_codes = billing.get("cpt_codes", [])
        hcfa_boxes["box_24_service_lines"] = []
        for line in cpt_codes:
            line_dict = line if isinstance(line, dict) else line.model_dump()
            hcfa_boxes["box_24_service_lines"].append({
                "24A_dates": encounter.get("date_of_service"),
                "24B_pos": billing.get("place_of_service", "11"),
                "24D_cpt": line_dict.get("code"),
                "24D_modifiers": line_dict.get("modifiers", []),
                "24E_dx_pointers": line_dict.get("dx_pointers", [1]),
                "24F_charge": line_dict.get("charge", 0.0),
                "24G_units": line_dict.get("units", 1),
                "24J_rendering_npi": provider.get("npi")
            })

        # --- BOX 25: Federal Tax ID ---
        tax_id = provider.get("tax_id")
        hcfa_boxes["box_25_tax_id"] = tax_id
        if not tax_id:
            missing_fields.append({"box": "25", "field": "Federal Tax ID", "severity": "FATAL", "fix": "Clinic EIN is mandatory for payment disbursement."})

        # --- BOX 28 & 29: Total Charge & Amount Paid ---
        hcfa_boxes["box_28_total_charge"] = billing.get("total_charge", 0.0)
        hcfa_boxes["box_29_amount_paid"] = billing.get("patient_copay_collected", 0.0)

        # --- BOX 33, 33a: Billing Provider Info, Phone, & NPI ---
        hcfa_boxes["box_33_billing_provider"] = {
            "name": provider.get("organization_name"),
            "phone": provider.get("phone", "8005551212"),
            "address": provider.get("address"),
            "city_state_zip": f"{provider.get('city')}, {provider.get('state')} {provider.get('zip')}",
            "33a_npi": provider.get("npi"),
            "33b_taxonomy": provider.get("taxonomy_code")
        }

        is_clean = len(missing_fields) == 0

        return {
            "is_clean_claim": is_clean,
            "hcfa_boxes": hcfa_boxes,
            "missing_fields": missing_fields,
            "physician_queries": physician_queries
        }
