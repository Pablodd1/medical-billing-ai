# ============================================
# FILE: medical_billing_ai/growth/nppes_lead_scraper.py
# PURPOSE: Live Online Medical Clinic & Provider Lead Scraper via CMS NPPES API
#          Scrapes real, verified clinics, NPIs, decision makers, phone numbers,
#          addresses, and specialties, and calculates revenue recovery ROI.
# ============================================

import csv
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
import requests


class NPPESLeadScraper:
    """
    Scrapes real medical practices, clinics, and autonomous practitioners from
    the official U.S. CMS NPPES NPI Registry API (https://npiregistry.cms.hhs.gov/api/?version=2.1).
    Enriches each prospect with specialty denial-risk hooks, estimated claim volume,
    and recoverable annual revenue for outbound sales & marketing.
    """

    NPPES_API_URL = "https://npiregistry.cms.hhs.gov/api/"

    SPECIALTY_PROFILES = {
        "Family Medicine": {
            "avg_monthly_claims": 750,
            "avg_claim_value": 185.0,
            "denial_leakage_pct": 0.11,
            "undercoding_pct": 0.08,
            "pain_point_hook": (
                "Eliminate E/M 99213->99214 undercoding on chronic care visits (T2DM/HTN) "
                "and prevent FCSO LCD L36063 denials on lab & preventive bundles."
            ),
            "top_cpts": ["99214", "99213", "82306", "93000"],
        },
        "Nurse Practitioner": {
            "avg_monthly_claims": 520,
            "avg_claim_value": 165.0,
            "denial_leakage_pct": 0.14,
            "undercoding_pct": 0.09,
            "pain_point_hook": (
                "Automated Florida Statute §464.0123 Autonomous APRN & §458.348 Supervisory Protocol "
                "pre-scrubbing plus automatic 363L00000X taxonomy routing to stop clearinghouse rejections."
            ),
            "top_cpts": ["99214", "99213", "17000", "20610"],
        },
        "Physical Medicine & Rehabilitation": {
            "avg_monthly_claims": 680,
            "avg_claim_value": 245.0,
            "denial_leakage_pct": 0.15,
            "undercoding_pct": 0.07,
            "pain_point_hook": (
                "Prevent CARC 97 bundling denials on joint injections (CPT 20610) & trigger point "
                "injections (CPT 20552) with automated Modifier -25 surgical scoping and FCSO LCD L36805 checks."
            ),
            "top_cpts": ["99214", "20610", "20552", "97110"],
        },
        "Orthopaedic Surgery": {
            "avg_monthly_claims": 890,
            "avg_claim_value": 320.0,
            "denial_leakage_pct": 0.14,
            "undercoding_pct": 0.06,
            "pain_point_hook": (
                "Stop CARC 97 & CARC 50 denials on arthrocentesis (CPT 20610) and diagnostic imaging "
                "by auto-linking Box 24E (SV107) musculoskeletal pointers and enforcing ICD-10 I.B.5 symptom suppression."
            ),
            "top_cpts": ["99214", "20610", "73560", "20552"],
        },
        "Dermatology": {
            "avg_monthly_claims": 950,
            "avg_claim_value": 235.0,
            "denial_leakage_pct": 0.13,
            "undercoding_pct": 0.07,
            "pain_point_hook": (
                "Zero out NCCI PTP bundling rejections on actinic keratosis cryosurgery (CPT 17000) "
                "and skin biopsies (CPT 11102/11104) billed alongside same-day E/M 99214-25."
            ),
            "top_cpts": ["99214", "17000", "11102", "11104"],
        },
        "Internal Medicine": {
            "avg_monthly_claims": 820,
            "avg_claim_value": 195.0,
            "denial_leakage_pct": 0.11,
            "undercoding_pct": 0.08,
            "pain_point_hook": (
                "Capture full AMA 2021/2023 Moderate/High MDM E/M reimbursement (99214/99215) "
                "and automate secondary Medicare/Medigap Loop 2320 COB crossover."
            ),
            "top_cpts": ["99214", "99215", "93000", "82306"],
        },
        "Physical Therapist": {
            "avg_monthly_claims": 1100,
            "avg_claim_value": 140.0,
            "denial_leakage_pct": 0.12,
            "undercoding_pct": 0.05,
            "pain_point_hook": (
                "Enforce CMS NCCI MUE unit limits on therapeutic exercise (CPT 97110) and manual therapy "
                "(CPT 97140) while automating post-835 patient deductible & coinsurance SMS collections."
            ),
            "top_cpts": ["97110", "97140", "97530", "97161"],
        },
    }

    DEFAULT_CAMPAIGN_QUERIES = [
        {"taxonomy_description": "Family Medicine", "state": "FL", "city": "Miami", "limit": 25},
        {"taxonomy_description": "Nurse Practitioner", "state": "FL", "city": "Miami", "limit": 25},
        {"taxonomy_description": "Physical Medicine & Rehabilitation", "state": "FL", "city": "Miami", "limit": 20},
        {"taxonomy_description": "Dermatology", "state": "FL", "city": "Miami", "limit": 15},
        {"taxonomy_description": "Orthopaedic Surgery", "state": "FL", "city": "Orlando", "limit": 15},
        {"taxonomy_description": "Family Medicine", "state": "FL", "city": "Tampa", "limit": 20},
        {"taxonomy_description": "Nurse Practitioner", "state": "FL", "city": "Fort Lauderdale", "limit": 20},
        {"taxonomy_description": "Internal Medicine", "state": "FL", "city": "Jacksonville", "limit": 15},
    ]

    @staticmethod
    def _clean_phone(raw_phone: str) -> str:
        digits = re.sub(r"\D", "", str(raw_phone or ""))
        if len(digits) == 10:
            return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
        return raw_phone or ""

    def _enrich_lead(self, item: Dict[str, Any], query_specialty: str) -> Optional[Dict[str, Any]]:
        npi = str(item.get("number", "")).strip()
        if not npi:
            return None

        enum_type = item.get("enumeration_type", "NPI-2")
        basic = item.get("basic", {}) or {}
        addresses = item.get("addresses", []) or []
        taxonomies = item.get("taxonomies", []) or []

        # Prefer LOCATION address over MAILING
        loc_addr = next((a for a in addresses if a.get("address_purpose") == "LOCATION"), None)
        if not loc_addr and addresses:
            loc_addr = addresses[0]
        loc_addr = loc_addr or {}

        phone = self._clean_phone(loc_addr.get("telephone_number", ""))
        fax = self._clean_phone(loc_addr.get("fax_number", ""))
        if not phone:
            return None  # Only keep actionable leads with direct phone contact

        primary_tax = next((t for t in taxonomies if t.get("primary")), taxonomies[0] if taxonomies else {})
        tax_code = primary_tax.get("code", "207Q00000X")
        tax_desc = primary_tax.get("desc") or query_specialty or "Family Medicine"

        # Clinic name & Decision Maker extraction
        if enum_type == "NPI-2":
            clinic_name = (
                basic.get("organization_name")
                or basic.get("authorized_official_last_name", "") + " Medical Group"
            ).strip()
            ao_first = basic.get("authorized_official_first_name", "").strip().title()
            ao_last = basic.get("authorized_official_last_name", "").strip().title()
            ao_cred = basic.get("authorized_official_credential", "").strip()
            ao_title = basic.get("authorized_official_title_or_position", "Managing Partner / Owner").strip().title()
            if ao_first or ao_last:
                dm_name = f"{ao_first} {ao_last}".strip()
                if ao_cred:
                    dm_name = f"{dm_name}, {ao_cred}"
            else:
                dm_name = "Medical Director / Practice Owner"
            dm_title = ao_title or "Medical Director"
        else:
            first = basic.get("first_name", "").strip().title()
            last = basic.get("last_name", "").strip().title()
            cred = basic.get("credential", "APRN" if "363L" in tax_code else "MD").strip()
            dm_name = f"{first} {last}".strip()
            if cred:
                dm_name = f"{dm_name}, {cred}"
            clinic_name = f"{first} {last} {cred} Clinical Practice".strip()
            dm_title = f"Lead Practitioner ({cred})"

        # Match specialty profile
        profile_key = "Family Medicine"
        for k in self.SPECIALTY_PROFILES:
            if k.lower() in tax_desc.lower() or k.lower() in query_specialty.lower():
                profile_key = k
                break
        if "363L" in tax_code:
            profile_key = "Nurse Practitioner"

        prof = self.SPECIALTY_PROFILES[profile_key]
        monthly_claims = prof["avg_monthly_claims"] if enum_type == "NPI-2" else int(prof["avg_monthly_claims"] * 0.65)
        annual_Gross = round(monthly_claims * 12 * prof["avg_claim_value"], 2)
        biller_fee_saved = round(annual_Gross * 0.06, 2)  # Traditional 6% RCM agency fee
        denial_recovered = round(annual_Gross * (prof["denial_leakage_pct"] * 0.75), 2)
        undercoding_recovered = round(annual_Gross * (prof["undercoding_pct"] * 0.60), 2)
        total_annual_roi = round(biller_fee_saved + denial_recovered + undercoding_recovered, 2)

        city = (loc_addr.get("city") or "Miami").title()
        state = (loc_addr.get("state") or "FL").upper()
        postal = str(loc_addr.get("postal_code") or "33131")[:5]
        street = (loc_addr.get("address_1") or "").title()

        return {
            "lead_id": f"LEAD-{npi}",
            "npi": npi,
            "enumeration_type": enum_type,
            "clinic_name": clinic_name,
            "decision_maker_name": dm_name,
            "decision_maker_title": dm_title,
            "specialty": tax_desc,
            "taxonomy_code": tax_code,
            "phone": phone,
            "fax": fax,
            "address": street,
            "city": city,
            "state": state,
            "zip": postal,
            "estimated_monthly_claims": monthly_claims,
            "estimated_annual_billing": annual_Gross,
            "estimated_biller_fee_savings": biller_fee_saved,
            "estimated_denial_recovery": denial_recovered,
            "estimated_undercoding_recovery": undercoding_recovered,
            "estimated_total_annual_upside": total_annual_roi,
            "pain_point_hook": prof["pain_point_hook"],
            "top_cpts": prof["top_cpts"],
            "status": "NEW_SCRAPED",
            "source": "CMS_NPPES_LIVE_API",
            "scraped_at": datetime.now().isoformat(timespec="seconds"),
        }

    def scrape_nppes(
        self,
        taxonomy_description: str = "Family Medicine",
        state: str = "FL",
        city: str = "",
        limit: int = 25,
        enumeration_type: str = "NPI-2",
    ) -> List[Dict[str, Any]]:
        """Queries the live CMS NPPES API v2.1 and returns enriched clinic leads."""
        params: Dict[str, Any] = {
            "version": "2.1",
            "taxonomy_description": taxonomy_description,
            "state": state,
            "limit": min(max(limit, 1), 200),
        }
        if city:
            params["city"] = city
        if enumeration_type in ("NPI-1", "NPI-2"):
            params["enumeration_type"] = enumeration_type

        leads: List[Dict[str, Any]] = []
        try:
            resp = requests.get(self.NPPES_API_URL, params=params, timeout=12)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("results", []):
                enriched = self._enrich_lead(item, taxonomy_description)
                if enriched:
                    leads.append(enriched)
        except Exception:
            pass
        return leads

    def run_full_campaign_scrape(self, output_dir: Optional[str] = None) -> Dict[str, Any]:
        """Runs a multi-city, multi-specialty scrape across Florida and saves CSV + JSON."""
        base_dir = output_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "leads"
        )
        os.makedirs(base_dir, exist_ok=True)

        seen_npis = set()
        all_leads: List[Dict[str, Any]] = []

        for q in self.DEFAULT_CAMPAIGN_QUERIES:
            # Try NPI-2 Organizations first, plus NPI-1 for Nurse Practitioners
            enum_type = "NPI-1" if q["taxonomy_description"] == "Nurse Practitioner" else "NPI-2"
            batch = self.scrape_nppes(
                taxonomy_description=q["taxonomy_description"],
                state=q["state"],
                city=q["city"],
                limit=q["limit"],
                enumeration_type=enum_type,
            )
            for lead in batch:
                if lead["npi"] not in seen_npis:
                    seen_npis.add(lead["npi"])
                    all_leads.append(lead)

        json_path = os.path.join(base_dir, "scraped_medical_clinics.json")
        csv_path = os.path.join(base_dir, "scraped_medical_clinics.csv")

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(all_leads, f, indent=2)

        if all_leads:
            fieldnames = [
                "lead_id", "npi", "enumeration_type", "clinic_name",
                "decision_maker_name", "decision_maker_title", "specialty",
                "taxonomy_code", "phone", "fax", "address", "city", "state", "zip",
                "estimated_monthly_claims", "estimated_annual_billing",
                "estimated_total_annual_upside", "pain_point_hook", "status", "source"
            ]
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(all_leads)

        total_pipeline_value = sum(l.get("estimated_total_annual_upside", 0) for l in all_leads)
        return {
            "total_leads_scraped": len(all_leads),
            "total_annual_recoverable_value": round(total_pipeline_value, 2),
            "json_path": json_path,
            "csv_path": csv_path,
            "leads": all_leads,
        }
