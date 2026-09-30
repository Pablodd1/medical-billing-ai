# ============================================
# FILE: medical_billing_ai/transmission/sftp_client.py
# PURPOSE: Real SFTP transport for Office Ally (Paramiko / SSH, port 22)
# CONTRACT (per OA SFTP setup guide):
#   - Upload claims ONLY to /inbound
#   - Poll /outbound for 999 / 277CA / 835 reports
#   - Filenames must contain '837P' (prod) or 'OATEST' (test)
# ============================================

import time
from typing import List, Dict, Any, Optional

try:
    import paramiko
except ImportError:  # paramiko optional at import time; required at connect time
    paramiko = None

try:
    from config import OfficeAllyConfig
except ImportError:
    from ..config import OfficeAllyConfig


class OfficeAllySFTPClient:
    def __init__(self, config: OfficeAllyConfig):
        if paramiko is None:
            raise ImportError("paramiko is required for SFTP transmission: pip install paramiko")
        self.config = config

    def _connect(self):
        transport = paramiko.Transport((self.config.sftp_host, self.config.sftp_port))
        transport.connect(
            username=self.config.sftp_username,
            password=self.config.sftp_password.get_secret_value()
            if self.config.sftp_password else None,
        )
        sftp = paramiko.SFTPClient.from_transport(transport)
        return transport, sftp

    @staticmethod
    def build_claim_filename(submitter_id: str, batch_id: str, test: bool = False) -> str:
        """OA hard gate: filename must contain '837P'; test files must contain 'OATEST'."""
        tag = "OATEST_" if test else ""
        safe_batch = "".join(c if c.isalnum() or c in "_-" else "_" for c in batch_id)
        return f"{tag}{submitter_id}_{safe_batch}_837P_{int(time.time())}.edi"

    def upload_claim_file(self, x12_content: str, batch_id: str, test: bool = False) -> Dict[str, Any]:
        filename = self.build_claim_filename(self.config.claims_submitter_id, batch_id, test)
        remote_path = f"{self.config.inbound_dir.rstrip('/')}/{filename}"
        transport, sftp = self._connect()
        try:
            with sftp.file(remote_path, "w") as f:
                f.write(x12_content)
        finally:
            sftp.close()
            transport.close()
        return {
            "filename": filename,
            "remote_path": remote_path,
            "byte_size": len(x12_content.encode("utf-8")),
            "uploaded_at_epoch": time.time(),
            "test_file": test,
        }

    def poll_outbound(self, name_contains: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lists /outbound and downloads report files. Classification is by
        transaction set (ST segment) content, done by the caller."""
        transport, sftp = self._connect()
        results: List[Dict[str, Any]] = []
        try:
            for attr in sftp.listdir_attr(self.config.outbound_dir):
                if name_contains and name_contains not in attr.filename:
                    continue
                remote_path = f"{self.config.outbound_dir.rstrip('/')}/{attr.filename}"
                with sftp.file(remote_path, "r") as f:
                    content = f.read().decode("utf-8", errors="replace")
                results.append({
                    "filename": attr.filename,
                    "remote_path": remote_path,
                    "byte_size": attr.st_size,
                    "modified_epoch": attr.st_mtime,
                    "content": content,
                })
        finally:
            sftp.close()
            transport.close()
        return results
