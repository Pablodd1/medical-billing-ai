# Developer Architecture & Onboarding Guide

Welcome to the **Autonomous Medical Billing AI** repository. This guide provides other software engineers, clinical informatics specialists, and platform architects with an in-depth understanding of the internal mechanics, design patterns, regulatory rules, and development workflows.

---

## 🧭 Core Architecture & Data Flow

Every encounter processed by the system travels through the 6 stages defined in [`AutonomousMedicalBillingPipeline`](pipeline.py):

```text
[Raw Document / Input Note]
           │
           ▼
[Stage 1: Multimodal Ingestion] (ingestion/multimodal_ingestor.py)
   Extracts text using PyMuPDF (fitz) or OCR cleaners.
   Disambiguates Date of Service (DOS), Place of Service (POS), and Provider credentials.
           │
           ▼
[Stage 2: Real-Time Eligibility] (eligibility/eligibility_engine.py)
   Executes 270 Inquiry. Verifies active coverage, copay, deductible, and prior-auth.
   Holds asynchronous clearinghouse transactions (HELD_AWAITING_271).
           │
           ▼
[Stage 3: Clinical NLP & Coding] (coding/clinical_coder.py)
   Translates narrative into ICD-10 diagnoses & CPT procedures.
   Calculates AMA 2021/2023 E/M Medical Decision Making (MDM) level.
   Applies ICD-10 Section I.B.5 symptom suppression.
   Maps medical necessity diagnosis pointers for procedures (Box 24E / SV107).
           │
           ▼
[Stage 4: 8-Tier Clinical Scrutiny] (scrutiny/scrutinizer.py)
   Evaluates 8 regulatory tiers (Florida APRN scope, FCSO LCDs, NCCI, duplicate guard).
   Calculates confidence score (0.0 to 1.0).
           │
    ┌──────┴──────────────────────────┐
    │                                 │
[Passed: Clean Claim]         [Failed / Flawed]
    │                                 │
    │                        [Stage 5: HITL Review Queue]
    │                           Saved to SQLite review_queue.
    │                           Requires biller approval via /portal.
    │                                 │
    └────────────────┬────────────────┘
                     │
                     ▼
[Stage 6: ANSI X12 837P Generation & Dispatch] (transmission/x12_837_generator.py)
   Constructs 5010 837P EDI with ABK/ABF qualifiers and correct Loop 2320 ordering.
   Dispatches to Office Ally SFTP (or executes clearly-labelled simulation).
   Records lifecycle state in persistent ledger (claim_ledger.sqlite3).
```

---

## 🔬 Clinical NLP Engine (`coding/clinical_coder.py`)

The clinical coding engine must be robust against clinical shorthand while never making unverified assumptions.

### 1. Clause-Bounded Negation (`_extract_clause_prefix`)
In clinical documentation, clinicians frequently document negative review of systems alongside positive assessments:
> *"Patient reports right knee pain without trauma. Orthopedic Assessment: Internal derangement right knee (M23.201)."*

If a negation search looks back across sentences, `"without trauma"` will negate `"M23.201"`. To prevent this:
```python
@staticmethod
def _extract_clause_prefix(text: str, start_pos: int, lookback: int = 70) -> str:
    limit = max(0, start_pos - lookback)
    sub = text[limit:start_pos]
    delims = list(re.finditer(r"[\.\;\n\:\!\?]", sub))
    clause_start = limit + delims[-1].end() if delims else limit
    return text[clause_start:start_pos]
```
The search window is strictly terminated by `.`, `;`, `\n`, `:`, `!`, and `?`.

### 2. Multi-Occurrence Evaluation (`re.finditer`)
When a clinician writes:
> *"Emergency Dept: Ruled out S82.001. Ortho Follow-up: S82.001 confirmed on repeat film."*

The engine must not stop after finding the first negated instance. Using `re.finditer()`, it groups all occurrences of each code. If at least **one** occurrence is non-negated and clinically confirmed, the code is coded.

### 3. Non-Diagnosis & Vertebral Spine Level Filtering
Certain clinical tokens match the letter-and-two-digits format of ICD-10 categories:
* Vertebrae: `C1`–`C7`, `T1`–`T12`, `L1`–`L5`, `S1`–`S5` (e.g., *"Compression fracture at T12"*)
* Rooms: `room C12`, `bed 14`
* Labs & Dosing: `B12`, `D3`, `TID`, `BID`, `PRN`

The engine uses `SPINE_LEVEL_TOKENS`, `ROOM_LOCATION_REGEX`, and `VALID_3CHAR_COMPLETE_ICD10` to guarantee that only valid complete diagnosis codes (such as `I10` for Essential Hypertension) are extracted.

