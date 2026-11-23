# ============================================
# FILE: scripts/build_results_site.py
# PURPOSE: Build a self-contained, offline "Batch Results" site (HTML) from the
#          biller CSV + consolidated 837 + OCR text, with per-claim audit flags.
# DEPENDENCIES: stdlib only. Output contains PHI -> exports/ is git-ignored.
# USAGE: python scripts/build_results_site.py [--prefix SEP26]
#        python scripts/build_results_site.py --serve   (http://127.0.0.1:8080)
# ============================================

import argparse
import csv
import difflib
import functools
import http.server
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPORTS = ROOT / "exports"
DATA_DIR = ROOT / "data" / "september_2026"
FACILITY_ADDR = "15955 Bass Creek Road"
PLACEHOLDER_TAX_ID = "591234567"
PLACEHOLDER_PHONE = "8005551212"
KNOWN_PAYERS = (
    "humana", "simply", "aetna", "cigna", "united", "molina", "sunshine",
    "wellcare", "florida blue", "devoted", "careplus", "avmed", "leon",
    "medica", "preferred care", "doctors healthcare", "alignment", "clover",
    "oscar", "ambetter", "medicaid", "qmb", "mcra", "tricare", "aarp",
)
NOISE_PAYER = re.compile(r"polic|pollc|pending|disab|eligib|dr\.", re.I)


def ocr_sex(text: str) -> str | None:
    """First standalone M/F line within 14 lines after the 'Sex' label."""
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if ln.lower() == "sex":
            for nxt in lines[i + 1:i + 15]:
                if re.fullmatch(r"[MF]", nxt):
                    return nxt
    return None


def find_837(prefix: str) -> Path | None:
    """Final, OATEST, or DRAFT consolidated 837 for this batch prefix."""
    for name in (
        f"OFFICE_ALLY_BATCH_{prefix}.837",
        f"OATEST_OFFICE_ALLY_BATCH_{prefix}.837",
        f"DRAFT_NOT_FOR_SUBMISSION_{prefix}.837",
    ):
        if (EXPORTS / name).exists():
            return EXPORTS / name
    return None


def split_837(path: Path | None) -> dict[str, str]:
    """Map patient control number -> that claim's ST..SE segment text."""
    if not path or not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8", errors="replace").replace("\r", "")
    out = {}
    for block in re.split(r"(?=^ST\*837)", raw, flags=re.M)[1:]:
        m = re.search(r"BHT\*[^*]*\*[^*]*\*([^*~]+)", block)
        if m:
            out[m.group(1)] = block.split("SE*")[0].replace("~\n", "~\n").strip() + "~"
    return out


def audit_row(row: dict, data_dir: Path = DATA_DIR) -> list[dict]:
    """Return coder/biller review flags for one CLEAN claim row."""
    flags = []
    src = row["Source_PDF_File"]
    ocr_path = data_dir / f"{src}.ocr.txt"
    text = ocr_path.read_text(encoding="utf-8", errors="replace") if ocr_path.exists() else ""

    sex = ocr_sex(text)
    if sex and sex != row["Box3_Sex"]:
        flags.append({"sev": "high", "code": "SEX_MISMATCH",
                      "msg": f"Chart says {sex}, claim says {row['Box3_Sex']} (defaulted). Payer will reject."})
    elif not sex:
        flags.append({"sev": "med", "code": "SEX_UNVERIFIED", "msg": "Sex not found in OCR; value may be a default."})

    if row["Box5_Patient_Address"].strip() == FACILITY_ADDR and "Previous address" in text:
        flags.append({"sev": "high", "code": "ADDRESS_FALLBACK",
                      "msg": "Patient address is the facility address; chart has a separate home address."})

    last = row["Box2_Patient_Name_Last_First"].split(",")[0].strip().lower().replace(" ", "")
    fn = re.sub(r"[^a-z ]", "", src.lower()).split()
    fn_last = fn[0] if fn else ""
    if fn_last and difflib.SequenceMatcher(None, last, fn_last).ratio() < 1.0 and last[:4] != fn_last[:4]:
        flags.append({"sev": "med", "code": "NAME_VS_FILE", "msg": f"Name '{last}' differs from filename '{fn_last}'."})
    elif fn_last and last != fn_last:
        flags.append({"sev": "low", "code": "NAME_TYPO?", "msg": f"Name '{last}' vs filename '{fn_last}': verify spelling against card."})

    sec = row["Box9d_Secondary_Payer_Name"].strip()
    if sec and (NOISE_PAYER.search(sec) or not any(k in sec.lower() for k in KNOWN_PAYERS)):
        flags.append({"sev": "med", "code": "SECONDARY_PAYER_NOISE", "msg": f"Secondary payer '{sec}' is not a recognised payer."})
    return flags


