from functools import lru_cache
from uuid import UUID

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATS_", env_file=".env", env_ignore_empty=True, extra="ignore", populate_by_name=True
    )
    database_url: str
    secure_cookies: bool = True
    public_origin: str = "https://ats.example.invalid"
    session_hours: int = Field(default=8, ge=1, le=24)
    live_calls_enabled: bool = False
    telephony_tenant_id: UUID | None = None
    test_destinations: str = ""
    ami_host: str = "127.0.0.1"
    ami_port: int = 5039
    ami_tls: bool = True
    ami_user: str = "ats-beta"
    ami_secret: SecretStr = SecretStr("")
    ami_caller_id: str = Field(default="", pattern=r"^(|[0-9]{10})$")
    ami_trunk: str = Field(default="mcm", pattern=r"^[a-zA-Z0-9_.-]{1,80}$")
    ami_tech: str = Field(default="PJSIP", pattern=r"^(PJSIP|SIP)$")
    ami_context: str = Field(default="ats-beta", pattern=r"^[a-zA-Z0-9_.-]{1,80}$")
    ami_number_format: str = Field(default="ru8", pattern=r"^(ru8|e164|d10)$")
    ami_caller_id_format: str = Field(default="d10", pattern=r"^(d10|ru8|e164)$")
    ami_dial_prefix: str = Field(default="", pattern=r"^[0-9]{0,10}$")
    ami_play_message: bool = True
    ami_record_calls: bool = True
    megafon_url: str = ""
    megafon_api_key: SecretStr = SecretStr("")
    megafon_crm_token: SecretStr = SecretStr("")
    megafon_user: str = ""
    amo_url: str = ""
    amo_access_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("ATS_AMOCRM_TOKEN", "ATS_AMO_ACCESS_TOKEN"),
    )
    amo_tenant_id: UUID | None = None
    crm_enabled: bool = False
    worker_poll_seconds: float = Field(default=1, ge=0.05, le=60)
    recordings_dir: str = "var/recordings"
    inbound_enabled: bool = False
    inbound_contexts: str = "from-mcm,ats-in"
    media_enabled: bool = False
    media_dir: str = "/var/lib/ats-beta-media"
    asterisk_recordings_dir: str = "/var/spool/asterisk/monitor/ats-beta"
    connection_encryption_key: SecretStr = SecretStr("")
    events_enabled: bool = True

    @model_validator(mode="after")
    def validate_live(self):
        if self.inbound_enabled and not self.telephony_tenant_id:
            raise ValueError("Inbound calls require a tenant binding")
        if self.live_calls_enabled and (not self.telephony_tenant_id or not self.destinations):
            raise ValueError("Live calls require tenant binding and explicit test destinations")
        if not self.ami_tls and self.ami_host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote AMI requires TLS; use a local secured tunnel otherwise")
        if self.crm_enabled and (
            not self.amo_tenant_id or not self.amo_access_token.get_secret_value()
        ):
            raise ValueError("CRM requires an explicit tenant and access token binding")
        return self

    @property
    def destinations(self) -> set[str]:
        return {p.strip() for p in self.test_destinations.split(",") if p.strip()}


@lru_cache
def settings() -> Settings:
    return Settings()
