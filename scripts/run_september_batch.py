# ============================================
# FILE: scripts/run_september_batch.py
# PURPOSE: Autonomous Batch Ingestion, Strict Demographic Scrubbing,
#          Consolidated Office Ally 837P Batch Builder, CMS-1500 CSV Worklist,
#          and Ready-to-Send Email Package Generator for Medical Billers
# ============================================

import os
import sys
import csv
import json
import time
import argparse
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ["OA_ALLOW_SIMULATION"] = "true"

from pipeline import AutonomousMedicalBillingPipeline
from models.encounter import DocumentSourceType
from remittance.claim_ledger import ClaimLedger
from compliance.claim_gate import (
    coder_review_notes, hard_gate_issues, icd10_issues, load_billing_entity, validate_x12_837,
)

try:  # optional: pick up BILLING_* values from .env
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except ImportError:
    pass

OVERRIDE_DEMO_KEYS = ("first_name", "last_name", "dob", "gender", "member_id", "medicaid_id", "secondary_payer",
                      "address", "city", "state", "zip", "date_of_service", "referring_provider")


def load_overrides(path: Optional[str]) -> Dict[str, Dict[str, Any]]:
    """Biller corrections keyed by PDF file name (see exports/overrides_template.json)."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def apply_overrides(demo: Dict[str, Any], ov: Dict[str, Any]) -> None:
    """Applies biller-verified values onto extracted demographics (in place)."""
    for key in OVERRIDE_DEMO_KEYS:
        if key in ov:
            demo[key] = ov[key] or None
    if ov.get("first_name") or ov.get("last_name"):
        demo["patient_name"] = f"{demo.get('last_name')}, {demo.get('first_name')}"


def download_file(file_id: str, dest_path: str) -> bool:
    """Downloads a file from Google Drive via direct export link."""
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 1000:
        return True
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        with urllib.request.urlopen(req) as resp, open(dest_path, "wb") as f:
            f.write(resp.read())
        return os.path.exists(dest_path) and os.path.getsize(dest_path) > 1000
    except Exception as e:
        print(f"    [!] Error downloading {file_id}: {e}")
        return False


def export_biller_csv(results: List[Dict[str, Any]], csv_path: str) -> None:
    """Exports a comprehensive CMS-1500 (Boxes 1-33) Excel/CSV worklist for medical billers."""
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    headers = [
        "Row_#",
        "Source_PDF_File",
        "Claim_Status",
        "Review_Tier",
        "Box1_Payer_Type",
        "Box1a_Medicare_MBI",
        "Box2_Patient_Name_Last_First",
        "Box3_Patient_DOB",
        "Box3_Sex",
        "Box5_Patient_Address",
        "Box5_City",
        "Box5_State",
        "Box5_Zip",
        "Box9a_Secondary_Medicaid_ID",
        "Box9d_Secondary_Payer_Name",
        "Box17_Referring_Physician",
        "Box17b_Attending_NPI",
        "Box18_SNF_Admission_Date",
        "Box21_ICD10_A_Primary",
        "Box21_ICD10_B",
        "Box21_ICD10_C",
        "Box21_ICD10_D",
        "Box21_ICD10_E_to_L_Additional",
        "Box24A_Date_Of_Service",
        "Box24B_Place_Of_Service",
        "Box24D_CPT_Code",
        "Box24D_Modifiers",
        "Box24E_Diagnosis_Pointers",
        "Box24F_Line_Charge_USD",
        "Box24G_Units",
        "Box24J_Rendering_NPI",
        "Box26_Patient_Control_No",
        "Box28_Total_Charge_USD",
        "Box31_Rendering_Provider",
        "Box32_Service_Facility_Name",
        "Box33a_Billing_NPI",
        "Scrutiny_Confidence",
        "Action_Required_Or_Notes"
    ]

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for r in results:
            dxs = r.get("icd10_codes", [])
            dx_a = dxs[0] if len(dxs) > 0 else ""
            dx_b = dxs[1] if len(dxs) > 1 else ""
            dx_c = dxs[2] if len(dxs) > 2 else ""
            dx_d = dxs[3] if len(dxs) > 3 else ""
            dx_rest = ", ".join(dxs[4:12]) if len(dxs) > 4 else ""
            cpts = ", ".join(r.get("cpt_codes", []))
            mods = ", ".join(r.get("modifiers", []))
            ptrs = r.get("dx_pointers", "A")
            issues_list = r.get("issues", [])
            notes = "; ".join(f"[{i.get('code')}] {i.get('message')}" for i in issues_list) if issues_list else "READY FOR OFFICE ALLY"

            writer.writerow([
                r.get("index"),
                r.get("filename"),
                r.get("status"),
                r.get("review_tier") or "HOLD",
                "Medicare Part B",
                r.get("member_id") or "MISSING",
                r.get("patient_name") or "MISSING",
                r.get("dob") or "MISSING",
                r.get("gender") or "",
                r.get("address") or "",
                r.get("city") or "",
                r.get("state") or "",
                r.get("zip") or "",
                r.get("medicaid_id") or "",
                r.get("secondary_payer") or "",
                r.get("referring_provider") or "",
                "",
                r.get("admission_date") or "",
                dx_a,
                dx_b,
                dx_c,
                dx_d,
                dx_rest,
                r.get("date_of_service") or "MISSING",
                r.get("place_of_service") or "31",
                cpts,
                mods,
                ptrs,
                f"{r.get('total_charge', 0.0):.2f}",
                1,
                r.get("rendering_npi") or "",
                r.get("patient_control_number"),
                f"{r.get('total_charge', 0.0):.2f}",
                r.get("consultant_provider") or "Dr. Nasser Eftekhari, MD",
                r.get("facility_name") or "",
                r.get("billing_npi") or "NOT CONFIGURED",
                f"{(r.get('confidence_score') or 0.0) * 100:.0f}%",
                notes
            ])


def build_email_package(
    results: List[Dict[str, Any]],
    csv_path: str,
    batch_837_path: str,
    html_out_path: str,
    eml_out_path: str,
    recipient_email: Optional[str] = None
) -> None:
    """Builds an HTML email digest and an RFC-5322 .eml package with attached CSV and .837 batch file."""
    clean_claims = [r for r in results if r.get("status") == "TRANSMITTED_CLEAN_CLAIM"]
    flagged = [r for r in results if r.get("status") != "TRANSMITTED_CLEAN_CLAIM"]
    clean_total = sum(r.get("total_charge", 0.0) for r in clean_claims)

    rows_html = []
    for r in results:
        badge_color = "#16a34a" if r.get("status") == "TRANSMITTED_CLEAN_CLAIM" else "#dc2626"
        status_label = "CLEAN (837P READY)" if r.get("status") == "TRANSMITTED_CLEAN_CLAIM" else "HOLD / REVIEW"
        dx_str = ", ".join(r.get("icd10_codes", [])[:4])
        if len(r.get("icd10_codes", [])) > 4:
            dx_str += f" (+{len(r['icd10_codes']) - 4})"
        rows_html.append(
            f"<tr>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'>{r['index']}</td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'><b>{r.get('patient_name')}</b></td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'>{r.get('dob') or 'MISSING'}</td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'><code>{r.get('member_id') or 'MISSING'}</code></td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'>{r.get('date_of_service') or 'MISSING'}</td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'>{r.get('facility_name', '')[:28]} (POS {r.get('place_of_service')})</td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'><b>{', '.join(r.get('cpt_codes', []))}</b></td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'>{dx_str or 'NONE'}</td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'>${r.get('total_charge', 0.0):.2f}</td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;color:{badge_color};font-weight:bold;'>{status_label}</td>"
            f"</tr>"
        )

    flagged_html = ""
    if flagged:
        items = []
        for fl in flagged:
            reasons = "; ".join(i.get("message", "") for i in fl.get("issues", []))
            items.append(f"<li><b>#{fl['index']} ({fl['filename']})</b>: {reasons}</li>")
        flagged_html = (
            f"<div style='background:#fef2f2;border-left:4px solid #dc2626;padding:12px;margin:16px 0;'>"
            f"<h3 style='margin:0 0 8px 0;color:#991b1b;'>Action Required: {len(flagged)} Document(s) Held for Biller Review</h3>"
            f"<ul style='margin:0;padding-left:20px;color:#7f1d1d;'>{''.join(items)}</ul>"
            f"</div>"
        )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#1e293b;line-height:1.5;max-width:1100px;margin:0 auto;padding:16px;">
  <h2 style="color:#0f172a;border-bottom:2px solid #0284c7;padding-bottom:8px;">
    Medical Billing AI — September 2026 Batch Package (Office Ally &amp; CMS-1500 Worklist)
  </h2>
  <p>Attached to this email are your two production billing deliverables:</p>
  <ol>
    <li><b><code>{os.path.basename(batch_837_path)}</code></b> — Consolidated HIPAA 5010 X12 837P file containing all <b>{len(clean_claims)} clean claims (${clean_total:,.2f})</b>. Upload directly in <b>Office Ally &rarr; Upload Claims &rarr; Professional (837P)</b> or transmit via SFTP.</li>
    <li><b><code>{os.path.basename(csv_path)}</code></b> — Full CMS-1500 Box 1–33 Biller Spreadsheet (Excel-compatible) for manual verification, secondary Medicaid crossover tracking, or archive.</li>
  </ol>
  <table style="border-collapse:collapse;margin:16px 0;background:#f8fafc;width:100%;">
    <tr>
      <td style="padding:12px;border:1px solid #cbd5e1;"><b>Total Files Ingested:</b> {len(results)}</td>
      <td style="padding:12px;border:1px solid #cbd5e1;color:#16a34a;"><b>Clean Claims Ready:</b> {len(clean_claims)} ({len(clean_claims)/len(results)*100:.1f}%)</td>
      <td style="padding:12px;border:1px solid #cbd5e1;color:#dc2626;"><b>Held for Review:</b> {len(flagged)}</td>
      <td style="padding:12px;border:1px solid #cbd5e1;"><b>Clean Billed Total:</b> <b>${clean_total:,.2f}</b></td>
    </tr>
  </table>
  {flagged_html}
  <h3>Complete Patient Claim Schedule</h3>
  <table style="border-collapse:collapse;width:100%;font-size:13px;">
    <thead>
      <tr style="background:#0f172a;color:#ffffff;text-align:left;">
        <th style="padding:8px;border:1px solid #334155;">#</th>
        <th style="padding:8px;border:1px solid #334155;">Patient Name</th>
        <th style="padding:8px;border:1px solid #334155;">DOB</th>
        <th style="padding:8px;border:1px solid #334155;">Medicare MBI</th>
        <th style="padding:8px;border:1px solid #334155;">DOS</th>
        <th style="padding:8px;border:1px solid #334155;">Facility / POS</th>
        <th style="padding:8px;border:1px solid #334155;">CPT</th>
        <th style="padding:8px;border:1px solid #334155;">ICD-10 Diagnoses</th>
        <th style="padding:8px;border:1px solid #334155;">Charge</th>
        <th style="padding:8px;border:1px solid #334155;">Status</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>
</body>
</html>"""

    os.makedirs(os.path.dirname(html_out_path), exist_ok=True)
    with open(html_out_path, "w", encoding="utf-8") as f:
        f.write(html_body)

    msg = EmailMessage()
    msg["Subject"] = (
        f"[{'READY FOR OFFICE ALLY' if not os.path.basename(batch_837_path).startswith('DRAFT') else 'DRAFT - DO NOT SUBMIT'}] "
        f"September 2026 PM&R Batch — {len(clean_claims)} Queued Claims (${clean_total:,.2f})"
    )
    msg["From"] = os.environ.get("SMTP_FROM", "billing-ai@magnoliahealth.local")
    msg["To"] = recipient_email or os.environ.get("BILLER_EMAIL", "biller@practice.local")
    msg.set_content(
        f"September 2026 Medical Billing Batch Complete.\n"
        f"Clean Claims: {len(clean_claims)} (${clean_total:,.2f})\n"
        f"Held for Review: {len(flagged)}\n"
        f"Please see attached CMS-1500 CSV spreadsheet and consolidated Office Ally .837 batch file."
    )
    msg.add_alternative(html_body, subtype="html")

    if os.path.exists(csv_path):
        with open(csv_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="text", subtype="csv", filename=os.path.basename(csv_path))

    if os.path.exists(batch_837_path):
        with open(batch_837_path, "rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype="edi-x12", filename=os.path.basename(batch_837_path))

    with open(eml_out_path, "wb") as f:
        f.write(bytes(msg))

    if recipient_email and os.environ.get("SMTP_HOST"):
        smtp_host = os.environ["SMTP_HOST"]
        smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        smtp_user = os.environ.get("SMTP_USER", "")
        smtp_pass = os.environ.get("SMTP_PASS", "")
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"[*] Live Email Dispatched via SMTP to: {recipient_email}")


