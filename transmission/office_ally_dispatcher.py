# ============================================
# FILE: medical_billing_ai/transmission/office_ally_dispatcher.py
# PURPOSE: Office Ally SFTP Dispatcher & Acknowledgment Tracker
#
# IMPORTANT CHANGE: this dispatcher no longer fabricates 999/277CA
# acknowledgments. After a real upload the claim enters
# SUBMITTED_AWAITING_ACK; the real 999/277CA/835 files are polled from
# /outbound and parsed by remittance/acknowledgment parsers (Phase 2).
#
# Simulation is available ONLY as an explicit, clearly-labelled fallback
# (OA_ALLOW_SIMULATION=true or no credentials), so test suites keep
# working without SFTP access -- but simulated results can never be
# mistaken for real transmission.
# ============================================

import os
import time
from typing import Dict, Any, Optional
from datetime import datetime

try:
    from config import OfficeAllyConfig
    from transmission.sftp_client import OfficeAllySFTPClient
except ImportError:
    from ..config import OfficeAllyConfig
    from ..transmission.sftp_client import OfficeAllySFTPClient


class OfficeAllyDispatcher:
    def __init__(
        self,
        submitter_id: str = None,
        config: Optional[OfficeAllyConfig] = None,
        sftp_client: Optional[OfficeAllySFTPClient] = None,
        allow_simulation: Optional[bool] = None,
    ):
        self.config = config or OfficeAllyConfig()
        if submitter_id:
            self.config.claims_submitter_id = submitter_id
        self._sftp_client = sftp_client
        self.transmitted_batches = []
        # Simulation is opt-in via env var; otherwise requires real creds
        if allow_simulation is None:
            allow_simulation = os.getenv("OA_ALLOW_SIMULATION", "").lower() in ("1", "true", "yes")
        self.allow_simulation = allow_simulation

    @property
    def client(self) -> OfficeAllySFTPClient:
        if self._sftp_client is None:
            self._sftp_client = OfficeAllySFTPClient(self.config)
        return self._sftp_client

    def dispatch_batch(self, x12_837_content: str, batch_id: str = None,
                       test: bool = False) -> Dict[str, Any]:
        now = datetime.now()
        batch_id = batch_id or f"BATCH_{now.strftime('%Y%m%d_%H%M%S')}"

        if self.config.has_sftp_credentials:
            upload = self.client.upload_claim_file(x12_837_content, batch_id, test=test)
            record = {
                "batch_id": batch_id,
                "filename": upload["filename"],
                "remote_path": upload["remote_path"],
                "timestamp": now.isoformat(),
                "byte_size": upload["byte_size"],
                "status": "SUBMITTED_AWAITING_ACK",
                "simulation": False,
            }
            self.transmitted_batches.append(record)
            return {
                "success": True,
                "simulation": False,
                "transmission": record,
                # Real acknowledgments arrive asynchronously in /outbound.
                "ack_999_functional": None,
                "ack_277ca_claim_level": None,
                "next_step": (
                    "Poll /outbound for the 999 (functional) and 277CA "
                    "(claim-level) acknowledgments; reconcile by CLM01."
                ),
            }

        if not self.allow_simulation:
            raise RuntimeError(
                "Office Ally SFTP credentials are not configured "
                "(set OA_SFTP_USERNAME / OA_SFTP_PASSWORD). Refusing to "
                "simulate transmission silently. For test environments set "
                "OA_ALLOW_SIMULATION=true."
            )

        # --- Explicit, clearly-labelled simulation path (tests only) ---
        record = {
            "batch_id": batch_id,
            "filename": OfficeAllySFTPClient.build_claim_filename(
                self.config.claims_submitter_id, batch_id, test=True
            ),
            "timestamp": now.isoformat(),
            "byte_size": len(x12_837_content.encode("utf-8")),
            "status": "SIMULATED_NOT_TRANSMITTED",
            "simulation": True,
        }
        self.transmitted_batches.append(record)
        return {
            "success": True,
            "simulation": True,
            "warning": "SIMULATION MODE: no file was sent to Office Ally.",
            "transmission": record,
            "ack_999_functional": {"status": "ACCEPTED", "simulated": True},
            "ack_277ca_claim_level": {"status": "ACCEPTED_FORWARDED_TO_PAYER", "simulated": True},
        }

    def poll_acknowledgments(self):
        """Downloads new 999/277CA/835 files from /outbound."""
        if not self.config.has_sftp_credentials:
            raise RuntimeError("SFTP credentials required to poll acknowledgments.")
        return self.client.poll_outbound()
