# ============================================
# FILE: scripts/parallel_ocr.py
# PURPOSE: High-Performance Parallel OCR Processor for Scanned Medical Charts
# ============================================

import os
import sys
import glob
import time
import numpy as np
from concurrent.futures import ProcessPoolExecutor, as_completed

def _worker_ocr(pdf_path: str, txt_path: str):
    import fitz
    from rapidocr_onnxruntime import RapidOCR
    
    t0 = time.time()
    ocr = RapidOCR()
    doc = fitz.open(pdf_path)
    ocr_pages = []
    for pno in range(len(doc)):
        pix = doc[pno].get_pixmap(dpi=100)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            img = img[:, :, :3]
        res, _ = ocr(img)
        if res:
            lines = [txt for _, txt, score in res if score > 0.45]
            if lines:
                ocr_pages.append(f"=== PAGE {pno+1} ===\n" + "\n".join(lines))
    full_text = "\n\n".join(ocr_pages).strip()
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(full_text)
    return os.path.basename(pdf_path), len(full_text), time.time() - t0

def run_parallel_ocr(data_dir: str = "data/september_2026", max_workers: int = 6, limit: int = None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    all_pdfs = sorted(glob.glob(os.path.join(data_dir, "*.pdf")))
    pending = []
    for p in all_pdfs:
        txt_path = f"{p}.ocr.txt"
        if not os.path.exists(txt_path) or os.path.getsize(txt_path) < 20:
            pending.append((p, txt_path))

    total_pending = len(pending)
    if limit:
        pending = pending[:limit]

    print(f"[*] Found {len(all_pdfs)} total PDFs in {data_dir}.", flush=True)
    print(f"[*] Already cached: {len(all_pdfs) - total_pending}.", flush=True)
    print(f"[*] Pending OCR: {len(pending)} files (Workers: {max_workers}).", flush=True)

    if not pending:
        print("[*] All files already have cached OCR text. Nothing to do!", flush=True)
        return

    start_all = time.time()
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_worker_ocr, p, t): p for p, t in pending}
        done_count = 0
        for fut in as_completed(futures):
            done_count += 1
            try:
                base_name, char_count, duration = fut.result()
                print(f"[{done_count}/{len(pending)}] [OK] {base_name}: {char_count} chars extracted in {duration:.1f}s", flush=True)
            except Exception as e:
                failed_path = futures[fut]
                print(f"[{done_count}/{len(pending)}] [FAIL] {os.path.basename(failed_path)}: {e}", flush=True)

    total_time = time.time() - start_all
    print(f"\n[✓] Parallel OCR completed: {len(pending)} files processed in {total_time:.1f}s ({total_time/60:.1f} min)", flush=True)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    run_parallel_ocr(max_workers=args.workers, limit=args.limit)