### 4. ICD-10-CM Guideline Section I.B.5: Symptom Suppression
```python
SYMPTOM_SUPPRESSION_MAP = {
    "M25.561": ["M23.", "M17."],  # Knee pain suppressed by meniscus derangement or OA
    "M25.562": ["M23.", "M17."],
    "M25.569": ["M23.", "M17."],
    "R53.83": ["E55.", "E11.", "I10"]  # Fatigue suppressed by underlying endocrine/vascular disease
}
```
If a confirmed diagnosis matching a suppressor prefix exists in `seen_codes`, the corresponding symptom code is suppressed.

### 5. Medical Necessity Diagnosis Pointers (Box 24E / SV107)
Never hardcode `dx_pointers=[1]`. Procedure service lines must link to the diagnosis that clinically justifies them:
```python
if re.match(r"^(?:20[0-9]{3}|73[0-9]{3})$", proc_code):  # Musculoskeletal / Orthopedic
    for d in extracted_diagnoses:
        if d.code.startswith("M") or d.code.startswith("S"):
            target_pointer = d.pointer
            break
elif re.match(r"^17[0-9]{3}$", proc_code):  # Integumentary / Dermatology
    for d in extracted_diagnoses:
        if d.code.startswith("L") or d.code.startswith("D"):
            target_pointer = d.pointer
            break
```

---

## 🛡️ The 8 Scrutiny Tiers (`scrutiny/scrutinizer.py`)

Before any claim can be submitted, `MedicalBillingScrutinizer` checks eight independent risk vectors:

1. **Tier 1: Clinical Documentation & Diagnosis Presence**
   * Verifies that chief complaint, HPI, and assessment exist.
   * Verifies that at least one non-negated ICD-10 code was extracted. If no diagnosis exists, raises `MISSING_DIAGNOSES` (Severity: CRITICAL).
2. **Tier 2: Florida Scope of Practice & Credentialing**
   * Enforces Florida Statutes §458.348 / §464.012 (Supervisory protocol with licensed MD/DO) and §464.0123 (Autonomous APRN registration).
   * Verifies dual NPI separation (Individual Type 1 NPI in Box 24J vs. Clinic Type 2 NPI in Box 33a).
3. **Tier 3: Medical Necessity & FCSO Florida LCD/NCD**
   * Validates procedure CPT codes against Florida Medicare Administrative Contractor (FCSO Jurisdiction N) Local Coverage Determinations (e.g. LCD L36805 for joint injections, L36063 for vitamin D testing).
4. **Tier 4: NCCI Edits (PTP Bundling & MUE Limits)**
   * Checks for mutually exclusive unbundled pairs.
   * Verifies that Modifier -25 is strictly supported by documentation.
5. **Tier 5: WEDI SNIP Levels 1–5 Integrity**
   * Validates standard ANSI X12 syntax, field lengths, required subscriber demographics, and payer IDs.
6. **Tier 6: Place of Service (POS) Validation**
   * Reconciles clinical setting against POS code (`11` for Office, `02`/`10` for Telehealth, `31` for SNF).
7. **Tier 7: Payer Timely Filing Limits**
   * Calculates days elapsed since Date of Service. Compares against payer deadline (Medicare = 365 days; Medicaid = 180 days; Commercial = 90–180 days).
8. **Tier 8: Persistent Duplicate Claim Fingerprinting**
   * Calculates SHA-256 hash: `hash(member_id|dos|cpts|rendering_npi)`.
   * Checks both in-memory cache and persistent SQLite `claim_fingerprints` table.
   * Prevents CARC 18 duplicate submission denials across server restarts.

---

## 📑 HIPAA 5010 837P Generator (`transmission/x12_837_generator.py`)

### ASC X12 Loop 2320 Segment Ordering Rule
Under ASC X12N 005010X222A1 Table 2, Loop 2320 (Other Subscriber / Secondary Coordination of Benefits), the segment order is strictly:
```text
SBR (Subscriber Info, Pos 005)
 └── CAS (Claim Adjustments, Pos 010)
      └── AMT*D (COB Payer Paid Amount, Pos 015)
           └── OI (Other Insurance Info, Pos 035)
                └── NM1*IL (Other Subscriber Name, Loop 2330A, Pos 065)
```
Emitting `OI` before `AMT*D` causes clearinghouses to reject claims with 999 syntax errors. The generator strictly outputs `AMT*D` before `OI`.

### ICD-10 Health Care Diagnosis Codes (HI Segment)
* Primary Diagnosis: Emits qualifier `ABK` (e.g. `HI*ABK:M23201~`)
* Secondary Diagnoses: Emits qualifier `ABF` (e.g. `HI*ABK:M23201*ABF:I10~`)
* *Never use `BK` or `BF` (which are legacy ICD-9 qualifiers).*

---

## 🗄️ Claims Ledger Database Schema (`remittance/claim_ledger.py`)

SQLite database is stored at `remittance/claim_ledger.sqlite3`. Uses WAL mode (`PRAGMA journal_mode=WAL`) for concurrent access.

