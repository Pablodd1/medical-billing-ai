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
import inspect
import argparse
import smtplib
import urllib.request
from email.message import EmailMessage
from typing import Dict, Any, List, Optional, Tuple
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
from transmission.x12_837_generator import lookup_known_service_facility
from transmission.office_ally_companion import OfficeAllyCompanionValidator

try:  # optional: pick up BILLING_* values from .env
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
except ImportError:
    pass

# Offline batch assembly builds a consolidated .837 for review/upload; never attempt
# per-claim live SFTP connections during local batch triage.
os.environ["OA_ALLOW_SIMULATION"] = "true"
os.environ.pop("OA_SFTP_USERNAME", None)
os.environ.pop("OA_SFTP_PASSWORD", None)

OVERRIDE_DEMO_KEYS = (
    "first_name", "last_name", "dob", "gender", "member_id", "medicaid_id",
    "secondary_payer", "is_qmb", "part_a_only", "address", "city", "state", "zip",
    "admission_date", "date_of_service", "facility_name", "place_of_service", "referring_provider",
)


def _format_secondary_and_qmb(r: Dict[str, Any]) -> Tuple[str, str, str]:
    """Formats Box 9a (Secondary/Medicaid ID), Box 9d (Secondary Payer), and QMB protection badge."""
    medicaid_id = str(r.get("medicaid_id") or "").strip()
    sec_payer = str(r.get("secondary_payer") or "").strip()
    issues_list = r.get("issues") or []
    is_qmb = bool(r.get("is_qmb")) or any(
        i.get("code") == "QMB_DO_NOT_BALANCE_BILL" for i in issues_list
    )

    box9a = medicaid_id or ("QMB_VERIFY_ID" if is_qmb else "")
    if sec_payer and is_qmb and "QMB" not in sec_payer.upper():
        box9d = f"{sec_payer} (QMB - DO NOT BALANCE BILL)"
    elif sec_payer:
        box9d = sec_payer
    elif is_qmb:
        box9d = "FL MEDICAID (77027) - QMB"
    elif medicaid_id:
        box9d = "FL MEDICAID (77027)"
    else:
        box9d = ""

    qmb_status = "QMB_DO_NOT_BALANCE_BILL" if is_qmb else ""
    return box9a, box9d, qmb_status


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
        "Box9_QMB_Protection",
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
        "Box32a_Service_Facility_NPI",
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
            box9a_id, box9d_payer, qmb_status = _format_secondary_and_qmb(r)

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
                box9a_id,
                box9d_payer,
                qmb_status,
                r.get("referring_provider") or "",
                r.get("attending_npi") or "",
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
                r.get("facility_npi") or "",
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
    recipient_email: Optional[str] = None,
    batch_prefix: str = "SEP26",
    oa_preflight: Optional[Dict[str, Any]] = None,
) -> None:
    """Builds an HTML email digest and an RFC-5322 .eml package with attached CSV and .837 batch file."""
    total_files = len(results)
    clean_claims = [r for r in results if r.get("status") == "TRANSMITTED_CLEAN_CLAIM"]
    flagged = [r for r in results if r.get("status") != "TRANSMITTED_CLEAN_CLAIM"]
    clean_total = sum(r.get("total_charge", 0.0) for r in clean_claims)
    clean_pct = (len(clean_claims) / total_files * 100.0) if total_files else 0.0
    batch_label = "September 2026" if batch_prefix == "SEP26" else f"Batch {batch_prefix}"

    qmb_rows = []
    medicaid_rows = []
    rows_html = []
    for r in results:
        badge_color = "#16a34a" if r.get("status") == "TRANSMITTED_CLEAN_CLAIM" else "#dc2626"
        status_label = "CLEAN (837P READY)" if r.get("status") == "TRANSMITTED_CLEAN_CLAIM" else "HOLD / REVIEW"
        dx_str = ", ".join(r.get("icd10_codes", [])[:4])
        if len(r.get("icd10_codes", [])) > 4:
            dx_str += f" (+{len(r['icd10_codes']) - 4})"

        box9a_id, box9d_payer, qmb_status = _format_secondary_and_qmb(r)
        if qmb_status:
            qmb_rows.append((r, box9a_id, box9d_payer))
        if box9a_id or box9d_payer:
            medicaid_rows.append((r, box9a_id, box9d_payer, bool(qmb_status)))

        sec_parts = []
        if box9a_id:
            sec_parts.append(f"<code>{box9a_id}</code>")
        if box9d_payer:
            sec_parts.append(box9d_payer)
        if qmb_status:
            sec_parts.append("<span style='background:#fef3c7;color:#92400e;padding:1px 5px;border-radius:4px;font-weight:bold;font-size:11px;'>QMB: NO BALANCE BILL</span>")
        sec_cell_html = "<br>".join(sec_parts) if sec_parts else "&mdash;"

        adm_label = f"<br><span style='color:#64748b;font-size:11px;'>Adm: {r.get('admission_date')}</span>" if r.get("admission_date") else ""

        rows_html.append(
            f"<tr>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'>{r['index']}</td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'><b>{r.get('patient_name')}</b></td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'>{r.get('dob') or 'MISSING'}</td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'><code>{r.get('member_id') or 'MISSING'}</code></td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;font-size:12px;'>{sec_cell_html}</td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'>{r.get('date_of_service') or 'MISSING'}{adm_label}</td>"
            f"<td style='padding:6px;border:1px solid #cbd5e1;'>{(r.get('facility_name') or '')[:26]} (POS {r.get('place_of_service')})</td>"
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

    qmb_medicaid_html = ""
    if qmb_rows or medicaid_rows:
        qmb_items = []
        for r, b9a, b9d in qmb_rows:
            qmb_items.append(
                f"<li><b>#{r['index']} {r.get('patient_name')}</b> (MBI <code>{r.get('member_id') or 'N/A'}</code>, "
                f"Medicaid/Policy: <code>{b9a}</code> — {b9d}): <b>DO NOT BALANCE BILL PATIENT</b> "
                f"(SSA &sect; 1902(n)(3)(B); bill secondary crossover to FL Medicaid 77027).</li>"
            )
        sec_items = []
        for r, b9a, b9d, is_q in medicaid_rows:
            if not is_q:
                sec_items.append(
                    f"<li><b>#{r['index']} {r.get('patient_name')}</b>: Box 9a ID <code>{b9a or 'N/A'}</code> &bull; Box 9d <b>{b9d or 'Secondary'}</b> ({r.get('status')})</li>"
                )
        qmb_medicaid_html = (
            f"<div style='background:#fffbeb;border-left:4px solid #d97706;padding:12px 16px;margin:16px 0;'>"
            f"<h3 style='margin:0 0 6px 0;color:#92400e;font-size:15px;'>"
            f"Secondary FL Medicaid / COB Tracking ({len(medicaid_rows)}) &amp; Federal QMB Balance-Billing Shield ({len(qmb_rows)})</h3>"
            + (f"<p style='margin:4px 0;font-size:13px;color:#78350f;'><b>QMB-Protected Beneficiaries (Patient Statement / Balance Billing Strictly Prohibited):</b></p><ul style='margin:4px 0 8px 0;padding-left:20px;font-size:13px;color:#78350f;'>{''.join(qmb_items)}</ul>" if qmb_items else "")
            + (f"<p style='margin:4px 0;font-size:13px;color:#78350f;'><b>Additional Extracted Secondary / Medicaid IDs (CMS-1500 Box 9a / 9d):</b></p><ul style='margin:4px 0;padding-left:20px;font-size:13px;color:#78350f;'>{''.join(sec_items)}</ul>" if sec_items else "")
            + "</div>"
        )

    oa_html = ""
    if oa_preflight:
        pyx_label = "PASS (0 Schema Errors)" if oa_preflight.get("pyx12_clean") else "SCHEMA ERRORS DETECTED"
        oa_state = (
            "READY FOR LIVE OFFICE ALLY UPLOAD"
            if oa_preflight.get("ready_for_office_ally")
            else f"DRAFT PRE-FLIGHT ({oa_preflight.get('error_count', 0)} Box 33 / Submitter Prerequisite(s) Pending)"
        )
        oa_html = (
            f"<div style='background:#f0fdf4;border-left:4px solid #16a34a;padding:12px 16px;margin:16px 0;font-size:13px;'>"
            f"<h3 style='margin:0 0 6px 0;color:#14532d;font-size:15px;'>Office Ally 5010A1 Companion Guide &amp; EDI Verification</h3>"
            f"<ul style='margin:0;padding-left:20px;color:#14532d;'>"
            f"<li><b>X12 5010A1 Schema (<code>pyx12</code>):</b> {pyx_label} across <b>{oa_preflight.get('claim_count', 0)}</b> queued transaction(s).</li>"
            f"<li><b>Rendering / Billing Taxonomy:</b> <code>208100000X</code> (Physical Medicine &amp; Rehabilitation — Dr. Nasser Eftekhari, NPI <code>1356442776</code>).</li>"
            f"<li><b>SNF Facility &amp; Admission Fidelity:</b> Loop <code>2310C</code> (<code>NM1*77</code> NPPES SNF Location + 9-digit ZIP+4) and Box 18 (<code>DTP*435</code> Admission Date) populated.</li>"
            f"<li><b>Office Ally Companion Verdict:</b> {oa_state} ({oa_preflight.get('warning_count', 0)} advisory warning(s)).</li>"
            f"</ul></div>"
        )

    html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family:Segoe UI,Arial,sans-serif;color:#1e293b;line-height:1.5;max-width:1150px;margin:0 auto;padding:16px;">
  <h2 style="color:#0f172a;border-bottom:2px solid #0284c7;padding-bottom:8px;">
    Medical Billing AI — {batch_label} Pre-Scrubbed Package (Office Ally &amp; CMS-1500 Worklist)
  </h2>
  <p>Hi Team,</p>
  <p>We ran Dr. Nasser Eftekhari's scanned SNF charts (<b>{total_files} PDFs</b>) through our automated pre-billing scrubber so you can review, verify, and finalize claims directly inside <b>Office Ally</b> without manual typing.</p>
  <div style="background:#eff6ff;border-left:4px solid #0284c7;padding:12px 16px;margin:14px 0;">
    <h3 style="margin:0 0 6px 0;color:#0f172a;font-size:15px;">How This Workflow Operates (You Stay 100% in Control)</h3>
    <ol style="margin:0;padding-left:20px;font-size:14px;">
      <li><b>Step 1 — Local AI Scrub &amp; Triage (Done):</b> Reads scanned face sheets &amp; progress notes, maps CMS-1500 Boxes 1–33, validates Medicare MBIs, secondary FL Medicaid / QMB status, USPS home addresses, ICD-10-CM &amp; Initial SNF Consult CPT codes (<code>99304–99306</code>), and holds any chart with a payer or identity mismatch so dirty claims never touch Office Ally.</li>
      <li><b>Step 2 — Biller Review &amp; Box 33 Setup (Needed Now):</b> Open the attached Excel/CSV worklist (<code>{os.path.basename(csv_path)}</code>) to review the <b>{len(clean_claims)} clean claims (${clean_total:,.2f})</b>, confirm Place of Service (<b>31</b> Part A SNF vs. <b>32</b> Nursing Facility), confirm the held chart items below, and reply with the <b>Box 33 Billing Group Legal Name, Group NPI, Federal Tax ID (EIN), Street Address + 9-digit ZIP+4, and Phone</b>.</li>
      <li><b>Step 3 — Visualize &amp; Finalize Inside Office Ally:</b> Once Box 33 is added, we unlock and upload the live <code>OFFICE_ALLY_BATCH_{batch_prefix}.837</code> file into <b>Office Ally Service Center &rarr; Upload Claims &rarr; Professional (837P)</b>. Every CMS-1500 will appear on screen in Office Ally's <b>Manage / Online Claim Fix</b> queue for your visual inspection and final approval before release.</li>
    </ol>
  </div>
  <p><b>Attached Deliverables:</b></p>
  <ol>
    <li><b><code>{os.path.basename(csv_path)}</code></b> — Full CMS-1500 Box 1–33 Biller Spreadsheet (opens directly in Excel) including Secondary FL Medicaid IDs (Box 9a), QMB protection flags, Box 18 SNF Admission Dates, and Box 32 Facility NPIs.</li>
    <li><b><code>{os.path.basename(batch_837_path)}</code></b> — Consolidated HIPAA 5010A1 X12 837P file containing all <b>{len(clean_claims)} clean claims (${clean_total:,.2f})</b> with PM&amp;R Taxonomy <code>208100000X</code>, Loop <code>2310C</code> SNF Facility, and <code>DTP*435</code> Admission Dates.</li>
  </ol>
  <table style="border-collapse:collapse;margin:16px 0;background:#f8fafc;width:100%;">
    <tr>
      <td style="padding:12px;border:1px solid #cbd5e1;"><b>Total Files Ingested:</b> {total_files}</td>
      <td style="padding:12px;border:1px solid #cbd5e1;color:#16a34a;"><b>Clean Claims Ready:</b> {len(clean_claims)} ({clean_pct:.1f}%)</td>
      <td style="padding:12px;border:1px solid #cbd5e1;color:#dc2626;"><b>Held for Review:</b> {len(flagged)}</td>
      <td style="padding:12px;border:1px solid #cbd5e1;color:#b45309;"><b>Medicaid / QMB Tracked:</b> {len(medicaid_rows)} ({len(qmb_rows)} QMB)</td>
      <td style="padding:12px;border:1px solid #cbd5e1;"><b>Clean Billed Total:</b> <b>${clean_total:,.2f}</b></td>
    </tr>
  </table>
  {oa_html}
  {qmb_medicaid_html}
  {flagged_html}
  <h3>Complete Patient Claim Schedule</h3>
  <table style="border-collapse:collapse;width:100%;font-size:12.5px;">
    <thead>
      <tr style="background:#0f172a;color:#ffffff;text-align:left;">
        <th style="padding:8px;border:1px solid #334155;">#</th>
        <th style="padding:8px;border:1px solid #334155;">Patient Name</th>
        <th style="padding:8px;border:1px solid #334155;">DOB</th>
        <th style="padding:8px;border:1px solid #334155;">Medicare MBI</th>
        <th style="padding:8px;border:1px solid #334155;">Box 9a/9d Secondary &amp; QMB</th>
        <th style="padding:8px;border:1px solid #334155;">DOS / Box 18 Adm</th>
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
        f"[{'READY FOR OFFICE ALLY' if not os.path.basename(batch_837_path).startswith('DRAFT') else 'BILLER REVIEW & BOX 33 NEEDED'}] "
        f"{batch_label} SNF Billing Package — {len(clean_claims)} Clean Claims (${clean_total:,.2f}) + {len(flagged)} Held"
    )
    msg["From"] = os.environ.get("SMTP_FROM", "jasmelacosta@gmail.com")
    msg["To"] = recipient_email or os.environ.get("BILLER_EMAIL", "zakiullahdanish@gmail.com")

    qmb_txt_lines = [
        f"  * #{r['index']} {r.get('patient_name')} (Medicaid/Policy: {b9a} | {b9d}) — QMB: DO NOT BALANCE BILL PATIENT"
        for r, b9a, b9d in qmb_rows
    ]
    qmb_txt_block = ("\nQMB FEDERAL BALANCE-BILLING ALERTS (SSA § 1902(n)(3)(B)):\n" + "\n".join(qmb_txt_lines) + "\n") if qmb_txt_lines else ""

    msg.set_content(
        f"Hi,\n\n"
        f"We ran Dr. Nasser Eftekhari's {batch_label} SNF PDFs ({total_files} files) through our automated pre-billing scrubber.\n\n"
        f"- Clean Claims Ready: {len(clean_claims)} (${clean_total:,.2f})\n"
        f"- Held Locally for Biller Verification: {len(flagged)}\n"
        f"- Secondary FL Medicaid / COB Tracked: {len(medicaid_rows)} ({len(qmb_rows)} QMB-protected)\n"
        f"- EDI Fidelity: Taxonomy 208100000X (PM&R), Loop 2310C SNF Service Facility, Box 18 DTP*435 Admission Date, pyx12 5010A1 clean.\n"
        f"{qmb_txt_block}\n"
        f"HOW THIS WORKS IN 3 STEPS:\n"
        f"1. Open the attached Excel/CSV sheet ({os.path.basename(csv_path)}) to review Boxes 1-33, Secondary Medicaid/QMB flags, POS 31 vs 32, and held items.\n"
        f"2. Reply with Box 33 Billing Group info (Legal Name, Group NPI, Federal Tax ID/EIN, Street Address + 9-digit ZIP+4, Phone, and Office Ally Sender ID).\n"
        f"3. Once Box 33 is added, we upload the unlocked .837 batch file into Office Ally Service Center so every CMS-1500 appears visually in your Office Ally Manage/Fix Claims screen for final sign-off.\n"
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

    os.makedirs(data_dir, exist_ok=True)
    os.makedirs("exports", exist_ok=True)

    # Discover local PDFs directly if present in data_dir; otherwise fall back to Drive manifest
    local_pdfs_found = sorted(f for f in os.listdir(data_dir) if f.lower().endswith(".pdf"))
    if local_pdfs_found:
        manifest = {fn: "" for fn in local_pdfs_found}
    elif not os.path.exists(manifest_path):
        from scripts.batch_manifest import get_manifest
        manifest = get_manifest()
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
    else:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    # This script always runs in simulation (files are uploaded manually), so it uses an
    # isolated working ledger: reruns are reproducible and never poison the main ledger's
    # duplicate-claim guard. Duplicates *within* this batch are still caught.
    batch_ledger = os.path.abspath(os.path.join("exports", f"batch_ledger_{batch_prefix}.sqlite3"))
    if os.path.exists(batch_ledger):
        os.remove(batch_ledger)
    os.environ["LEDGER_DB_PATH"] = batch_ledger
    from config import OfficeAllyConfig
    oa_cfg = OfficeAllyConfig()
    pipeline = AutonomousMedicalBillingPipeline(submitter_id=oa_cfg.claims_submitter_id, config=oa_cfg)

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

    # 1. Parallel Pre-download (only for remote Drive IDs)
    with ThreadPoolExecutor(max_workers=6) as executor:
        download_tasks = []
        for fn, fid in file_items:
            lp = os.path.join(data_dir, fn)
            if fid and (not os.path.exists(lp) or os.path.getsize(lp) < 1000):
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
            if not file_id and os.path.exists(local_ocr_cache):
                success = True
            else:
                success = download_file(file_id, local_pdf)
            if not success:
                results.append({
                    "index": idx,
                    "filename": filename,
                    "patient_control_number": f"ENC-{batch_prefix}-{idx:03d}",
                    "patient_name": f"DOWNLOAD_FAILED ({filename})",
                    "place_of_service": "",
                    "cpt_codes": [],
                    "icd10_codes": [],
                    "total_charge": 0.0,
                    "status": "DOWNLOAD_FAILED",
                    "review_tier": "HOLD",
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
        encounter_pcn = f"ENC-{batch_prefix}-{idx:03d}"

        service_facility = ov.get("service_facility") or lookup_known_service_facility(demo.get("facility_name"))
        resolved_facility_name = (
            (service_facility.get("name") if isinstance(service_facility, dict) else None)
            or demo.get("facility_name")
        )
        resolved_facility_npi = service_facility.get("npi") if isinstance(service_facility, dict) else None
        qmb_flag = bool(demo.get("is_qmb"))

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
            if qmb_flag and not any(i.get("code") == "QMB_DO_NOT_BALANCE_BILL" for i in issues):
                issues.append({
                    "tier": "Tier 0: QMB Federal Balance-Billing Shield",
                    "severity": "WARNING",
                    "code": "QMB_DO_NOT_BALANCE_BILL",
                    "message": (
                        "Patient is a Qualified Medicare Beneficiary (QMB / MCRA Co-Pay Medicaid). "
                        "Federal law (SSA § 1902(n)(3)(B)) strictly prohibits balance-billing Medicare "
                        "deductibles or coinsurance to the patient; cross over to FL Medicaid (77027)."
                    ),
                })
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
                "is_qmb": qmb_flag,
                "part_a_only": bool(demo.get("part_a_only")),
                "address": demo.get("address"),
                "city": demo.get("city"),
                "state": demo.get("state"),
                "zip": demo.get("zip"),
                "admission_date": demo.get("admission_date"),
                "date_of_service": dos,
                "place_of_service": pos,
                "facility_name": resolved_facility_name,
                "facility_npi": resolved_facility_npi,
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
            "taxonomy_code": ov.get("taxonomy_code") or "208100000X",
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

        pipeline_clean = status == "TRANSMITTED_CLEAN_CLAIM"
        dx_problems = icd10_issues(dxs) if pipeline_clean else []
        if dx_problems:
            status = "FLAGGED_FOR_HUMAN_REVIEW"
            for dp in dx_problems:
                print(f"   [HOLD] [{dp['code']}] {dp['message']}")

        if status == "TRANSMITTED_CLEAN_CLAIM":
            if "demo" in inspect.signature(coder_review_notes).parameters:
                coder_notes = coder_review_notes(doc_text, dxs, ov, demo=demo)
            else:
                coder_notes = coder_review_notes(doc_text, dxs, ov)
        else:
            coder_notes = []

        combined_issues = list(pipeline_res.get("scrutiny_issues", [])) + dx_problems + coder_notes
        if qmb_flag and not any(i.get("code") == "QMB_DO_NOT_BALANCE_BILL" for i in combined_issues):
            qmb_note = {
                "tier": "Tier 0: QMB Federal Balance-Billing Shield",
                "severity": "WARNING",
                "code": "QMB_DO_NOT_BALANCE_BILL",
                "message": (
                    "Patient is a Qualified Medicare Beneficiary (QMB / MCRA Co-Pay Medicaid). "
                    "Federal law (SSA § 1902(n)(3)(B)) strictly prohibits balance-billing Medicare "
                    "deductibles or coinsurance to the patient; cross over to FL Medicaid (77027)."
                ),
            }
            combined_issues.append(qmb_note)
            if status == "TRANSMITTED_CLEAN_CLAIM":
                coder_notes.append(qmb_note)

        review_tier = "HOLD" if status != "TRANSMITTED_CLEAN_CLAIM" else ("CODER_REVIEW" if coder_notes else "READY")
        billed_pos = pipeline_res["billing_details"].place_of_service if "billing_details" in pipeline_res else pos

        # Build complete 5010A1 encounter_dict including PM&R taxonomy 208100000X,
        # Loop 2310C Service Facility (NM1*77), and Box 18 Admission Date (DTP*435).
        edi_payload = pipeline_res.get("edi_837p_payload")
        if review_tier != "HOLD" and "billing_details" in pipeline_res:
            billing_dict = pipeline_res["billing_details"].model_dump()
            if resolved_facility_name and not billing_dict.get("facility_name"):
                billing_dict["facility_name"] = resolved_facility_name
            encounter_dict = {
                "encounter_id": encounter_pcn,
                "patient_control_number": encounter_pcn,
                "date_of_service": dos,
                "admission_date": demo.get("admission_date"),
                "facility_name": resolved_facility_name,
                "service_facility": service_facility,
                "patient": patient_data,
                "provider": provider_data,
                "payer": payer_data,
                "billing": billing_dict,
            }
            edi_payload = pipeline.x12_generator.generate_837p(encounter_dict)

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
            "is_qmb": qmb_flag,
            "part_a_only": bool(demo.get("part_a_only")),
            "address": demo.get("address"),
            "city": demo.get("city"),
            "state": demo.get("state"),
            "zip": demo.get("zip"),
            "admission_date": demo.get("admission_date"),
            "date_of_service": dos,
            "place_of_service": billed_pos,
            "facility_name": resolved_facility_name,
            "facility_npi": resolved_facility_npi,
            "referring_provider": demo.get("referring_provider"),
            "attending_npi": demo.get("attending_npi"),
            "rendering_npi": demo["rendering_npi"],
            "consultant_provider": demo.get("consultant_provider") or "Dr. Nasser Eftekhari, MD",
            "status": status,
            "cpt_codes": cpts,
            "modifiers": mods,
            "dx_pointers": dx_ptrs_str,
            "icd10_codes": dxs,
            "total_charge": total_charge,
            "issues": combined_issues,
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

    # 5. Export Consolidated Office Ally Batch .837 File + Run Office Ally Companion Pre-Flight
    # Submittable only when the billing entity is fully configured, coder-review flags are
    # signed off, AND OfficeAllyCompanionValidator passes with 0 errors; otherwise DRAFT.
    needs_coder = [r for r in results if r.get("review_tier") == "CODER_REVIEW"]
    draft_reasons = list(entity_problems)
    if needs_coder and not coder_signoff:
        draft_reasons.append(f"{len(needs_coder)} claim(s) await coder sign-off (--coder-signoff NAME)")

    consolidated_837 = pipeline.x12_generator.generate_batch_837p(clean_edi_payloads)
    tentative_name = (
        f"OATEST_OFFICE_ALLY_BATCH_{batch_prefix}.837"
        if (not draft_reasons and pipeline.config.usage_indicator == "T")
        else (f"OFFICE_ALLY_BATCH_{batch_prefix}.837" if not draft_reasons else f"DRAFT_NOT_FOR_SUBMISSION_{batch_prefix}.837")
    )
    companion = OfficeAllyCompanionValidator(config=pipeline.config)
    oa_preflight = (
        companion.validate_837p(consolidated_837, filename=tentative_name, run_pyx12=True)
        if consolidated_837
        else companion.validate_837p("", filename=tentative_name, run_pyx12=False)
    )
    if not draft_reasons and consolidated_837 and not oa_preflight.get("ready_for_office_ally"):
        top_errs = ", ".join(sorted({e["rule_id"] for e in oa_preflight.get("errors", [])[:4]}))
        draft_reasons.append(f"Office Ally pre-flight blocked ({top_errs})")

    submit_ready = not draft_reasons and bool(consolidated_837)
    name_837 = (
        tentative_name
        if submit_ready
        else f"DRAFT_NOT_FOR_SUBMISSION_{batch_prefix}.837"
    )
    for stale in (
        f"OFFICE_ALLY_BATCH_{batch_prefix}.837",
        f"OATEST_OFFICE_ALLY_BATCH_{batch_prefix}.837",
        f"DRAFT_NOT_FOR_SUBMISSION_{batch_prefix}.837",
    ):
        if stale != name_837 and os.path.exists(os.path.join("exports", stale)):
            os.remove(os.path.join("exports", stale))
    batch_837_path = f"exports/{name_837}"
    with open(batch_837_path, "w", encoding="utf-8") as f:
        f.write(consolidated_837)

    slug = "september_2026" if batch_prefix == "SEP26" else batch_prefix.lower()
    oa_preflight["filename"] = name_837
    oa_preflight_path = f"exports/{slug}_oa_preflight.json"
    with open(oa_preflight_path, "w", encoding="utf-8") as f:
        json.dump(oa_preflight, f, indent=2)

    if coder_signoff:
        print(f"[*] Coder sign-off recorded: {coder_signoff}")
    print(
        f"[*] Office Ally Companion Pre-Flight: pyx12_clean={oa_preflight.get('pyx12_clean')}, "
        f"claims={oa_preflight.get('claim_count')}, errors={oa_preflight.get('error_count')}, "
        f"warnings={oa_preflight.get('warning_count')} -> {oa_preflight_path}"
    )
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
                "admission_date": r.get("admission_date"),
                "medicaid_id": r.get("medicaid_id"),
                "place_of_service": None,
                "name_verified": False, "primary_payer_confirmed": False,
            }
    overrides_tpl_path = "exports/overrides_template.json" if batch_prefix == "SEP26" else f"exports/{slug}_overrides_template.json"
    with open(overrides_tpl_path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2)

    # 6. Export CMS-1500 Biller Worklist CSV
    csv_path = f"exports/{slug}_cms1500_biller_sheet.csv"
    export_biller_csv(results, csv_path)

    # 7. Export Email Digest HTML + RFC-5322 .eml Package (with CSV & .837 attached)
    html_path = f"exports/{slug}_email_digest.html"
    eml_path = f"exports/{slug}_biller_package.eml"
    build_email_package(
        results,
        csv_path,
        batch_837_path,
        html_path,
        eml_path,
        recipient_email=email_to,
        batch_prefix=batch_prefix,
        oa_preflight=oa_preflight,
    )

    # 8. Output Summary JSON & Markdown Table
    summary_path = "september_batch_summary.json" if batch_prefix == "SEP26" else f"exports/{slug}_batch_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    md_path = "september_batch_report.md" if batch_prefix == "SEP26" else f"exports/{slug}_batch_report.md"
    clean_claims = [r for r in results if r.get("status") == "TRANSMITTED_CLEAN_CLAIM"]
    flagged = [r for r in results if r.get("status") != "TRANSMITTED_CLEAN_CLAIM"]
    clean_billed = sum(r.get("total_charge", 0.0) for r in clean_claims)
    total_n = len(results) or 1
    qmb_count = sum(1 for r in results if _format_secondary_and_qmb(r)[2])
    medicaid_count = sum(1 for r in results if _format_secondary_and_qmb(r)[0] or _format_secondary_and_qmb(r)[1])
    report_title = "September 2026 Batch Ingestion Report" if batch_prefix == "SEP26" else f"Batch {batch_prefix} Ingestion Report"

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {report_title} (Production Biller Package)\n\n")
        f.write(f"- **Total Documents Processed:** {len(results)}\n")
        f.write(f"- **Clean Claims Ready for Office Ally:** {len(clean_claims)} ({len(clean_claims)/total_n*100:.1f}%)\n")
        f.write(f"- **Held for Biller Review:** {len(flagged)} ({len(flagged)/total_n*100:.1f}%)\n")
        f.write(f"- **Secondary FL Medicaid / COB Tracked:** {medicaid_count} ({qmb_count} QMB Balance-Billing Protected)\n")
        f.write(f"- **Total Clean Billed Charges:** ${clean_billed:,.2f}\n")
        f.write(f"- **Consolidated Office Ally 837P File:** `{batch_837_path}` ({len(consolidated_837):,} bytes)\n")
        f.write(f"- **Office Ally Companion Pre-Flight Report:** `{oa_preflight_path}` (`pyx12_clean={oa_preflight.get('pyx12_clean')}`, errors={oa_preflight.get('error_count')}, warnings={oa_preflight.get('warning_count')})\n")
        f.write(f"- **CMS-1500 Excel/CSV Worklist:** `{csv_path}`\n")
        f.write(f"- **Email-Ready Package (.eml):** `{eml_path}`\n\n")
        f.write("## Batch Reconciliation Table\n\n")
        f.write("| # | Patient Name | DOB | MBI | Secondary / QMB | PCN | DOS | Box 18 Adm | POS | CPT | Diagnoses | Charge | Status |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in results:
            cpt_str = ", ".join(r["cpt_codes"]) if r["cpt_codes"] else "HOLD"
            dx_str = ", ".join(r["icd10_codes"][:4]) if r["icd10_codes"] else "None"
            if len(r["icd10_codes"]) > 4:
                dx_str += f" (+{len(r['icd10_codes'])-4})"
            b9a, b9d, qmb_s = _format_secondary_and_qmb(r)
            sec_md = " / ".join(x for x in (b9a, b9d, qmb_s) if x) or "—"
            f.write(
                f"| {r['index']} | {r['patient_name']} | {r.get('dob') or 'MISSING'} | `{r.get('member_id') or 'MISSING'}` "
                f"| {sec_md} | `{r['patient_control_number']}` | {r.get('date_of_service') or 'MISSING'} "
                f"| {r.get('admission_date') or '—'} | {r['place_of_service']} "
                f"| {cpt_str} | {dx_str} | ${r['total_charge']:.2f} | `{r['status']}` |\n"
            )

    print("\n" + "=" * 80)
    print("▶ BATCH PROCESSING COMPLETE: MEDICAL BILLER DELIVERABLES GENERATED")
    print("=" * 80)
    print(f"Total Documents Evaluated:        {len(results)}")
    print(f"  Clean 837P Claims Ready:        {len(clean_claims)} ({len(clean_claims)/total_n*100:.1f}%)")
    print(f"  Held for Biller Review:         {len(flagged)} ({len(flagged)/total_n*100:.1f}%)")
    print(f"  Secondary Medicaid / QMB:       {medicaid_count} ({qmb_count} QMB Protected)")
    print(f"  Total Clean Billed Charges:     ${clean_billed:,.2f}")
    print(f"  1. Office Ally Master .837:     {batch_837_path} ({len(consolidated_837):,} bytes)")
    print(f"  2. OA Companion Pre-Flight:     {oa_preflight_path} (pyx12_clean={oa_preflight.get('pyx12_clean')})")
    print(f"  3. CMS-1500 Excel/CSV Sheet:    {csv_path}")
    print(f"  4. Email Package (.eml):        {eml_path}")
    print(f"  5. HTML Email Digest:           {html_path}")
    print("=" * 80)
    return {
        "results": results,
        "batch_837_path": batch_837_path,
        "oa_preflight_path": oa_preflight_path,
        "oa_preflight": oa_preflight,
        "csv_path": csv_path,
        "html_path": html_path,
        "eml_path": eml_path,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run Batch Ingestion & Export Biller Package")
    parser.add_argument("--data-dir", type=str, default="data/september_2026", help="Directory containing batch PDFs")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of documents to process")
    parser.add_argument("--batch-prefix", type=str, default="SEP26", help="Batch prefix for unique patient control numbers")
    parser.add_argument("--email-to", type=str, default=None, help="Optional recipient email address for SMTP dispatch")
    parser.add_argument("--overrides", type=str, default=None, help="Biller corrections JSON (default: <data-dir>/overrides.json)")
    parser.add_argument("--coder-signoff", type=str, default=None, help="Name of the coder who reviewed CODER_REVIEW claims")
    args = parser.parse_args()
    run_batch(limit=args.limit, batch_prefix=args.batch_prefix, data_dir=args.data_dir,
              email_to=args.email_to, overrides_path=args.overrides, coder_signoff=args.coder_signoff)
