# ============================================
# FILE: scripts/batch_manifest.py
# PURPOSE: Extracts all file IDs and names for the September 2026 Batch
# ============================================

import re
import os
import sys
import json

def get_manifest(step_file: str = ""):
    step_file = (
        step_file
        or (sys.argv[1] if len(sys.argv) > 1 else "")
        or os.environ.get("DRIVE_MANIFEST_HTML", "")
        or r"C:\Users\Owner\.gemini\antigravity\brain\0226c4b0-2b08-4e4e-ba17-2f55c9f6b3c8\.system_generated\steps\1479\output.txt"
    )
    if not os.path.exists(step_file):
        return {}
    with open(step_file, "r", encoding="utf-8") as f:
        html = f.read()

    # Pattern matches Google Drive AF_initDataCallback:
    # [null,\"<ID>\"], ... \"<NAME>.pdf\"
    pattern = re.compile(r'\[null,\\+"([a-zA-Z0-9_-]{28,35})\\+"\](?:(?!\[null,\\+").)*?\\+"([A-Za-z0-9\s\.\-_]+\.pdf)\\+"', re.DOTALL)
    matches = pattern.findall(html)
    
    seen = {}
    for fid, name in matches:
        clean_name = name.strip()
        if clean_name not in seen:
            seen[clean_name] = fid

    return seen

if __name__ == "__main__":
    manifest = get_manifest()
    print(f"Total files in manifest: {len(manifest)}")
    with open("september_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    for name, fid in sorted(manifest.items()):
        print(f"  {name}: {fid}")