```sql
-- Batch-level transmission records
CREATE TABLE batches (
    batch_id TEXT PRIMARY KEY,
    filename TEXT,
    remote_path TEXT,
    isa13 TEXT,
    gs06 TEXT,
    submitted_at TEXT,
    simulation INTEGER DEFAULT 0,
    status TEXT DEFAULT 'SUBMITTED'  -- 'SUBMITTED' or 'SIMULATED_NOT_TRANSMITTED'
);

-- Claim-level lifecycle tracking
CREATE TABLE claims (
    patient_control_number TEXT PRIMARY KEY,  -- CLM01
    batch_id TEXT,
    encounter_id TEXT,
    patient_name TEXT,
    total_charge REAL,
    status TEXT DEFAULT 'SUBMITTED',          -- 'SUBMITTED', 'SIMULATED_NOT_TRANSMITTED', 'PAID', 'DENIED'
    ack_999_status TEXT,
    ack_277ca_status TEXT,
    era_status TEXT,
    total_paid REAL,
    last_event TEXT,
    updated_at TEXT
);

-- Immutable audit event trail
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT,
    source_file TEXT,
    event_type TEXT,
    patient_control_number TEXT,
    batch_id TEXT,
    detail TEXT
);

-- Human-in-the-Loop review queue
CREATE TABLE review_queue (
    patient_control_number TEXT PRIMARY KEY,
    encounter_id TEXT,
    patient_name TEXT,
    total_charge REAL,
    confidence_score REAL,
    scrutiny_issues TEXT,
    hcfa_missing TEXT,
    physician_queries TEXT,
    encounter_payload TEXT,
    created_at TEXT,
    status TEXT DEFAULT 'PENDING_REVIEW',      -- 'PENDING_REVIEW' or 'RESOLVED'
    resolved_at TEXT,
    resolution_notes TEXT,
    resolved_by TEXT
);

-- Persistent SHA-256 duplicate claim fingerprints
CREATE TABLE claim_fingerprints (
    fingerprint TEXT PRIMARY KEY,
    patient_control_number TEXT,
    registered_at TEXT
);
```

---

## 💻 Developer Runbook & Best Practices

### 1. Adding a New Diagnosis to the Ontology
To add a condition to [`ClinicalCodingEngine.DIAGNOSIS_ONTOLOGY`](coding/clinical_coder.py):
1. Specify the lowercase keyword (e.g. `"lumbar radiculopathy"`).
2. Specify the exact ICD-10-CM code and full official descriptor (e.g. `("M54.16", "Radiculopathy, lumbar region")`).
3. If this diagnosis explains back pain or paresthesias, add the symptom to `SYMPTOM_SUPPRESSION_MAP`.
4. Add a unit test in `tests/test_billing_pipeline.py`.

### 2. Adding a New Procedure to the Ontology
To add a CPT code to [`ClinicalCodingEngine.PROCEDURE_ONTOLOGY`](coding/clinical_coder.py):
1. Specify the lowercase keyword (e.g. `"trigger point injection"`).
2. Specify the CPT code, standard clinic charge, and description (`("20552", 110.00, "Injection(s); single or multiple muscle group(s)")`).
3. If it is a minor surgical procedure (10000–69999), verify that `has_surgical_procedure` in `generate_coding()` attaches Modifier -25 when appropriate.
4. Update `dx_pointers` mapping in `generate_coding()` so the service line points to the correct musculoskeletal diagnosis.

### 3. Running All Verifications Locally
```bash
# 1. Run complete unit test suite
$env:PYTHONIOENCODING="utf-8"; python -m unittest discover tests

# 2. Run Human-in-the-Loop workflow test
python scratch/test_hitl_workflow.py

# 3. Run Jev-1 route monitor
$env:API_KEYS="smoke-test-key-2026"; node scripts/jev-audit.mjs
```

---

## ⚠️ Anti-Patterns to Avoid

| ❌ NEVER DO THIS | ✅ ALWAYS DO THIS | RATIONALE |
|---|---|---|
| Fallback to `Z00.00` when no diagnosis is found. | Return empty diagnoses and flag in Tier 1. | `Z00.00` is an annual preventive exam; billing it on a problem visit is false claims fraud. |
| Negate codes using string search across full notes. | Bound negation strictly to sentence/clause delimiters. | Prevents negated history in HPI from cancelling confirmed diagnoses in Assessment. |
| Auto-attach Modifier -25 to diagnostic X-rays or labs. | Restrict Modifier -25 to minor surgical procedures (CPT 10000–69999). | Billing -25 with diagnostic services triggers automatic payer scrub audits. |
| Hardcode `dx_pointers=[1]` on all procedure lines. | Align procedure pointers with supporting diagnoses by specialty. | Prevents CARC 11 medical necessity denials. |
| Mark simulated claims as `SUBMITTED` in the ledger. | Mark simulated claims as `SIMULATED_NOT_TRANSMITTED`. | Prevents un-transmitted test claims from polluting practice accounts receivable aging. |
| Store duplicate fingerprints in an in-memory set only. | Persist fingerprints in SQLite `claim_fingerprints`. | Prevents duplicate claims when server restarts. |