def build_rows(prefix: str, data_dir: Path = DATA_DIR) -> tuple[list[dict], dict]:
    slug = "september_2026" if prefix == "SEP26" else prefix.lower()
    csv_path = EXPORTS / f"{slug}_cms1500_biller_sheet.csv"
    path_837 = find_837(prefix)
    claims_837 = split_837(path_837)
    rows = []
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            queued = r["Review_Tier"] != "HOLD"
            r["flags"] = audit_row(r, data_dir=data_dir) if queued else []
            if r["Review_Tier"] == "CODER_REVIEW":
                r["flags"].append({"sev": "med", "code": "CODER_REVIEW", "msg": r["Action_Required_Or_Notes"]})
            r["edi"] = claims_837.get(r["Box26_Patient_Control_No"], "")
            rows.append(r)
    is_draft = bool(path_837 and path_837.name.startswith("DRAFT"))
    edi_all = "".join(claims_837.values())
    globals_ = []
    if is_draft or not path_837:
        globals_.append({"sev": "high", "msg": f"{path_837.name if path_837 else '837'} is a DRAFT: billing entity (Tax ID, Type-2 NPI, phone, address + 9-digit ZIP+4) is not configured or coder sign-off is pending. Do not upload."})
    if f"REF*EI*{PLACEHOLDER_TAX_ID}" in edi_all or PLACEHOLDER_PHONE in edi_all:
        globals_.append({"sev": "high", "msg": "837 contains placeholder Tax ID or phone."})
    preflight_path = EXPORTS / f"{slug}_oa_preflight.json"
    if preflight_path.exists():
        pf = json.loads(preflight_path.read_text(encoding="utf-8", errors="replace"))
        globals_.append({
            "sev": "low" if pf.get("pyx12_clean") else "high",
            "msg": (
                f"Office Ally 5010A1 Pre-Flight: pyx12_clean={pf.get('pyx12_clean')}, "
                f"queued_claims={pf.get('claim_count')}, companion_errors={pf.get('error_count')}, "
                f"warnings={pf.get('warning_count')}."
            ),
        })
    if not globals_:
        globals_.append({"sev": "low", "msg": "Billing entity configured and coder sign-off recorded. Present."})
    return rows, {"global": globals_}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>
