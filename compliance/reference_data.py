# ============================================
# FILE: medical_billing_ai/compliance/reference_data.py
# PURPOSE: Load authoritative coding-edit reference data with provenance,
#          so scrutiny tiers run on real CMS/FCSO data instead of hardcoded
#          samples.
#
# DATA SOURCES (download quarterly, place CSVs in REFERENCE_DATA_DIR):
#   - NCCI PTP edits (practitioner): https://www.cms.gov/medicare/coding-billing/
#     national-correct-coding-initiative-ncci-edits/medicare-ncci-procedure-
#     procedure-ptp-edits  (PRVDR zip -> *CCIPRC*.csv; columns: Column 1,
#     Column 2, Effective Date, Deletion Date, Modifier Indicator)
#   - MUE values: https://www.cms.gov/medicare/coding-billing/
#     national-correct-coding-initiative-ncci-edits/medicare-ncci-medically-
#     unlikely-edits (columns: HCPCS/CPT Code, Practitioner Services MUE Values,
#     MUE Adjudication Indicator)
#   - FCSO LCDs (FL MAC): https://www.fcso.com/LCD/ + CMS MCD API
#     (https://api.coverage.cms.gov) -> export LCD ID + covered ICD-10 list
#
# Design: if a dataset is not loaded, lookups return status "NOT_LOADED"
# and the caller must treat the claim as UNVERIFIED (not auto-pass).
# ============================================

import csv
import os
from typing import Dict, List, Optional, Any

DATA_DIR = os.getenv("REFERENCE_DATA_DIR",
                     os.path.join(os.path.dirname(__file__), "reference_data"))


class ReferenceDataLibrary:
    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        self._ptp: Dict[tuple, Dict[str, str]] = {}
        self._mue: Dict[str, Dict[str, str]] = {}
        self._lcd: Dict[str, Dict[str, Any]] = {}
        self.provenance: Dict[str, Dict[str, str]] = {}
        self._load_all()

    # ---------- loaders ----------
    def _load_all(self):
        self._load_ptp()
        self._load_mue()
        self._load_lcd()

    def _path(self, *candidates: str) -> Optional[str]:
        for name in candidates:
            p = os.path.join(self.data_dir, name)
            if os.path.exists(p):
                return p
        return None

    def _load_ptp(self):
        path = self._path("ncci_ptp.csv", "cciprc.csv")
        if not path:
            return
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                norm = {k.strip().lower().replace(" ", "_"): (v or "").strip()
                        for k, v in row.items() if k}
                c1 = norm.get("column_1", "")
                c2 = norm.get("column_2", "")
                if c1 and c2:
                    self._ptp[(c1, c2)] = {
                        "modifier_indicator": norm.get("modifier_indicator", ""),
                        "effective_date": norm.get("effective_date", ""),
                        "deletion_date": norm.get("deletion_date", ""),
                    }
        self.provenance["ncci_ptp"] = {
            "file": os.path.basename(path), "rows": str(len(self._ptp)),
            "source": "CMS NCCI PTP (practitioner) quarterly file",
        }

    def _load_mue(self):
        path = self._path("ncci_mue.csv", "mue.csv")
        if not path:
            return
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                norm = {k.strip().lower().replace(" ", "_").replace("/", "_"): (v or "").strip()
                        for k, v in row.items() if k}
                code = norm.get("hcpcs_cpt_code", "") or norm.get("hcpcs", "")
                if code:
                    self._mue[code] = {
                        "mue_value": (norm.get("practitioner_services_mue_values", "")
                                      or norm.get("mue_value", "")),
                        "adjudication_indicator": (norm.get("mue_adjudication_indicator", "")
                                                   or norm.get("mai", "")),
                    }
        self.provenance["ncci_mue"] = {
            "file": os.path.basename(path), "rows": str(len(self._mue)),
            "source": "CMS NCCI MUE (practitioner) quarterly file",
        }

    def _load_lcd(self):
        path = self._path("fcso_lcd.csv", "lcd.csv")
        if not path:
            return
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                norm = {k.strip().lower().replace(" ", "_"): (v or "").strip()
                        for k, v in row.items() if k}
                lcd_id = norm.get("lcd_id", "")
                icd10 = norm.get("icd10_code", "") or norm.get("diagnosis_code", "")
                if lcd_id and icd10:
                    entry = self._lcd.setdefault(lcd_id, {"covered_icd10": set(),
                                                          "title": norm.get("title", "")})
                    entry["covered_icd10"].add(icd10.replace(".", "").upper())
        self.provenance["fcso_lcd"] = {
            "file": os.path.basename(path), "rows": str(len(self._lcd)),
            "source": "FCSO LCD export / CMS MCD API",
        }

    # ---------- lookups ----------
    def ptp_edit(self, code1: str, code2: str) -> Dict[str, Any]:
        """Column1/Column2 bundling edit. modifier_indicator '1' = -59 allowed."""
        if not self._ptp:
            return {"status": "NOT_LOADED", "bundled": None}
        hit = self._ptp.get((code1, code2))
        if hit is None:
            return {"status": "OK", "bundled": False}
        return {"status": "EDIT_HIT", "bundled": True, **hit}

    def mue_limit(self, cpt: str) -> Dict[str, Any]:
        if not self._mue:
            return {"status": "NOT_LOADED", "limit": None}
        hit = self._mue.get(cpt)
        if hit is None:
            return {"status": "NO_MUE", "limit": None}
        try:
            limit = int(hit["mue_value"]) if hit["mue_value"] else None
        except ValueError:
            limit = None
        return {"status": "OK", "limit": limit,
                "adjudication_indicator": hit["adjudication_indicator"]}

    def lcd_covers(self, lcd_id: str, icd10: str) -> Dict[str, Any]:
        if not self._lcd:
            return {"status": "NOT_LOADED", "covered": None}
        entry = self._lcd.get(lcd_id)
        if entry is None:
            return {"status": "LCD_NOT_FOUND", "covered": None}
        covered = icd10.replace(".", "").upper() in entry["covered_icd10"]
        return {"status": "OK", "covered": covered, "lcd_title": entry["title"]}

    def status_summary(self) -> Dict[str, Any]:
        return {
            "ncci_ptp_loaded": bool(self._ptp),
            "ncci_mue_loaded": bool(self._mue),
            "fcso_lcd_loaded": bool(self._lcd),
            "provenance": self.provenance,
            "data_dir": self.data_dir,
        }
