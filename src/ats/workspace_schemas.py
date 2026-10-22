"""Validated commands for the operator workspace. No arbitrary ORM/setting dictionaries."""

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from ats.schemas import CampaignCreate, ContactCreate, Input, phone_e164, valid_timezone


class ContactEdit(ContactCreate):
    group: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=254)
    notes: str = Field(default="", max_length=10000)
    blocked: bool = False


class ImportContacts(Input):
    text: str = Field(max_length=180000)
    list_id: UUID | None = None
    consent: bool = False
    consent_source: str = Field(default="", max_length=256)

    @model_validator(mode="after")
    def source(self):
        if self.consent and not self.consent_source.strip():
            raise ValueError("Consent requires a source")
        return self


class ListSave(Input):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class Members(Input):
    contact_ids: list[UUID] = Field(min_length=1, max_length=1000)


class CampaignEdit(CampaignCreate):
    # A full validated command; memberships are edited through /members.
    contact_ids: list[UUID] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def members_unchanged(self):
        if self.contact_ids:
            raise ValueError("Use the campaign members command")
        return self


class Choice(Input):
    next: str = Field(default="continue", max_length=80)
    qualified: bool | None = None


class Question(Input):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=2000)
    choices: dict[str, Choice] = Field(default_factory=dict, max_length=20)

    @field_validator("choices")
    @classmethod
    def normalize_choices(cls, choices):
        result = {}
        for key, choice in choices.items():
            normalized = key.strip().lower()
            normalized = (
                "1"
                if normalized in {"да", "yes", "y", "ага", "конечно", "хочу", "интересует", "1"}
                else "2"
                if normalized in {"нет", "no", "n", "не", "не надо", "не интересует", "2"}
                else normalized
            )
            if normalized in result:
                raise ValueError("Duplicate normalized choices")
            result[normalized] = choice
        return result


class Scenario(Input):
    greeting: str = Field(default="Здравствуйте, {name}!", max_length=2000)
    intro: str = Field(default="", max_length=2000)
    questions: list[Question] = Field(default_factory=list, max_length=30)
    default_qualified: bool = False
    qualified_text: str = Field(default="Спасибо, передаю информацию специалисту.", max_length=2000)
    not_qualified_text: str = Field(default="Спасибо, до свидания.", max_length=2000)
    goodbye: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def graph(self):
        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("Question ids must be unique")
        for q in self.questions:
            for choice in q.choices.values():
                if choice.next not in {"continue", "end", *ids}:
                    raise ValueError("Unknown question target")
        return self


class TemplateSave(Input):
    name: str = Field(min_length=1, max_length=120)
    text: str = Field(default="", max_length=10000)
    scenario: Scenario = Field(default_factory=Scenario)


class ScenarioTest(Input):
    answers: dict[str, str] = Field(default_factory=dict, max_length=30)
    name: str = Field(default="Тест", max_length=120)


class NumberSave(Input):
    phone: str = Field(pattern=r"^[0-9]{10}$")
    label: str = Field(default="", max_length=120)
    enabled: bool = True
    verified: bool = False
    quarantined: bool = False
    daily_limit: int = Field(default=30, ge=1, le=100000)
    cooldown_seconds: int = Field(default=60, ge=0, le=86400)


class BlockSave(Input):
    phone: str
    reason: str = Field(min_length=1, max_length=500)
    _phone = field_validator("phone")(phone_e164)


class UserSave(Input):
    login: str = Field(pattern=r"^[a-zA-Z0-9_.@-]{1,120}$")
    password: str = Field(default="", max_length=256)
    role: Literal["admin", "operator", "viewer"] = "operator"
    active: bool = True
    name: str = Field(default="", max_length=120)
    extension: str = Field(default="", pattern=r"^[0-9]{0,20}$")

    @field_validator("password")
    @classmethod
    def strong_password(cls, value):
        if value and len(value) < 12:
            raise ValueError("Password must have at least 12 characters")
        return value


class PasswordChange(Input):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class OperatorStatus(Input):
    status: Literal["free", "break", "offline"]


class Outcome(Input):
    outcome: str = Field(min_length=1, max_length=500)


class WorkspaceSave(Input):
    name: str = Field(min_length=1, max_length=120)
    timezone: str = "Europe/Moscow"
    max_channels: int = Field(ge=1, le=100)
    daily_limit: int = Field(ge=1, le=100000)
    _timezone = field_validator("timezone")(valid_timezone)


class ProviderSettings(Input):
    # Secrets remain in the server environment and are never persisted as plain JSON.
    megafon_user: str = Field(default="", max_length=120)
    amo_responsible_user_id: int | None = Field(default=None, gt=0)
    amo_auto_create_contacts: bool = False
    amo_auto_create_tasks: bool = False
    task_due_hours: int = Field(default=24, ge=1, le=168)
    default_mode: Literal["message", "agent", "operator"] = "message"
    auto_quarantine_on_complaints: int = Field(default=3, ge=1, le=100)


class RetryJob(Input):
    evidence: str = Field(min_length=10, max_length=80)
