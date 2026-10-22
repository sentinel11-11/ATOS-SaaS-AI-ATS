import re
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


def phone_e164(value: str) -> str:
    value = re.sub(r"[ ()-]", "", value.strip())
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
        raise PydanticCustomError("phone_e164", "Use an international number, e.g. +79991234567")
    return value


def phone_from_import(value: str) -> str:
    """Permit known phone punctuation, never salvage digits from arbitrary text/formulas."""
    value = re.sub(r"[\s()-]", "", value.strip().removeprefix("'"))
    digits = value.removeprefix("+")
    if not re.fullmatch(r"[0-9]{8,15}", digits):
        raise ValueError("Invalid imported phone")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return phone_e164("+" + digits)


class Login(Input):
    tenant_id: UUID
    login: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)


class ContactCreate(Input):
    name: str = Field(min_length=1, max_length=120)
    phone: str
    consent: bool = False
    consent_source: str = Field(default="", max_length=256)
    crm_contact_id: int | None = Field(default=None, gt=0)
    _phone = field_validator("phone")(phone_e164)

    @model_validator(mode="after")
    def source_required(self):
        if self.consent and not self.consent_source.strip():
            raise PydanticCustomError("consent_source_required", "Consent requires a source")
        return self


class ContactPatch(Input):
    consent: bool
    consent_source: str = Field(min_length=1, max_length=256)
    blocked: bool

    @model_validator(mode="after")
    def valid_source(self):
        if not self.consent_source.strip():
            raise ValueError("Consent changes require a source or reason")
        return self


class CampaignCreate(Input):
    name: str = Field(min_length=1, max_length=120)
    route: Literal["sim", "multicom_ami", "megafon_vats"] = "sim"
    contact_ids: list[UUID] = Field(min_length=1, max_length=1000)
    max_channels: int = Field(default=1, ge=1, le=100)
    window_start: int = Field(default=9, ge=0, le=23)
    window_end: int = Field(default=18, ge=1, le=24)
    template_id: UUID | None = None
    mode: Literal["message", "agent", "operator"] = "message"
    connect_on_qualify: bool = True
    weekdays: list[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    retry_max: int = Field(default=0, ge=0, le=5)
    retry_delay_minutes: int = Field(default=30, ge=1, le=1440)

    @model_validator(mode="after")
    def valid_window(self):
        if self.window_start >= self.window_end:
            raise ValueError("Use a daytime window with start < end")
        if len(set(self.contact_ids)) != len(self.contact_ids):
            raise ValueError("Duplicate contacts")
        if len(set(self.weekdays)) != len(self.weekdays) or any(
            d not in range(7) for d in self.weekdays
        ):
            raise ValueError("Use unique weekdays 0 to 6")
        return self


class CampaignState(Input):
    status: Literal["running", "paused"]


class ResolveAttempt(Input):
    status: Literal["completed", "failed"]
    evidence: str = Field(min_length=10, max_length=80)


def valid_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as e:
        raise ValueError("Unknown IANA timezone") from e
    return value
