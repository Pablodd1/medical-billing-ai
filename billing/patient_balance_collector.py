# ============================================
# FILE: medical_billing_ai/billing/patient_balance_collector.py
# PURPOSE: Automated Patient Out-of-Pocket Collections & Statement Generator (Post-835)
# ============================================

from typing import Dict, Any, List
from datetime import datetime
from ..models.encounter import PatientBalanceStatement

class PatientBalanceCollector:
    """
    Captures the remaining 30-40% of clinic revenue (Patient Responsibility)
    from 835 ERAs (Group Code PR: Deductibles, Coinsurance, Copayments).
    Generates automated electronic patient statements and SMS/Stripe payment links.
    """

    def generate_patient_statement_from_835(
        self,
        claim_remittance: Dict[str, Any],
        patient_info: Dict[str, Any],
        encounter_id: str,
        clinic_name: str = "Magnolia Health Clinic"
    ) -> PatientBalanceStatement:
        total_billed = claim_remittance.get("total_charge", 0.0)
        insurance_paid = claim_remittance.get("total_paid", 0.0)
        patient_resp = claim_remittance.get("patient_responsibility", 0.0)

        # Break down Patient Responsibility adjustments
        copay = 0.0
        deductible = 0.0
        coinsurance = 0.0
        contractual_adj = 0.0

        for adj in claim_remittance.get("adjustments", []):
            group = adj.get("group_code")
            carc = str(adj.get("carc_code"))
            amt = float(adj.get("amount", 0.0))

            if group == "PR":
                if carc == "1":
                    deductible += amt
                elif carc == "2":
                    coinsurance += amt
                elif carc == "3":
                    copay += amt
                else:
                    patient_resp = max(patient_resp, amt)
            elif group == "CO":
                contractual_adj += amt

        # Calculate final patient balance if not already populated
        calculated_patient_balance = round(copay + deductible + coinsurance, 2)
        if calculated_patient_balance == 0.0 and patient_resp > 0.0:
            calculated_patient_balance = round(patient_resp, 2)

        statement_id = f"STMT-{encounter_id[-8:] if len(encounter_id) >= 8 else encounter_id}"
        payment_link = f"https://pay.magnoliahealth.com/checkout/{statement_id}"

        patient_name = f"{patient_info.get('first_name', '')} {patient_info.get('last_name', '')}".strip() or "Valued Patient"
        patient_phone = patient_info.get("phone", "3055550199")
        patient_email = patient_info.get("email", "patient@example.com")
        dos = patient_info.get("date_of_service", datetime.now().strftime("%Y-%m-%d"))

        # SMS Notification message
        sms_text = (
            f"Hello {patient_name}, {clinic_name} has received the insurance explanation of benefits "
            f"for your visit on {dos}. Your insurance covered ${insurance_paid:.2f}. "
            f"Your remaining patient balance is ${calculated_patient_balance:.2f} "
            f"(Deductible: ${deductible:.2f}, Copay/Coinsurance: ${(copay + coinsurance):.2f}). "
            f"Pay securely here: {payment_link}"
        )

        return PatientBalanceStatement(
            statement_id=statement_id,
            encounter_id=encounter_id,
            patient_name=patient_name,
            patient_phone=patient_phone,
            patient_email=patient_email,
            date_of_service=dos,
            total_billed=total_billed,
            insurance_paid=insurance_paid,
            contractual_adjustment=contractual_adj,
            patient_responsibility=calculated_patient_balance,
            copay_amount=copay,
            deductible_amount=deductible,
            coinsurance_amount=coinsurance,
            payment_link_url=payment_link,
            sms_message=sms_text,
            status="PENDING_COLLECTION" if calculated_patient_balance > 0 else "PAID"
        )
