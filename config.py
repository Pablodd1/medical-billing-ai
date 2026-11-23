# ============================================
# FILE: medical_billing_ai/config.py
# PURPOSE: Office Ally Trading-Partner Configuration (Single Source of Truth)
# NOTES:
#   - Claims and Eligibility use DIFFERENT Submitter IDs (OA issues both separately).
#   - Eligibility requires a password placed in ISA04 (max 10 chars).
#   - Receiver is ISA07='30', ISA08/GS03='330897513' per OA companion guide.
#   - ISA15: 'P' production / 'T' test. OA ALSO requires 'OATEST' in the
#     filename for test files -- the filename, not ISA15, routes test traffic.
# ============================================

import os
from typing import Any, Dict, Optional
from pydantic import SecretStr
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _HAS_PYDANTIC_SETTINGS = True
except ImportError:  # graceful fallback if pydantic-settings not installed yet
    from pydantic import BaseModel as BaseSettings  # type: ignore
    SettingsConfigDict = dict  # type: ignore
    _HAS_PYDANTIC_SETTINGS = False


class OfficeAllyConfig(BaseSettings):
    """All values overridable via environment variables prefixed with OA_."""

    def __init__(self, **data: Any):
        if not _HAS_PYDANTIC_SETTINGS:
            env_defaults: Dict[str, Any] = {}
            for field_name in (
                "sftp_host",
                "sftp_port",
                "sftp_username",
                "sftp_password",
                "inbound_dir",
                "outbound_dir",
                "service_center_url",
                "soap_realtime_url",
                "soap_legacy_url",
                "soap_username",
                "soap_password",
                "core_receiver_id",
                "core_rule_version",
                "claims_submitter_id",
                "eligibility_submitter_id",
                "eligibility_password",
                "receiver_qualifier",
                "receiver_id",
                "receiver_name",
                "usage_indicator",
            ):
                if field_name not in data:
                    env_val = os.getenv(f"OA_{field_name.upper()}")
                    if env_val is not None and env_val != "":
                        env_defaults[field_name] = int(env_val) if field_name == "sftp_port" else env_val
            data = {**env_defaults, **data}
        super().__init__(**data)

    # --- SFTP (per OA SFTP setup guide) ---
    sftp_host: str = "ftp10.officeally.com"
    sftp_port: int = 22
    sftp_username: Optional[str] = None
    sftp_password: Optional[SecretStr] = None
    inbound_dir: str = "/inbound"     # claims MUST be dropped here
    outbound_dir: str = "/outbound"   # 999/277CA/835 reports arrive here

    # --- Web Portal & CAQH CORE Real-Time SOAP (per OA 270/271 CORE 2.2.0 guide) ---
    service_center_url: str = "https://sc.officeally.com"
    soap_realtime_url: str = "https://wsd.officeally.com/TransactionService/rtx.svc"
    soap_legacy_url: str = "https://wsd.officeally.com/TransactionService/RealtimeTransaction.asmx"
    soap_username: Optional[str] = None
    soap_password: Optional[SecretStr] = None
    core_receiver_id: str = "OFFALLY"
    core_rule_version: str = "2.2.0"

    # --- Trading partner IDs ---
    claims_submitter_id: str = "MAGNOLIA_HEALTH"      # ISA06/GS02 for 837 (OA-assigned Company ID)
    eligibility_submitter_id: str = ""                # SEPARATE OA-issued ID for 270/271
    eligibility_password: Optional[SecretStr] = None  # ISA04, max 10 chars
    receiver_qualifier: str = "30"                    # ISA07
    receiver_id: str = "330897513"                    # ISA08/GS03
    receiver_name: str = "OFFICE ALLY"

    # --- Production / Test ---
    usage_indicator: str = "P"        # ISA15: 'P' or 'T'

    try:
        model_config = SettingsConfigDict(env_prefix="OA_")
    except Exception:
        pass

    @property
    def has_sftp_credentials(self) -> bool:
        return bool(self.sftp_username and self.sftp_password)

    @property
    def has_soap_credentials(self) -> bool:
        return bool(
            (self.soap_username and self.soap_password)
            or (self.sftp_username and self.sftp_password)
        )

    @property
    def is_placeholder_submitter_id(self) -> bool:
        return self.claims_submitter_id.strip().upper() in {
            "",
            "MAGNOLIA_HEALTH",
            "SUBMITTER_ID",
            "TEST",
            "UNKNOWN",
            "NOT CONFIGURED",
            "000000000",
        }

    @property
    def effective_eligibility_submitter_id(self) -> str:
        return self.eligibility_submitter_id or self.claims_submitter_id
