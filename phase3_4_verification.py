# Phase 3/4 verification: real 270 build, 271 parse, auth, PHI crypto, audit log, reference data
import os, sys, tempfile, json

tmp = tempfile.mkdtemp()
os.environ["LEDGER_DB_PATH"] = os.path.join(tmp, "l.sqlite3")
os.environ["CONTROL_DB_PATH"] = os.path.join(tmp, "c.sqlite3")
os.environ["AUDIT_LOG_PATH"] = os.path.join(tmp, "audit.jsonl")
os.environ["OA_ALLOW_SIMULATION"] = "true"
os.environ["API_KEYS"] = "test-key-123"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import OfficeAllyConfig
from models.encounter import PatientInfo, PayerInfo
from eligibility.eligibility_engine import RealTimeEligibilityEngine, parse_271

cfg = OfficeAllyConfig(eligibility_submitter_id="MAGELIG", usage_indicator="T")
from pydantic import SecretStr
cfg.eligibility_password = SecretStr("PW123")
eng = RealTimeEligibilityEngine(config=cfg)

patient = PatientInfo(first_name="John", last_name="Doe", dob="1980-05-15",
                      gender="M", member_id="MEM123456")
payer = PayerInfo(name="AETNA", payer_id="60054")

# --- 270 build ---
x270 = eng.build_270(patient, payer, provider_npi="1234567890", provider_name="MAGNOLIA HEALTH")
assert "ST*270*" in x270 and "005010X279A1" in x270
assert "*30*330897513" in x270, "receiver must be 30*330897513"
assert "ZZ*MAGELIG" in x270, "eligibility submitter ID"
assert "*01*PW123" in x270, "ISA04 eligibility password"
assert "*0*T*:" in x270, "ISA15=T in test mode"
segs = [s for s in x270.replace("\n", "~").split("~") if s.strip()]
se = [s for s in segs if s.startswith("SE*")][0]
body_count = sum(1 for s in segs[segs.index(next(s for s in segs if s.startswith('ST*'))):] if not s.startswith(("GE*", "IEA*")))
assert int(se.split("*")[1]) == body_count, f"SE count {se} vs {body_count}"
print("270 build OK: envelope 30*330897513, ISA04 password, ISA15=T, SE exact")

# --- 271 parse ---
SAMPLE_271 = (
    "ST*271*0001*005010X279A1~BHT*0022*11*REQ000001*20250101*1200~"
    "HL*1**20*1~NM1*PR*2*AETNA*****PI*60054~"
    "HL*2*1*21*1~NM1*1P*2*MAGNOLIA HEALTH*****XX*1234567890~"
    "HL*3*2*22*0~NM1*IL*1*DOE*JOHN****MI*MEM123456~"
    "DTP*346*D8*20250101~"
    "EB*1*IND*30^^^26**HM*AETNA OPEN ACCESS~"
    "EB*B*IND*98***26*25*****Y~"
    "EB*C*IND*30***27*500*****Y~"
    "EB*G*IND*30***29*2000*****Y~"
    "SE*12*0001~"
)
r = parse_271(SAMPLE_271)
assert r["is_active"] is True
assert r["copay"] == 25.0, r["copay"]
assert r["remaining_deductible"] == 500.0
assert r["out_of_pocket_remaining"] == 2000.0
assert r["coverage_dates"]["start"] == "20250101"
print("271 parse OK: active, copay 25, deductible 500, OOP 2000, dates captured")

# --- simulation labeling ---
res = eng.check_eligibility(patient, payer, provider_npi="1234567890", provider_name="MAGNOLIA HEALTH")
assert res["simulation"] is True and "NOT verified" in res["warning"]
print("Simulation fallback OK: clearly labelled, never presented as verified")

# --- PHI crypto ---
from cryptography.fernet import Fernet
os.environ["PHI_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
from security.phi_crypto import encrypt_phi, decrypt_phi, hash_identifier, encryption_available
assert encryption_available()
tok = encrypt_phi("John Doe DOB 1980-05-15")
assert tok != "John Doe DOB 1980-05-15"
assert decrypt_phi(tok) == "John Doe DOB 1980-05-15"
h = hash_identifier("MEM123456")
assert h != "MEM123456" and len(h) == 16
print("PHI crypto OK: Fernet round-trip, hashed identifiers")

# --- auth ---
import asyncio
from security.auth import require_api_key
from fastapi import HTTPException
ok = asyncio.run(require_api_key("test-key-123"))
assert len(ok) == 12 and ok != "test-key-123"
try:
    asyncio.run(require_api_key("wrong-key"))
    raise AssertionError("should have rejected")
except HTTPException as e:
    assert e.status_code == 403
os.environ.pop("API_KEYS")
try:
    asyncio.run(require_api_key("test-key-123"))
    raise AssertionError("should fail-closed")
except HTTPException as e:
    assert e.status_code == 503
os.environ["API_KEYS"] = "test-key-123"
print("Auth OK: valid key accepted (fingerprinted), wrong key 403, no keys = fail-closed 503")

# --- audit log ---
from security.audit_log import audit_event
audit_event(ok, "ELIGIBILITY_CHECK", "/api/v1/eligibility/check", "OK", patient_ref="MEM123456")
line = open(os.environ["AUDIT_LOG_PATH"]).read().strip()
rec = json.loads(line)
assert "MEM123456" not in line, "raw PHI must never appear in audit log"
assert rec["patient_ref_hash"] and "test-key-123" not in line
print("Audit log OK: hashed identifiers only, no keys, no PHI")

# --- reference data (empty dir => NOT_LOADED) ---
os.environ["REFERENCE_DATA_DIR"] = os.path.join(tmp, "refdata")
os.makedirs(os.environ["REFERENCE_DATA_DIR"])
from compliance.reference_data import ReferenceDataLibrary
lib = ReferenceDataLibrary()
assert lib.ptp_edit("99213", "36415")["status"] == "NOT_LOADED"
with open(os.path.join(os.environ["REFERENCE_DATA_DIR"], "ncci_ptp.csv"), "w") as f:
    f.write("Column 1,Column 2,Effective Date,Deletion Date,Modifier Indicator\n"
            "80053,36415,20250101,*,1\n")
lib2 = ReferenceDataLibrary()
hit = lib2.ptp_edit("80053", "36415")
assert hit["status"] == "EDIT_HIT" and hit["modifier_indicator"] == "1"
assert lib2.ptp_edit("99213", "99214")["bundled"] is False
print("Reference data OK: NOT_LOADED when absent, real PTP edit hit when loaded")

print("\nPHASE 3/4 VERIFICATION PASSED (6/6)")
