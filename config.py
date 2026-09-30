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

from typing import Optional
from pydantic import SecretStr
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:  # graceful fallback if pydantic-settings not installed yet
    from pydantic import BaseModel as BaseSettings  # type: ignore
    SettingsConfigDict = dict  # type: ignore


class OfficeAllyConfig(BaseSettings):
    """All values overridable via environment variables prefixed with OA_."""

    # --- SFTP (per OA SFTP setup guide) ---
    sftp_host: str = "ftp10.officeally.com"
    sftp_port: int = 22
    sftp_username: Optional[str] = None
    sftp_password: Optional[SecretStr] = None
    inbound_dir: str = "/inbound"     # claims MUST be dropped here
    outbound_dir: str = "/outbound"   # 999/277CA/835 reports arrive here

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
    def effective_eligibility_submitter_id(self) -> str:
        return self.eligibility_submitter_id or self.claims_submitter_id
