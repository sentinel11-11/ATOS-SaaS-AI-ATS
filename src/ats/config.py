from functools import lru_cache
from uuid import UUID

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATS_", env_file=".env", env_ignore_empty=True, extra="ignore"
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

    @model_validator(mode="after")
    def validate_live(self):
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
