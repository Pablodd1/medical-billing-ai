# ============================================
# FILE: medical_billing_ai/remittance/claim_ledger.py
# PURPOSE: Persistent claim-submission ledger with acknowledgment and
#          remittance reconciliation.
#
# Every 837 batch upload, every parsed TA1/999/277CA, and every 835 CLP
# line is recorded here so the true lifecycle state of each claim is
# provable: SUBMITTED -> ACKED -> ACCEPTED/REJECTED -> PAID/DENIED.
#
# Default backend is SQLite (stdlib, same pattern as control_numbers).
# Set LEDGER_DB_URL=postgresql://... to use the same schema on Postgres
# (psycopg) in production.
# ============================================

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    filename TEXT,
    remote_path TEXT,
    isa13 TEXT,
    gs06 TEXT,
    submitted_at TEXT,
    simulation INTEGER DEFAULT 0,
    status TEXT DEFAULT 'SUBMITTED'
);
CREATE TABLE IF NOT EXISTS claims (
    patient_control_number TEXT PRIMARY KEY,
    batch_id TEXT,
    encounter_id TEXT,
    patient_name TEXT,
    total_charge REAL,
    status TEXT DEFAULT 'SUBMITTED',
    ack_999_status TEXT,
    ack_277ca_status TEXT,
    era_status TEXT,
    total_paid REAL,
    last_event TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT,
    source_file TEXT,
    event_type TEXT,
    patient_control_number TEXT,
    batch_id TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_pcn ON events(patient_control_number);
CREATE TABLE IF NOT EXISTS review_queue (
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
    status TEXT DEFAULT 'PENDING_REVIEW',
    resolved_at TEXT,
    resolution_notes TEXT,
    resolved_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_rq_status ON review_queue(status);
CREATE TABLE IF NOT EXISTS claim_fingerprints (
    fingerprint TEXT PRIMARY KEY,
    patient_control_number TEXT,
    registered_at TEXT
);
CREATE TABLE IF NOT EXISTS crm_leads (
    lead_id TEXT PRIMARY KEY,
    npi TEXT,
    clinic_name TEXT,
    decision_maker_name TEXT,
    decision_maker_title TEXT,
    specialty TEXT,
    phone TEXT,
    city TEXT,
    state TEXT,
    estimated_annual_billing REAL,
    estimated_total_annual_upside REAL,
    pain_point_hook TEXT,
    status TEXT DEFAULT 'NEW_SCRAPED',
    notes TEXT DEFAULT '',
    lead_json TEXT,
    updated_at TEXT
);
"""

_FINAL_STATUSES = {"PAID", "DENIED", "REJECTED_999", "REJECTED_277CA", "REJECTED_TA1"}


class ClaimLedger:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.getenv(
            "LEDGER_DB_PATH", os.path.join(os.path.dirname(__file__), "claim_ledger.sqlite3")
        )
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self):
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ---------- recording ----------
    def record_batch(self, batch_id: str, filename: str, remote_path: str = "",
                     isa13: str = "", gs06: str = "", simulation: bool = False):
        conn = self._connect()
        try:
            batch_status = "SIMULATED_NOT_TRANSMITTED" if simulation else "SUBMITTED"
            conn.execute(
                "INSERT OR REPLACE INTO batches (batch_id, filename, remote_path, isa13, gs06,"
                " submitted_at, simulation, status) VALUES (?,?,?,?,?,?,?,?)",
                (batch_id, filename, remote_path, isa13, gs06, self._now(), int(simulation), batch_status),
            )
            conn.execute(
                "INSERT INTO events (occurred_at, source_file, event_type, batch_id, detail)"
                " VALUES (?,?,?,?,?)",
                (self._now(), filename, "BATCH_SUBMITTED" if not simulation else "BATCH_SIMULATED", batch_id,
                 json.dumps({"remote_path": remote_path, "simulation": simulation})),
            )
            conn.commit()
        finally:
            conn.close()

    def record_claims_in_batch(self, batch_id: str, claims: List[Dict[str, Any]]):
        conn = self._connect()
        try:
            for c in claims:
                now = self._now()
                claim_status = c.get("status", "SUBMITTED")
                conn.execute(
                    "INSERT INTO claims (patient_control_number, batch_id, encounter_id,"
                    " patient_name, total_charge, status, last_event, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(patient_control_number) DO UPDATE SET"
                    " batch_id=excluded.batch_id, status=excluded.status,"
                    " last_event='BATCH_DISPATCHED', updated_at=excluded.updated_at",
                    (c["patient_control_number"], batch_id, c.get("encounter_id", ""),
                     c.get("patient_name", ""), float(c.get("total_charge", 0) or 0),
                     claim_status, "BATCH_DISPATCHED", now),
                )
            conn.commit()
        finally:
            conn.close()

    def _update_claim(self, conn, pcn: str, status: str, event_type: str,
                      source_file: str, detail: Dict[str, Any], **fields):
        sets = ["status=?", "last_event=?", "updated_at=?"]
        vals: List[Any] = [status, event_type, self._now()]
        for col, v in fields.items():
            sets.append(f"{col}=?")
            vals.append(v)
        vals.append(pcn)
        cur = conn.execute(f"UPDATE claims SET {', '.join(sets)} WHERE patient_control_number=?", vals)
        conn.execute(
            "INSERT INTO events (occurred_at, source_file, event_type,"
            " patient_control_number, detail) VALUES (?,?,?,?,?)",
            (self._now(), source_file, event_type, pcn, json.dumps(detail)),
        )
        return cur.rowcount

    # ---------- reconciliation ----------
    def reconcile_999(self, parsed: List[Dict[str, Any]], source_file: str) -> int:
        """Match functional groups to submitted batches by GS06 (AK1-2).
        A rejected AK5 marks every claim in that batch REJECTED_999."""
        updated = 0
        conn = self._connect()
        try:
            for group in parsed:
                gs06 = group.get("gs06_acknowledged", "")
                status = "ACK_999_ACCEPTED" if group.get("accepted") else "REJECTED_999"
                conn.execute(
                    "INSERT INTO events (occurred_at, source_file, event_type, detail)"
                    " VALUES (?,?,?,?)",
                    (self._now(), source_file, status,
                     json.dumps({k: v for k, v in group.items() if k != "transactions"})),
                )
                batch_row = conn.execute(
                    "SELECT batch_id FROM batches WHERE gs06=?", (gs06,)
                ).fetchone()
                if not batch_row:
                    continue
                batch_id = batch_row["batch_id"]
                conn.execute("UPDATE batches SET status=? WHERE batch_id=?", (status, batch_id))
                if group.get("accepted") is True:
                    cur = conn.execute(
                        "UPDATE claims SET ack_999_status=?, status=?, last_event=?, updated_at=?"
                        " WHERE batch_id=? AND status='SUBMITTED'",
                        (group.get("status_code", "A"), "ACKED_999", "ACK_999_ACCEPTED",
                         self._now(), batch_id),
                    )
                    updated += cur.rowcount
                else:
                    rejected_txs = [t for t in group.get("transactions", [])
                                    if t.get("accepted") is False]
                    if rejected_txs or group.get("accepted") is False:
                        cur = conn.execute(
                            "UPDATE claims SET ack_999_status=?, status='REJECTED_999',"
                            " last_event='ACK_999_REJECTED', updated_at=?"
                            " WHERE batch_id=? AND status NOT IN ('PAID','DENIED')",
                            (group.get("status_code", "R"), self._now(), batch_id),
                        )
                        updated += cur.rowcount
            conn.commit()
        finally:
            conn.close()
        return updated

    def reconcile_277ca(self, parsed: List[Dict[str, Any]], source_file: str) -> int:
        """Match by CLM01 patient control number. Returns number of claims updated."""
        updated = 0
        conn = self._connect()
        try:
            for claim in parsed:
                pcn = claim.get("patient_control_number")
                if not pcn:
                    continue
                latest = claim["status_events"][-1] if claim["status_events"] else {}
                if claim.get("accepted") is True:
                    status = "ACCEPTED_277CA"
                elif claim.get("accepted") is False:
                    status = "REJECTED_277CA"
                else:
                    status = "PENDING_277CA"
                updated += self._update_claim(
                    conn, pcn, status, "ACK_277CA", source_file, claim,
                    ack_277ca_status=latest.get("category_code", ""),
                )
            conn.commit()
        finally:
            conn.close()
        return updated

    def reconcile_835(self, parsed: Dict[str, Any], source_file: str) -> int:
        """Match ERA CLP lines by CLP01 patient control number."""
        updated = 0
        conn = self._connect()
        try:
            for claim in parsed.get("claims", []):
                pcn = claim.get("patient_control_number")
                if not pcn:
                    continue
                era_status = "DENIED" if claim.get("status_code") == "4" else "PAID"
                updated += self._update_claim(
                    conn, pcn, era_status, "ERA_835", source_file, claim,
                    era_status=claim.get("status_description", ""),
                    total_paid=float(claim.get("total_paid", 0) or 0),
                )
            conn.commit()
        finally:
            conn.close()
        return updated

    # ---------- queries ----------
    def get_claim(self, pcn: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM claims WHERE patient_control_number=?", (pcn,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def aging_report(self) -> Dict[str, Any]:
        """Claims stuck in non-final states - the daily follow-up worklist."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM claims").fetchall()
            stuck: Dict[str, List[Dict[str, Any]]] = {}
            for r in rows:
                d = dict(r)
                if d["status"] not in _FINAL_STATUSES:
                    stuck.setdefault(d["status"], []).append(d)
            counts = conn.execute(
                "SELECT status, COUNT(*) c, COALESCE(SUM(total_charge),0) chg,"
                " COALESCE(SUM(total_paid),0) paid FROM claims GROUP BY status"
            ).fetchall()
            return {
                "by_status": {r["status"]: {"count": r["c"], "charged": r["chg"], "paid": r["paid"]}
                              for r in counts},
                "needs_follow_up": stuck,
            }
        finally:
            conn.close()

    # ---------- human-in-the-loop (hitl) review queue ----------
    def record_flagged_claim(
        self,
        pcn: str,
        encounter_id: str,
        patient_name: str,
        total_charge: float,
        confidence_score: float,
        scrutiny_issues: List[Any],
        hcfa_missing: List[Any],
        physician_queries: List[Any],
        encounter_payload: Dict[str, Any],
    ):
        """Records a flagged claim in both the claims ledger (as FLAGGED_PENDING_REVIEW)
        and the review_queue table so the medical biller can inspect and resolve it."""
        conn = self._connect()
        try:
            now = self._now()
            conn.execute(
                "INSERT OR REPLACE INTO claims (patient_control_number, batch_id, encounter_id,"
                " patient_name, total_charge, status, last_event, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (pcn, "PENDING_REVIEW", encounter_id, patient_name, float(total_charge or 0),
                 "FLAGGED_PENDING_REVIEW", "SCRUTINY_FLAGGED", now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO review_queue (patient_control_number, encounter_id,"
                " patient_name, total_charge, confidence_score, scrutiny_issues, hcfa_missing,"
                " physician_queries, encounter_payload, created_at, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (pcn, encounter_id, patient_name, float(total_charge or 0), float(confidence_score or 0),
                 json.dumps(scrutiny_issues), json.dumps(hcfa_missing), json.dumps(physician_queries),
                 json.dumps(encounter_payload), now, "PENDING_REVIEW"),
            )
            conn.execute(
                "INSERT INTO events (occurred_at, source_file, event_type, patient_control_number, detail)"
                " VALUES (?,?,?,?,?)",
                (now, "scrutiny_engine", "CLAIM_FLAGGED_FOR_REVIEW", pcn,
                 json.dumps({"issues_count": len(scrutiny_issues), "confidence": confidence_score})),
            )
            conn.commit()
        finally:
            conn.close()

    def get_review_queue(self, status: str = "PENDING_REVIEW") -> List[Dict[str, Any]]:
        """Lists claims pending medical biller review."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM review_queue WHERE status=? ORDER BY created_at DESC",
                (status,)
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                d["scrutiny_issues"] = json.loads(d.get("scrutiny_issues") or "[]")
                d["hcfa_missing"] = json.loads(d.get("hcfa_missing") or "[]")
                d["physician_queries"] = json.loads(d.get("physician_queries") or "[]")
                # Exclude full encounter_payload from list view for lightweight transfer
                d.pop("encounter_payload", None)
                results.append(d)
            return results
        finally:
            conn.close()

    def get_review_item(self, pcn: str) -> Optional[Dict[str, Any]]:
        """Retrieves full detail of a flagged claim, including full encounter payload."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM review_queue WHERE patient_control_number=?", (pcn,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["scrutiny_issues"] = json.loads(d.get("scrutiny_issues") or "[]")
            d["hcfa_missing"] = json.loads(d.get("hcfa_missing") or "[]")
            d["physician_queries"] = json.loads(d.get("physician_queries") or "[]")
            d["encounter_payload"] = json.loads(d.get("encounter_payload") or "{}")
            return d
        finally:
            conn.close()

    def resolve_review_item(self, pcn: str, resolution_notes: str, resolved_by: str) -> bool:
        """Marks review queue item as RESOLVED and updates events."""
        conn = self._connect()
        try:
            now = self._now()
            cur = conn.execute(
                "UPDATE review_queue SET status='RESOLVED', resolved_at=?, resolution_notes=?,"
                " resolved_by=? WHERE patient_control_number=?",
                (now, resolution_notes, resolved_by, pcn),
            )
            conn.execute(
                "INSERT INTO events (occurred_at, source_file, event_type, patient_control_number, detail)"
                " VALUES (?,?,?,?,?)",
                (now, "biller_portal", "CLAIM_REVIEW_RESOLVED", pcn,
                 json.dumps({"resolved_by": resolved_by, "notes": resolution_notes})),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def register_fingerprint(self, fingerprint: str, pcn: str = "") -> None:
        """Persists a duplicate claim hash to prevent duplicate billing across server restarts."""
        if not fingerprint:
            return
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO claim_fingerprints (fingerprint, patient_control_number, registered_at) VALUES (?, ?, ?)",
                (fingerprint, pcn, self._now())
            )
            conn.commit()
        finally:
            conn.close()

    def has_fingerprint(self, fingerprint: str) -> bool:
        """Returns True if the claim fingerprint has already been registered in the database."""
        if not fingerprint:
            return False
        conn = self._connect()
        try:
            row = conn.execute("SELECT 1 FROM claim_fingerprints WHERE fingerprint = ?", (fingerprint,)).fetchone()
            return row is not None
        finally:
            conn.close()

    def get_all_fingerprints(self) -> set:
        """Returns set of all registered claim fingerprints."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT fingerprint FROM claim_fingerprints").fetchall()
            return {r[0] for r in rows}
        finally:
            conn.close()

    # ---------- CRM & Outbound Lead Management ----------
    def upsert_crm_leads(self, leads: List[Dict[str, Any]]) -> int:
        """Inserts or updates scraped medical clinic leads in the persistent CRM table."""
        if not leads:
            return 0
        conn = self._connect()
        count = 0
        try:
            now = self._now()
            for l in leads:
                lead_id = l.get("lead_id") or f"LEAD-{l.get('npi', '')}"
                conn.execute(
                    "INSERT INTO crm_leads (lead_id, npi, clinic_name, decision_maker_name,"
                    " decision_maker_title, specialty, phone, city, state,"
                    " estimated_annual_billing, estimated_total_annual_upside,"
                    " pain_point_hook, status, notes, lead_json, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(lead_id) DO UPDATE SET"
                    " clinic_name=excluded.clinic_name,"
                    " decision_maker_name=excluded.decision_maker_name,"
                    " phone=excluded.phone,"
                    " estimated_total_annual_upside=excluded.estimated_total_annual_upside,"
                    " lead_json=excluded.lead_json,"
                    " updated_at=excluded.updated_at",
                    (
                        lead_id,
                        str(l.get("npi", "")),
                        l.get("clinic_name", ""),
                        l.get("decision_maker_name", ""),
                        l.get("decision_maker_title", ""),
                        l.get("specialty", ""),
                        l.get("phone", ""),
                        l.get("city", ""),
                        l.get("state", "FL"),
                        float(l.get("estimated_annual_billing", 0) or 0),
                        float(l.get("estimated_total_annual_upside", 0) or 0),
                        l.get("pain_point_hook", ""),
                        l.get("status", "NEW_SCRAPED"),
                        l.get("notes", ""),
                        json.dumps(l),
                        now,
                    ),
                )
                count += 1
            conn.commit()
            return count
        finally:
            conn.close()

    def get_crm_leads(
        self,
        specialty: Optional[str] = None,
        city: Optional[str] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Queries CRM leads with optional filters."""
        conn = self._connect()
        try:
            count_row = conn.execute("SELECT COUNT(*) c FROM crm_leads").fetchone()
            if count_row and count_row["c"] == 0:
                # Auto-seed from leads/scraped_medical_clinics.json if present
                seed_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "leads",
                    "scraped_medical_clinics.json",
                )
                if os.path.exists(seed_path):
                    try:
                        with open(seed_path, "r", encoding="utf-8") as f:
                            seed_leads = json.load(f)
                        self.upsert_crm_leads(seed_leads)
                    except Exception:
                        pass

            sql = "SELECT * FROM crm_leads WHERE 1=1"
            params: List[Any] = []
            if specialty:
                sql += " AND LOWER(specialty) LIKE ?"
                params.append(f"%{specialty.lower()}%")
            if city:
                sql += " AND LOWER(city) LIKE ?"
                params.append(f"%{city.lower()}%")
            if status:
                sql += " AND status = ?"
                params.append(status)
            if search:
                sql += " AND (LOWER(clinic_name) LIKE ? OR LOWER(decision_maker_name) LIKE ? OR npi LIKE ?)"
                q = f"%{search.lower()}%"
                params.extend([q, q, q])
            sql += " ORDER BY estimated_total_annual_upside DESC LIMIT ?"
            params.append(int(limit))

            rows = conn.execute(sql, params).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                try:
                    full = json.loads(d.pop("lead_json", "{}") or "{}")
                except Exception:
                    full = {}
                merged = {**full, **d}
                out.append(merged)
            return out
        finally:
            conn.close()

    def update_crm_lead_status(self, lead_id: str, status: str, notes: str = "") -> Optional[Dict[str, Any]]:
        """Updates a clinic lead's sales pipeline status and notes."""
        conn = self._connect()
        try:
            now = self._now()
            conn.execute(
                "UPDATE crm_leads SET status=?, notes=?, updated_at=? WHERE lead_id=? OR npi=?",
                (status, notes, now, lead_id, lead_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM crm_leads WHERE lead_id=? OR npi=?", (lead_id, lead_id)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                full = json.loads(d.pop("lead_json", "{}") or "{}")
            except Exception:
                full = {}
            return {**full, **d}
        finally:
            conn.close()