:root{--bg:#f1f5f9;--card:#fff;--ink:#0f172a;--mut:#64748b;--ok:#047857;--warn:#b45309;--bad:#b91c1c;--pri:#4338ca}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--pri);color:#fff;padding:16px 24px}header h1{margin:0;font-size:18px}header p{margin:2px 0 0;opacity:.8;font-size:12px}
main{max-width:1280px;margin:0 auto;padding:20px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--card);border-radius:10px;padding:14px;box-shadow:0 1px 2px #0001}.card b{display:block;font-size:22px}.card span{color:var(--mut);font-size:12px}
.banner{background:#fef2f2;border:1px solid #fecaca;color:#7f1d1d;border-radius:10px;padding:12px 16px;margin-bottom:16px}
.banner ul{margin:6px 0 0 18px;padding:0}.tools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
input,select{padding:7px 10px;border:1px solid #cbd5e1;border-radius:8px;font:inherit}input{min-width:260px}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden;box-shadow:0 1px 2px #0001}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #e2e8f0;white-space:nowrap}th{background:#e2e8f0;cursor:pointer;font-size:12px}
tr.r:hover{background:#eef2ff;cursor:pointer}.pill{padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}
.ok{background:#d1fae5;color:var(--ok)}.hold{background:#fee2e2;color:var(--bad)}.high{background:#fee2e2;color:var(--bad)}.med{background:#fef3c7;color:var(--warn)}.low{background:#e0e7ff;color:var(--pri)}
.wrap{overflow:auto}dialog{border:0;border-radius:12px;max-width:900px;width:92%;padding:0;box-shadow:0 20px 50px #0006}
dialog .h{display:flex;justify-content:space-between;padding:14px 18px;background:#e2e8f0}dialog .b{padding:16px 18px;max-height:75vh;overflow:auto}
dl{display:grid;grid-template-columns:200px 1fr;gap:4px 12px;margin:0}dt{color:var(--mut)}dd{margin:0;font-family:ui-monospace,Consolas,monospace}
pre{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;overflow:auto;font-size:12px}button{cursor:pointer;border:0;border-radius:8px;padding:7px 12px;background:var(--pri);color:#fff}
.note{color:var(--mut);font-size:12px;margin-top:12px}
</style></head><body>
<header><h1>Medical Billing AI &mdash; Batch Results (__BATCH__)</h1>
<p>Assistant biller/coder view &bull; read-only &bull; contains PHI &mdash; local use only</p></header>
<main>
<div class="cards" id="cards"></div>
<div class="banner" id="banner"></div>
<div class="tools"><input id="q" placeholder="Search name, MBI, ICD-10, CPT, facility..." aria-label="Search">
<select id="f"><option value="all">All claims</option><option value="clean">Queued for 837</option><option value="coder">Coder review</option><option value="hold">Held for review</option><option value="flagged">Has audit flags</option><option value="high">High-severity flags</option></select>
<button id="csv">Download CSV</button></div>
<div class="wrap"><table id="t"><thead><tr></tr></thead><tbody></tbody></table></div>
<p class="note">Audit flags are automated heuristics comparing the claim to the OCR text; they do not replace coder review.</p>
</main>
<dialog id="d"><div class="h"><b id="dt"></b><button id="dc">Close</button></div><div class="b" id="db"></div></dialog>
<script>
const DATA=__DATA__;const G=__GLOBAL__;
const COLS=[["Row_#","#"],["Box2_Patient_Name_Last_First","Patient"],["Box3_Patient_DOB","DOB"],["Box1a_Medicare_MBI","MBI"],["Box9a_Secondary_Medicaid_ID","Box 9a Medicaid"],["Box9_QMB_Protection","QMB"],["Box24A_Date_Of_Service","DOS"],["Box18_SNF_Admission_Date","Box 18 Adm"],["Box24D_CPT_Code","CPT"],["Box21_ICD10_A_Primary","Dx 1"],["Box32_Service_Facility_Name","Facility"],["Box28_Total_Charge_USD","Charge"],["Review_Tier","Tier"],["flags","Flags"]];
const $=s=>document.querySelector(s);let sortK=null,asc=true;
const isClean=r=>r.Review_Tier!=="HOLD";
const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=x;return e};
function cards(){const c=DATA.filter(isClean),tot=c.reduce((s,r)=>s+parseFloat(r.Box28_Total_Charge_USD||0),0);
const hi=c.filter(r=>r.flags.some(f=>f.sev==="high")).length,fl=c.filter(r=>r.flags.length).length;
[["Documents",DATA.length],["Queued for 837",c.length],["Coder review",DATA.filter(r=>r.Review_Tier==="CODER_REVIEW").length],["Held",DATA.length-c.length],["Queued charges","$"+tot.toLocaleString(undefined,{minimumFractionDigits:2})],["Cross-check flags",fl],["High-severity",hi],["Fully clear",c.length-fl]].forEach(([k,v])=>{const d=el("div","card");d.append(el("b","",v),el("span","",k));$("#cards").append(d)})}
function banner(){const b=$("#banner");b.append(el("b","","Batch status:"));const u=el("ul");G.global.forEach(g=>u.append(el("li","",g.msg)));b.append(u)}
function rows(){const q=$("#q").value.toLowerCase(),f=$("#f").value;let r=DATA.filter(x=>{
if(q&&!JSON.stringify(x).toLowerCase().includes(q))return false;
if(f==="clean")return isClean(x);if(f==="coder")return x.Review_Tier==="CODER_REVIEW";if(f==="hold")return !isClean(x);if(f==="flagged")return x.flags.length;if(f==="high")return x.flags.some(z=>z.sev==="high");return true});
if(sortK)r=[...r].sort((a,b)=>{const A=a[sortK]??"",B=b[sortK]??"";const n=!isNaN(A)&&!isNaN(B);return(n?A-B:String(A).localeCompare(B))*(asc?1:-1)});return r}
function draw(){const tb=$("#t tbody");tb.replaceChildren();rows().forEach(r=>{const tr=el("tr","r");COLS.forEach(([k])=>{const td=el("td");
if(k==="Review_Tier"){td.append(el("span","pill "+(r.Review_Tier==="READY"?"ok":r.Review_Tier==="HOLD"?"hold":"med"),r.Review_Tier))}
else if(k==="flags"){const h=r.flags.filter(x=>x.sev==="high").length;td.append(el("span","pill "+(h?"high":r.flags.length?"med":"ok"),r.flags.length?r.flags.length+(h?" ("+h+" high)":""):"none"))}
else td.textContent=r[k]||"";tr.append(td)});tr.onclick=()=>open(r);tb.append(tr)})}
function open(r){$("#dt").textContent=r.Box2_Patient_Name_Last_First+" \\u2014 "+r.Box26_Patient_Control_No;const b=$("#db");b.replaceChildren();
if(r.flags.length){const u=el("ul");r.flags.forEach(f=>{const li=el("li");li.append(el("span","pill "+f.sev,f.sev.toUpperCase()),document.createTextNode(" "+f.code+": "+f.msg));u.append(li)});b.append(el("h4","","Audit flags"),u)}
else if(isClean(r))b.append(el("p","","No automated flags."));
else b.append(el("p","",r.Action_Required_Or_Notes));
b.append(el("h4","","CMS-1500 fields"));const dl=el("dl");Object.entries(r).filter(([k])=>!["flags","edi"].includes(k)).forEach(([k,v])=>{dl.append(el("dt","",k),el("dd","",v||"\\u2014"))});b.append(dl);
if(r.edi){b.append(el("h4","","837P segment"));b.append(el("pre","",r.edi.replace(/~/g,"~\\n")))}$("#d").showModal()}
const tr=$("#t thead tr");COLS.forEach(([k,l])=>{const th=el("th","",l);th.onclick=()=>{asc=sortK===k?!asc:true;sortK=k;draw()};tr.append(th)});
$("#q").oninput=draw;$("#f").onchange=draw;$("#dc").onclick=()=>$("#d").close();
$("#csv").onclick=()=>{const h=Object.keys(DATA[0]).filter(k=>!["flags","edi"].includes(k));const esc=v=>'"'+String(v??"").replace(/"/g,'""')+'"';
const t=[h.join(",")].concat(DATA.map(r=>h.map(k=>esc(r[k])).join(","))).join("\\n");const a=el("a");a.href=URL.createObjectURL(new Blob([t],{type:"text/csv"}));a.download="batch_results.csv";a.click()};
cards();banner();draw();
</script></body></html>"""


def build(prefix: str, data_dir: Path = DATA_DIR) -> Path:
    rows, meta = build_rows(prefix, data_dir=data_dir)
    html = (PAGE.replace("__TITLE__", f"Batch Results {prefix}").replace("__BATCH__", prefix)
            .replace("__DATA__", json.dumps(rows).replace("</", "<\\/"))
            .replace("__GLOBAL__", json.dumps(meta)))
    out = EXPORTS / "site" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefix", default="SEP26")
    ap.add_argument("--data-dir", default="data/september_2026")
    ap.add_argument("--serve", action="store_true", help="serve on 127.0.0.1:8080")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    out = build(a.prefix, data_dir=ROOT / a.data_dir)
    print(f"Built {out}")
    if a.serve:
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out.parent))
        with http.server.ThreadingHTTPServer(("127.0.0.1", a.port), handler) as srv:
            print(f"Serving http://127.0.0.1:{a.port}  (Ctrl+C to stop)")
            srv.serve_forever()


if __name__ == "__main__":
    main()