def run_batch(
    limit: int = None,
    batch_prefix: str = "SEP26",
    manifest_path: str = "september_manifest.json",
    data_dir: str = "data/september_2026",
    email_to: Optional[str] = None,
    overrides_path: Optional[str] = None,
    coder_signoff: Optional[str] = None
):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("=" * 80)
    print(f"▶ MEDICAL BILLING AI: AUTONOMOUS SEPTEMBER 2026 BATCH INGESTION (BATCH {batch_prefix})")
    print("=" * 80)

    if not os.path.exists(manifest_path):
        from scripts.batch_manifest import get_manifest
        manifest = get_manifest()
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
    else:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs("exports", exist_ok=True)
    # This script always runs in simulation (files are uploaded manually), so it uses an
    # isolated working ledger: reruns are reproducible and never poison the main ledger's
    # duplicate-claim guard. Duplicates *within* this batch are still caught.
    batch_ledger = os.path.abspath(os.path.join("exports", f"batch_ledger_{batch_prefix}.sqlite3"))
    if os.path.exists(batch_ledger):
        os.remove(batch_ledger)
    os.environ["LEDGER_DB_PATH"] = batch_ledger
    pipeline = AutonomousMedicalBillingPipeline()

    overrides_path = overrides_path or os.path.join(data_dir, "overrides.json")
    overrides = load_overrides(overrides_path)
    billing_entity, entity_problems = load_billing_entity()
    print(f"[*] Biller overrides loaded: {len(overrides)} ({overrides_path})")
    if entity_problems:
        print("[!] Billing entity NOT configured -> output will be a DRAFT, not submittable: " + "; ".join(entity_problems))

    file_items = sorted(manifest.items())
    if limit:
        file_items = file_items[:limit]

    print(f"[*] Total files queued for ingestion: {len(file_items)}")
    print(f"[*] Storage target directory: {data_dir}")
    print(f"[*] Strict Demographic Mode: ENABLED (No silent placeholder MBI/DOB/DOS)")

    # 1. Parallel Pre-download
    with ThreadPoolExecutor(max_workers=6) as executor:
        download_tasks = []
        for fn, fid in file_items:
            lp = os.path.join(data_dir, fn)
            if not os.path.exists(lp) or os.path.getsize(lp) < 1000:
                download_tasks.append((fn, executor.submit(download_file, fid, lp)))
        for fn, fut in download_tasks:
            try:
                fut.result()
            except Exception as e:
                print(f"    [!] Failed to download {fn}: {e}")

    results: List[Dict[str, Any]] = []
    queued: List[Any] = []  # (result dict, 837 payload) for claims that passed every gate so far

    for idx, (filename, file_id) in enumerate(file_items, 1):
        print(f"--------------------------------------------------------------------------------")
        print(f"[{idx}/{len(file_items)}] Processing Document: {filename}")
        local_pdf = os.path.join(data_dir, filename)
        local_ocr_cache = os.path.join(data_dir, f"{filename}.ocr.txt")

        if not os.path.exists(local_pdf) or os.path.getsize(local_pdf) < 1000:
            success = download_file(file_id, local_pdf)
            if not success:
                results.append({
                    "index": idx,
                    "filename": filename,
                    "status": "DOWNLOAD_FAILED",
                    "issues": [{"code": "DOWNLOAD_FAILED", "message": "Failed to fetch file from Google Drive"}]
                })
                continue

        # 2. Ingestion & OCR
        if os.path.exists(local_ocr_cache):
            with open(local_ocr_cache, "r", encoding="utf-8") as f:
                doc_text = f.read()
        else:
            parsed_doc = pipeline.ingestor.ingest_document(local_pdf, DocumentSourceType.PDF_FILE)
            doc_text = parsed_doc.raw_text
            with open(local_ocr_cache, "w", encoding="utf-8") as f:
                f.write(doc_text)

        # 3. Strict Clinical Demographics Extraction (Zero silent placeholders)
        demo = pipeline.ingestor.extract_chart_demographics(doc_text)
        ov = overrides.get(filename, {})
        apply_overrides(demo, ov)
        p_name = demo.get("patient_name")
        mbi = demo.get("member_id")
        dob = demo.get("dob")
        dos = demo.get("date_of_service")
        pos = demo.get("place_of_service") or "31"
        encounter_pcn = f"ENC-SEP26-{batch_prefix}-{idx:03d}"

        missing_demographics = []
        if not p_name:
            missing_demographics.append("Patient Name (Box 2)")
        if not mbi:
            missing_demographics.append("Medicare Beneficiary ID / MBI (Box 1a)")
        if not dob:
            missing_demographics.append("Patient Birthdate / DOB (Box 3)")
        if not dos:
            missing_demographics.append("Date of Service / Consultation Date (Box 24A)")

        gate_issues = hard_gate_issues(demo, filename, ov)
        if missing_demographics or gate_issues:
            issues = []
            if missing_demographics:
                issue_msg = f"Strict Demographic Audit Failed — Missing required fields: {', '.join(missing_demographics)}. Document appears to be an unbillable log sheet or incomplete scan."
                issues.append({
                    "tier": "Tier 0: Strict Demographic & Face Sheet Gate",
                    "severity": "CRITICAL",
                    "code": "MISSING_REQUIRED_DEMOGRAPHICS",
                    "message": issue_msg
                })
            issues.extend(gate_issues)
            for gi in issues:
                print(f"   [HOLD] [{gi['code']}] {gi['message']}")
            pipeline.dispatcher.ledger.record_flagged_claim(
                pcn=encounter_pcn,
                encounter_id=encounter_pcn,
                patient_name=p_name or filename,
                total_charge=0.0,
                confidence_score=0.0,
                scrutiny_issues=issues,
                hcfa_missing=missing_demographics,
                physician_queries=[],
                encounter_payload={"encounter_id": encounter_pcn, "filename": filename}
            )
            results.append({
                "index": idx,
                "filename": filename,
                "patient_control_number": encounter_pcn,
                "patient_name": p_name or f"UNIDENTIFIED ({filename})",
                "dob": dob,
                "gender": demo.get("gender"),
                "member_id": mbi,
                "medicaid_id": demo.get("medicaid_id"),
                "secondary_payer": demo.get("secondary_payer"),
                "address": demo.get("address"),
                "city": demo.get("city"),
                "state": demo.get("state"),
                "zip": demo.get("zip"),
                "admission_date": demo.get("admission_date"),
                "date_of_service": dos,
                "place_of_service": pos,
                "facility_name": demo.get("facility_name"),
                "referring_provider": demo.get("referring_provider"),
                "attending_npi": demo.get("attending_npi"),
                "rendering_npi": demo.get("rendering_npi"),
                "consultant_provider": demo.get("consultant_provider"),
                "status": "FLAGGED_FOR_HUMAN_REVIEW",
                "review_tier": "HOLD",
                "cpt_codes": [],
                "modifiers": [],
                "dx_pointers": "",
                "icd10_codes": [],
                "total_charge": 0.0,
                "issues": issues,
                "confidence_score": 0.0
            })
            continue

        patient_data = {
            "patient_id": mbi,
            "first_name": demo.get("first_name"),
            "last_name": demo.get("last_name"),
            "dob": dob,
            "gender": demo["gender"],
            "address": demo["address"],
            "city": demo["city"],
            "state": demo["state"],
            "zip": demo["zip"],
            "member_id": mbi
        }

        provider_data = {
            "organization_name": billing_entity["organization_name"] or "BILLING ENTITY NOT CONFIGURED",
            "rendering_first_name": "NASSER",
            "rendering_last_name": "EFTEKHARI",
            "name": demo.get("consultant_provider") or "DR. NASSER EFTEKHARI",
            "npi": demo["rendering_npi"],
            "rendering_npi_type_1": demo["rendering_npi"],
            "billing_npi_type_2": billing_entity["billing_npi_type_2"] or "0000000000",
            "type": "MD",
            "specialty": "Physical Medicine & Rehabilitation",
            "state": billing_entity["state"] or "FL",
            "tax_id": billing_entity["tax_id"] or "000000000",
            "phone": billing_entity["phone"] or "0000000000",
            "contact_name": billing_entity["contact_name"],
            "address": billing_entity["address"] or "NOT CONFIGURED",
            "city": billing_entity["city"] or "NOT CONFIGURED",
            "zip": billing_entity["zip"] or "00000"
        }

        payer_data = {
            "name": "MEDICARE FLORIDA (FIRST COAST)",
            "payer_id": "09102",
            "type": "Medicare"
        }

        # 4. Pipeline Execution
        pipeline_res = pipeline.process_encounter(
            raw_doc_or_content=doc_text,
            doc_type=DocumentSourceType.OCR_TEXT,
            patient_data=patient_data,
            provider_data=provider_data,
            payer_data=payer_data,
            encounter_id=encounter_pcn,
            date_of_service=dos,
            place_of_service_override=ov.get("place_of_service") if ov.get("place_of_service") in ("31", "32") else None
        )

        status = pipeline_res.get("status")
        total_charge = pipeline_res.get("total_billed", 0.0)
        cpts = []
        mods = []
        dx_ptrs_str = "A"
        dxs = []
        if "billing_details" in pipeline_res:
            b = pipeline_res["billing_details"]
            total_charge = b.total_charge
            cpts = [c.code for c in b.cpt_codes]
            if b.cpt_codes:
                mods = b.cpt_codes[0].modifiers
                ptr_letters = [chr(64 + p) for p in b.cpt_codes[0].dx_pointers if 1 <= p <= 12]
                dx_ptrs_str = ",".join(ptr_letters)
            dxs = [d.code for d in b.diagnosis_codes]

        edi_payload = pipeline_res.get("edi_837p_payload")
        pipeline_clean = status == "TRANSMITTED_CLEAN_CLAIM"
        dx_problems = icd10_issues(dxs) if pipeline_clean else []
        if dx_problems:
            status = "FLAGGED_FOR_HUMAN_REVIEW"
            for dp in dx_problems:
                print(f"   [HOLD] [{dp['code']}] {dp['message']}")
        coder_notes = coder_review_notes(doc_text, dxs, ov) if status == "TRANSMITTED_CLEAN_CLAIM" else []
        review_tier = "HOLD" if status != "TRANSMITTED_CLEAN_CLAIM" else ("CODER_REVIEW" if coder_notes else "READY")
        billed_pos = pipeline_res["billing_details"].place_of_service if "billing_details" in pipeline_res else pos

        print(f"   [RESULT] {p_name} (MBI: {mbi}, DOB: {dob}, DOS: {dos}) -> {status} | CPT: {cpts} (${total_charge:.2f}) | ICD-10: {dxs}")

        results.append({
            "index": idx,
            "filename": filename,
            "patient_control_number": encounter_pcn,
            "patient_name": p_name,
            "dob": dob,
            "gender": demo["gender"],
            "member_id": mbi,
            "medicaid_id": demo.get("medicaid_id"),
            "secondary_payer": demo.get("secondary_payer"),
            "address": demo.get("address"),
            "city": demo.get("city"),
            "state": demo.get("state"),
            "zip": demo.get("zip"),
            "admission_date": demo.get("admission_date"),
            "date_of_service": dos,
            "place_of_service": billed_pos,
            "facility_name": demo.get("facility_name"),
            "referring_provider": demo.get("referring_provider"),
            "attending_npi": None,
            "rendering_npi": demo["rendering_npi"],
            "consultant_provider": demo.get("consultant_provider") or "Dr. Nasser Eftekhari, MD",
            "status": status,
            "cpt_codes": cpts,
            "modifiers": mods,
            "dx_pointers": dx_ptrs_str,
            "icd10_codes": dxs,
            "total_charge": total_charge,
            "issues": list(pipeline_res.get("scrutiny_issues", [])) + dx_problems + coder_notes,
            "review_tier": review_tier,
            "billing_npi": billing_entity["billing_npi_type_2"],
            "confidence_score": pipeline_res.get("confidence_score")
        })
        if review_tier != "HOLD" and edi_payload:
            queued.append((results[-1], edi_payload))

    # 4b. X12 5010 validation (pyx12): any claim whose 837 fails the implementation guide is held.
    print(f"[*] Validating {len(queued)} claim(s) against the 5010 837P implementation guide (pyx12)...")
    for res, payload in queued:
        x12_errors = validate_x12_837(payload)
        if x12_errors:
            res["review_tier"] = "HOLD"
            res["status"] = "FLAGGED_FOR_HUMAN_REVIEW"
            res["issues"].append({"tier": "Tier 0: X12 Validation", "severity": "CRITICAL", "code": "X12_VALIDATION_FAILED",
                                  "message": "; ".join(e.split(" - ", 1)[-1] for e in x12_errors[:3])})
            print(f"   [HOLD] [X12_VALIDATION_FAILED] {res['patient_control_number']}: {x12_errors[0][:160]}")
    clean_edi_payloads = [payload for res, payload in queued if res["review_tier"] != "HOLD"]

    # 5. Export Consolidated Office Ally Batch .837 File
    # Submittable only when the billing entity is fully configured AND any claims with
    # coder-review flags have been signed off; otherwise it is written as a DRAFT.
    needs_coder = [r for r in results if r.get("review_tier") == "CODER_REVIEW"]
    draft_reasons = list(entity_problems)
    if needs_coder and not coder_signoff:
        draft_reasons.append(f"{len(needs_coder)} claim(s) await coder sign-off (--coder-signoff NAME)")
    submit_ready = not draft_reasons
    name_837 = f"OFFICE_ALLY_BATCH_{batch_prefix}.837" if submit_ready else f"DRAFT_NOT_FOR_SUBMISSION_{batch_prefix}.837"
    for stale in (f"OFFICE_ALLY_BATCH_{batch_prefix}.837", f"DRAFT_NOT_FOR_SUBMISSION_{batch_prefix}.837"):
        if stale != name_837 and os.path.exists(os.path.join("exports", stale)):
            os.remove(os.path.join("exports", stale))
    batch_837_path = f"exports/{name_837}"
    consolidated_837 = pipeline.x12_generator.generate_batch_837p(clean_edi_payloads)
    with open(batch_837_path, "w", encoding="utf-8") as f:
        f.write(consolidated_837)
    if coder_signoff:
        print(f"[*] Coder sign-off recorded: {coder_signoff}")
    print(f"[*] 837 status: {'SUBMITTABLE' if submit_ready else 'DRAFT - ' + '; '.join(draft_reasons)}")

    template = {"_instructions": "Resolve each claim: fix values, set name_verified / primary_payer_confirmed after checking, and set place_of_service to 31 (Medicare Part A SNF stay) or 32 (nursing facility, no Part A). Save as overrides.json in the data dir and rerun."}
    for r in results:
        if r.get("review_tier") in ("HOLD", "CODER_REVIEW"):
            template[r["filename"]] = {
                "_why": [i.get("code") for i in r.get("issues", [])],
                "first_name": (r.get("patient_name") or "").partition(", ")[2] or None,
                "last_name": (r.get("patient_name") or "").partition(", ")[0] or None,
                "gender": r.get("gender"), "address": r.get("address"), "city": r.get("city"),
                "state": r.get("state"), "zip": r.get("zip"),
                "place_of_service": None,
                "name_verified": False, "primary_payer_confirmed": False,
            }
    with open("exports/overrides_template.json", "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)

    # 6. Export CMS-1500 Biller Worklist CSV
    csv_path = "exports/september_2026_cms1500_biller_sheet.csv"
    export_biller_csv(results, csv_path)

    # 7. Export Email Digest HTML + RFC-5322 .eml Package (with CSV & .837 attached)
    html_path = "exports/september_2026_email_digest.html"
    eml_path = "exports/september_2026_biller_package.eml"
    build_email_package(results, csv_path, batch_837_path, html_path, eml_path, recipient_email=email_to)

    # 8. Output Summary JSON & Markdown Table
    summary_path = "september_batch_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    md_path = "september_batch_report.md"
    clean_claims = [r for r in results if r.get("status") == "TRANSMITTED_CLEAN_CLAIM"]
    flagged = [r for r in results if r.get("status") != "TRANSMITTED_CLEAN_CLAIM"]
    clean_billed = sum(r.get("total_charge", 0.0) for r in clean_claims)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# September 2026 Batch Ingestion Report (Production Biller Package)\n\n")
        f.write(f"- **Total Documents Processed:** {len(results)}\n")
        f.write(f"- **Clean Claims Ready for Office Ally:** {len(clean_claims)} ({len(clean_claims)/len(results)*100:.1f}%)\n")
        f.write(f"- **Held for Biller Review:** {len(flagged)} ({len(flagged)/len(results)*100:.1f}%)\n")
        f.write(f"- **Total Clean Billed Charges:** ${clean_billed:,.2f}\n")
        f.write(f"- **Consolidated Office Ally 837P File:** `{batch_837_path}` ({len(consolidated_837):,} bytes)\n")
        f.write(f"- **CMS-1500 Excel/CSV Worklist:** `{csv_path}`\n")
        f.write(f"- **Email-Ready Package (.eml):** `{eml_path}`\n\n")
        f.write("## Batch Reconciliation Table\n\n")
        f.write("| # | Patient Name | DOB | MBI | PCN | DOS | POS | CPT | Diagnoses | Charge | Status |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in results:
            cpt_str = ", ".join(r["cpt_codes"]) if r["cpt_codes"] else "HOLD"
            dx_str = ", ".join(r["icd10_codes"][:4]) if r["icd10_codes"] else "None"
            if len(r["icd10_codes"]) > 4:
                dx_str += f" (+{len(r['icd10_codes'])-4})"
            f.write(
                f"| {r['index']} | {r['patient_name']} | {r.get('dob') or 'MISSING'} | `{r.get('member_id') or 'MISSING'}` "
                f"| `{r['patient_control_number']}` | {r.get('date_of_service') or 'MISSING'} | {r['place_of_service']} "
                f"| {cpt_str} | {dx_str} | ${r['total_charge']:.2f} | `{r['status']}` |\n"
            )

    print("\n" + "=" * 80)
    print("▶ BATCH PROCESSING COMPLETE: MEDICAL BILLER DELIVERABLES GENERATED")
    print("=" * 80)
    print(f"Total Documents Evaluated:        {len(results)}")
    print(f"  Clean 837P Claims Ready:        {len(clean_claims)} ({len(clean_claims)/len(results)*100:.1f}%)")
    print(f"  Held for Biller Review:         {len(flagged)} ({len(flagged)/len(results)*100:.1f}%)")
    print(f"  Total Clean Billed Charges:     ${clean_billed:,.2f}")
    print(f"  1. Office Ally Master .837:     {batch_837_path} ({len(consolidated_837):,} bytes)")
    print(f"  2. CMS-1500 Excel/CSV Sheet:    {csv_path}")
    print(f"  3. Email Package (.eml):        {eml_path}")
    print(f"  4. HTML Email Digest:           {html_path}")
    print("=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run September 2026 Batch Ingestion & Export Biller Package")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of documents to process")
    parser.add_argument("--batch-prefix", type=str, default="SEP26", help="Batch prefix for unique patient control numbers")
    parser.add_argument("--email-to", type=str, default=None, help="Optional recipient email address for SMTP dispatch")
    parser.add_argument("--overrides", type=str, default=None, help="Biller corrections JSON (default: <data-dir>/overrides.json)")
    parser.add_argument("--coder-signoff", type=str, default=None, help="Name of the coder who reviewed CODER_REVIEW claims")
    args = parser.parse_args()
    run_batch(limit=args.limit, batch_prefix=args.batch_prefix, email_to=args.email_to,
              overrides_path=args.overrides, coder_signoff=args.coder_signoff)
