# ============================================
# FILE: compliance/code_search.py
# PURPOSE: Hybrid Meilisearch + Local `simple_icd_10_cm` & Florida RCM Code Lookup
# NOTES: Non-PHI public clinical codebooks only (ICD-10-CM, CPT, Modifiers, NCCI).
# ============================================

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from functools import lru_cache
from typing import Any, Dict, List, Optional

try:
    import simple_icd_10_cm as icd
except ImportError:  # pragma: no cover
    icd = None


FLORIDA_RCM_REFERENCE: List[Dict[str, str]] = [
    {
        "id": "MOD-25",
        "code": "25",
        "code_type": "MODIFIER",
        "title": "Significant, Separately Identifiable E/M Service by Same Physician Same Day",
        "notes": "Required on E/M (99202-99215) when billed same day as minor procedure (e.g., 17000, 20610) with distinct documentation.",
    },
    {
        "id": "MOD-59",
        "code": "59",
        "code_type": "MODIFIER",
        "title": "Distinct Procedural Service",
        "notes": "Indicates distinct session, anatomical site, or separate injury. Prefer X{EPSU} modifiers (XE, XS, XP, XU) for Medicare/FCSO when applicable.",
    },
    {
        "id": "MOD-XE",
        "code": "XE",
        "code_type": "MODIFIER",
        "title": "Separate Encounter",
        "notes": "CMS X-modifier for services distinct because they occurred during separate encounters on the same date.",
    },
    {
        "id": "MOD-XS",
        "code": "XS",
        "code_type": "MODIFIER",
        "title": "Separate Structure",
        "notes": "CMS X-modifier for services performed on separate organs or anatomical structures.",
    },
    {
        "id": "MOD-95",
        "code": "95",
        "code_type": "MODIFIER",
        "title": "Synchronous Telemedicine Service via Real-Time Interactive Audio/Video",
        "notes": "Pair with POS 02 or POS 10 (patient at home) per Medicare/Florida Medicaid telehealth billing guidelines.",
    },
    {
        "id": "CPT-99213",
        "code": "99213",
        "code_type": "CPT",
        "title": "Office or other outpatient visit, established patient, low MDM (20-29 min)",
        "notes": "Requires medically appropriate history/exam and low medical decision making or 20-29 minutes total time.",
    },
    {
        "id": "CPT-99214",
        "code": "99214",
        "code_type": "CPT",
        "title": "Office or other outpatient visit, established patient, moderate MDM (30-39 min)",
        "notes": "Common chronic illness management or prescription drug management visit; ensure distinct HPI/MDM if paired with Modifier 25.",
    },
    {
        "id": "CPT-20610",
        "code": "20610",
        "code_type": "CPT",
        "title": "Arthrocentesis, aspiration and/or injection, major joint or bursa (without ultrasound)",
        "notes": "Check FCSO LCD L36805 and laterality/site documentation; pair with RT/LT or 50 modifier when applicable.",
    },
    {
        "id": "CPT-17000",
        "code": "17000",
        "code_type": "CPT",
        "title": "Destruction, premalignant lesions (e.g., actinic keratoses), first lesion",
        "notes": "Check FCSO LCD L33947 medical necessity criteria; pair 17003 for lesions 2-14.",
    },
    {
        "id": "HCPCS-G2211",
        "code": "G2211",
        "code_type": "HCPCS",
        "title": "Visit complexity inherent to evaluation and management associated with longitudinal care",
        "notes": "Medicare CY2026 add-on with office/outpatient E/M; do NOT bill when E/M carries Modifier 25 unless payer exception applies.",
    },
]


@lru_cache(maxsize=1)
def _build_local_icd10_index() -> List[Dict[str, str]]:
    """Pre-caches billable/header ICD-10-CM items from `simple_icd_10_cm` for instant offline lookup."""
    if icd is None:
        return []
    items: List[Dict[str, str]] = []
    for code in icd.get_all_codes():
        try:
            desc = icd.get_description(code)
            is_leaf = bool(icd.is_leaf(code))
        except Exception:
            continue
        items.append(
            {
                "id": f"ICD10-{code.replace('.', '_')}",
                "code": code,
                "code_type": "ICD-10-CM",
                "title": desc,
                "billable": "true" if is_leaf else "false",
                "notes": "Billable leaf code" if is_leaf else "Category header (select child leaf code)",
            }
        )
    return items


def _meilisearch_query(
    query: str,
    limit: int,
    meili_url: Optional[str] = None,
    meili_key: Optional[str] = None,
    index_uid: str = "icd10_rcm_codes",
    timeout: float = 1.5,
) -> Optional[List[Dict[str, Any]]]:
    base = (meili_url or os.getenv("MEILISEARCH_URL", "")).strip().rstrip("/")
    if not base:
        return None
    key = (meili_key if meili_key is not None else os.getenv("MEILISEARCH_MASTER_KEY", "")).strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = json.dumps({"q": query, "limit": max(1, min(limit, 50))}).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/indexes/{index_uid}/search",
        data=payload,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        hits = data.get("hits")
        return hits if isinstance(hits, list) else None
    except Exception:
        return None


def search_clinical_codes(
    query: str,
    limit: int = 15,
    prefer_meilisearch: bool = True,
) -> Dict[str, Any]:
    """
    Hybrid clinical codebook search (non-PHI).
    Uses Meilisearch (`MEILISEARCH_URL`) for <10ms typo-tolerant search when running,
    and falls back deterministically to `simple_icd_10_cm` + Florida RCM reference rules.
    """
    q = (query or "").strip()
    lim = max(1, min(int(limit or 15), 50))
    if not q:
        return {"ok": True, "engine": "empty", "query": "", "count": 0, "results": []}

    if prefer_meilisearch:
        meili_hits = _meilisearch_query(q, lim)
        if meili_hits is not None:
            return {
                "ok": True,
                "engine": "meilisearch",
                "query": q,
                "count": len(meili_hits),
                "results": meili_hits,
            }

    q_low = q.lower()
    tokens = [t for t in q_low.replace(",", " ").split() if t]
    scored: List[tuple[int, Dict[str, str]]] = []

    def _score_doc(doc: Dict[str, str]) -> int:
        code_l = doc["code"].lower()
        title_l = doc["title"].lower()
        notes_l = doc.get("notes", "").lower()
        hay = f"{code_l} {title_l} {notes_l}"
        if code_l == q_low:
            return 100
        if code_l.startswith(q_low):
            return 80
        if q_low in title_l:
            return 70
        if all(t in hay for t in tokens):
            score = 50
            if doc.get("billable") == "true":
                score += 5
            return score
        if len(tokens) > 1 and any(t in code_l or t in title_l for t in tokens):
            return 15
        return 0

    for doc in FLORIDA_RCM_REFERENCE:
        sc = _score_doc(doc)
        if sc > 0:
            scored.append((sc + 10, doc))

    for doc in _build_local_icd10_index():
        sc = _score_doc(doc)
        if sc >= 50:
            scored.append((sc, doc))

    scored.sort(key=lambda pair: (-pair[0], len(pair[1]["code"]), pair[1]["code"]))
    top = [item for _, item in scored[:lim]]
    return {
        "ok": True,
        "engine": "local_icd10_rcm",
        "query": q,
        "count": len(top),
        "results": top,
    }
