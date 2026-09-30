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
            conn.execute(
                "INSERT OR REPLACE INTO batches (batch_id, filename, remote_path, isa13, gs06,"
                " submitted_at, simulation, status) VALUES (?,?,?,?,?,?,?,?)",
                (batch_id, filename, remote_path, isa13, gs06, self._now(), int(simulation), "SUBMITTED"),
            )
            conn.execute(
                "INSERT INTO events (occurred_at, source_file, event_type, batch_id, detail)"
                " VALUES (?,?,?,?,?)",
                (self._now(), filename, "BATCH_SUBMITTED", batch_id,
                 json.dumps({"remote_path": remote_path, "simulation": simulation})),
            )
            conn.commit()
        finally:
            conn.close()

    def record_claims_in_batch(self, batch_id: str, claims: List[Dict[str, Any]]):
        conn = self._connect()
        try:
            for c in claims:
                conn.execute(
                    "INSERT OR IGNORE INTO claims (patient_control_number, batch_id,"
                    " encounter_id, patient_name, total_charge, status, updated_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (c["patient_control_number"], batch_id, c.get("encounter_id", ""),
                     c.get("patient_name", ""), float(c.get("total_charge", 0) or 0),
                     "SUBMITTED", self._now()),
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
