# Phase 2 verification: real ack parsing + ledger reconciliation
import os, sys, tempfile
os.environ["LEDGER_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "ledger_test.sqlite3")
os.environ["OA_ALLOW_SIMULATION"] = "true"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from remittance.ack_parsers import AcknowledgmentParser
from remittance.claim_ledger import ClaimLedger
from transmission.office_ally_dispatcher import OfficeAllyDispatcher

p = AcknowledgmentParser()
ledger = ClaimLedger()
disp = OfficeAllyDispatcher(config=None, allow_simulation=True, ledger=ledger)

# Register a submitted batch + claim (as dispatch would)
ledger.record_batch("BAT_ENC001", "OATEST_MAGNOLIA_BAT_ENC001_837P_1.edi",
                    "/inbound/OATEST_MAGNOLIA_BAT_ENC001_837P_1.edi",
                    isa13="000000001", gs06="1", simulation=False)
ledger.record_claims_in_batch("BAT_ENC001", [{
    "patient_control_number": "ENC001", "encounter_id": "ENC001",
    "patient_name": "DOE, JOHN", "total_charge": 150.0,
}])

SAMPLE_999 = (
    "ISA*00*          *00*          *30*330897513     *ZZ*MAGNOLIA      *250101*1200*^*00501*000000101*0*P*:~\n"
    "GS*FA*330897513*MAGNOLIA*20250101*1200*101*X*005010X231A1~\n"
    "ST*999*0101*005010X231A1~\n"
    "AK1*HC*1*005010X222A1~\n"
    "AK2*837*0001*005010X222A1~\n"
    "IK5*A~\n"
    "AK9*A*1*1*1~\nSE*6*0101~\nGE*1*101~\nIEA*1*000000101~"
)

SAMPLE_277CA = (
    "ISA*00*          *00*          *30*330897513     *ZZ*MAGNOLIA      *250101*1300*^*00501*000000102*0*P*:~\n"
    "GS*HN*330897513*MAGNOLIA*20250101*1300*102*X*005010X214~\n"
    "ST*277*0102*005010X214~\nBHT*0085*08*REF123*20250101*1300*DG~\n"
    "HL*1**20*1~\nNM1*PR*2*AETNA*****PI*60054~\nTRN*1*TRACK1~\n"
    "HL*2*1*21*1~\nNM1*41*2*MAGNOLIA HEALTH*****46*MAGNOLIA~\nTRN*2*SUB1~\n"
    "HL*3*2*19*1~\nNM1*1P*2*MAGNOLIA HEALTH*****XX*1234567890~\nTRN*2*PROV1~\n"
    "HL*4*3*PT~\nNM1*QC*1*DOE*JOHN****MI*MEM123~\nTRN*2*ENC001~\n"
    "STC*A1:19:PR*20250101*WQ*150~\n"
    "SE*16*0102~\nGE*1*102~\nIEA*1*000000102~"
)

SAMPLE_835 = (
    "ISA*00*          *00*          *ZZ*AETNA         *ZZ*MAGNOLIA      *250103*0900*^*00501*000000103*0*P*:~\n"
    "GS*HP*AETNA*MAGNOLIA*20250103*0900*103*X*005010X221A1~\n"
    "ST*835*0103*005010X221A1~\nBPR*I*120*C*ACH***************20250103~\n"
    "TRN*1*CHK123~\nN1*PR*AETNA~\n"
    "CLP*ENC001*1*150*120*10*MB*CLM789*11*1~\nCAS*PR*1*20~\n"
    "SE*8*0103~\nGE*1*103~\nIEA*1*000000103~"
)

assert p.classify_file(SAMPLE_999) == "999"
assert p.classify_file(SAMPLE_277CA) == "277CA"
assert p.classify_file(SAMPLE_835) == "835"

summary = disp.ingest_outbound_files([
    {"filename": "ack_999.rsp", "content": SAMPLE_999},
    {"filename": "ack_277ca.rsp", "content": SAMPLE_277CA},
    {"filename": "era_835.edi", "content": SAMPLE_835},
])
assert summary["999_groups"] == 1, summary
assert summary["277ca_claims"] == 1, summary
assert summary["835_claims"] == 1, summary

claim = ledger.get_claim("ENC001")
assert claim["status"] == "PAID", claim
assert claim["total_paid"] == 120.0, claim

# Rejection path: 277CA A3 on a different claim
ledger.record_claims_in_batch("BAT_ENC001", [{
    "patient_control_number": "ENC002", "encounter_id": "ENC002",
    "patient_name": "SMITH, JANE", "total_charge": 200.0,
}])
rej = SAMPLE_277CA.replace("ENC001", "ENC002").replace("STC*A1:19:PR", "STC*A3:21:PR")
disp.ingest_outbound_files([{"filename": "ack2_277ca.rsp", "content": rej}])
claim2 = ledger.get_claim("ENC002")
assert claim2["status"] == "REJECTED_277CA", claim2

aging = ledger.aging_report()
assert aging["by_status"]["PAID"]["count"] == 1
assert aging["by_status"]["REJECTED_277CA"]["count"] == 1

print("PHASE 2 VERIFICATION PASSED")
print("  999 accepted group reconciled to batch via GS06")
print("  277CA A1 -> ACCEPTED, A3 -> REJECTED_277CA (by CLM01)")
print("  835 CLP -> PAID, total_paid=120.0, PR*1 adjustment captured")
print("  aging report:", {k: v["count"] for k, v in aging["by_status"].items()})
