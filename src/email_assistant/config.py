import re
from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field, HttpUrl, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from email_assistant.security.secrets import SECRET_VERSION_PATTERN

PositiveInt = Annotated[int, Field(gt=0)]
TelegramId = Annotated[int, Field(gt=0, strict=True)]
ServicePort = Annotated[int, Field(ge=1, le=65535)]
SecretVersion = Annotated[str, Field(pattern=SECRET_VERSION_PATTERN)]


class Role(StrEnum):
    JOB = "job"
    INGRESS = "ingress"
    WORKER = "worker"


class ConfigurationError(RuntimeError):
    """A startup error safe to report without logging configuration values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MAILPILOT_", hide_input_in_errors=True, frozen=True
    )

    role: Role
    environment: Literal["production", "test"] = "production"
    project_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")] = ""
    timezone: Literal["Europe/Zurich"] = "Europe/Zurich"
    work_budget_seconds: PositiveInt = 240
    hard_timeout_seconds: PositiveInt = 600
    allowed_user_ids: tuple[TelegramId, ...] = ()
    allowed_chat_ids: tuple[TelegramId, ...] = ()
    agent_enabled: bool = False
    port: ServicePort = Field(default=8080, validation_alias="PORT")
    firestore_emulator_host: str | None = Field(
        default=None, validation_alias="FIRESTORE_EMULATOR_HOST"
    )

    telegram_bot_id: PositiveInt | None = None
    task_queue_path: (
        Annotated[
            str, Field(pattern=r"^projects/[a-z0-9-]+/locations/[a-z0-9-]+/queues/[A-Za-z0-9_-]+$")
        ]
        | None
    ) = None
    task_worker_url: HttpUrl | None = None
    task_invoker_email: (
        Annotated[str, Field(pattern=r"^[a-z0-9-]+@[a-z0-9.-]+[.]iam[.]gserviceaccount[.]com$")]
        | None
    ) = None

    telegram_webhook_secret_version: SecretVersion | None = Field(default=None, repr=False)
    telegram_bot_token_version: SecretVersion | None = Field(default=None, repr=False)
    model_api_key_version: SecretVersion | None = Field(default=None, repr=False)

    outlook_tenant_id: UUID | None = None
    outlook_client_id: UUID | None = None
    outlook_endpoint: HttpUrl | None = Field(default=None, repr=False)
    model_provider: Literal["openai", "gemini", "anthropic"] = "openai"
    model_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")] | None = None
    model_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 10
    model_budget_seconds: Annotated[float, Field(gt=0, le=60)] = 20
    model_max_attempts: Annotated[int, Field(ge=1, le=4)] = 2
    model_input_bytes: Annotated[int, Field(ge=512, le=24000)] = 12000
    model_output_tokens: Annotated[int, Field(ge=128, le=4096)] = 1200
    model_endpoint: HttpUrl | None = Field(default=None, repr=False)
    telegram_endpoint: HttpUrl | None = Field(default=None, repr=False)

    @field_validator("allowed_user_ids", "allowed_chat_ids")
    @classmethod
    def unique_ids(cls, ids: tuple[int, ...]) -> tuple[int, ...]:
        if len(set(ids)) != len(ids):
            raise ValueError("IDs must be unique")
        return ids

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.work_budget_seconds >= self.hard_timeout_seconds:
            raise PydanticCustomError(
                "mailpilot_policy", "work budget must leave time before the hard timeout"
            )
        if self.agent_enabled:
            raise PydanticCustomError("mailpilot_policy", "Agent adapters are unavailable")
        if self.role in (Role.INGRESS, Role.WORKER):
            if not self.allowed_user_ids or not self.allowed_chat_ids:
                raise PydanticCustomError(
                    "mailpilot_policy",
                    "service roles require Telegram user and private chat allowlists",
                )

        if self.role == Role.INGRESS and self.model_api_key_version is not None:
            raise PydanticCustomError("mailpilot_policy", "Ingress cannot use a model secret")
        if self.role != Role.INGRESS and self.telegram_webhook_secret_version is not None:
            raise PydanticCustomError(
                "mailpilot_policy", "only Ingress can use the Telegram webhook secret"
            )

        task_config = (self.task_queue_path, self.task_worker_url, self.task_invoker_email)
        if any(task_config) and not all(task_config):
            raise PydanticCustomError("mailpilot_policy", "Cloud Tasks configuration is incomplete")
        if self.task_worker_url:
            url = urlsplit(str(self.task_worker_url))
            if (
                url.scheme != "https"
                or url.path not in ("", "/")
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise PydanticCustomError("mailpilot_policy", "Worker URL must be an HTTPS origin")

        secret_versions = (
            self.telegram_webhook_secret_version,
            self.telegram_bot_token_version,
            self.model_api_key_version,
        )
        endpoints = (self.outlook_endpoint, self.model_endpoint, self.telegram_endpoint)
        if self.environment == "production":
            if self.firestore_emulator_host is not None or any(endpoints):
                raise PydanticCustomError(
                    "mailpilot_policy",
                    "emulator and provider overrides require the test environment",
                )
            if self.role == Role.INGRESS and self.telegram_webhook_secret_version is None:
                raise PydanticCustomError(
                    "mailpilot_policy", "Ingress requires a Telegram webhook secret reference"
                )
            if self.role in (Role.JOB, Role.WORKER):
                if self.telegram_bot_token_version is None or self.model_api_key_version is None:
                    raise PydanticCustomError(
                        "mailpilot_policy",
                        "Job and Worker require Telegram and model secret references",
                    )
        else:
            if any(secret_versions):
                raise PydanticCustomError("mailpilot_policy", "test mode uses injected secrets")
            if not self.project_id.startswith("demo-") or not self.firestore_emulator_host:
                raise PydanticCustomError(
                    "mailpilot_policy", "test mode requires a demo project and a Firestore emulator"
                )
            self._validate_test_addresses()
        return self

    def _validate_test_addresses(self) -> None:
        emulator = self.firestore_emulator_host or ""
        if not re.fullmatch(r"(?:localhost|127\.0\.0\.1|\[::1\]|firestore):[0-9]{1,5}", emulator):
            raise PydanticCustomError(
                "mailpilot_policy", "test mode requires a local Firestore emulator host and port"
            )
        if not 1 <= int(emulator.rsplit(":", 1)[1]) <= 65535:
            raise PydanticCustomError("mailpilot_policy", "invalid Firestore emulator port")
        for endpoint in (self.outlook_endpoint, self.model_endpoint, self.telegram_endpoint):
            if endpoint is None:
                continue
            if endpoint.host not in {"localhost", "127.0.0.1", "[::1]", "mock-services"}:
                raise PydanticCustomError(
                    "mailpilot_policy", "test endpoints must use a local mock host"
                )
            parts = (endpoint.username, endpoint.password, endpoint.query, endpoint.fragment)
            if any(part is not None for part in parts):
                raise PydanticCustomError(
                    "mailpilot_policy",
                    "test endpoints cannot contain credentials, queries or fragments",
                )
            if endpoint.port == 0:
                raise PydanticCustomError("mailpilot_policy", "invalid test endpoint port")


def load_settings(role: Role) -> Settings:
    try:
        return Settings(role=role)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False)
        fields = sorted(
            {
                field
                for error in errors
                if error["loc"] and (field := str(error["loc"][0])) in Settings.model_fields
            }
        )
        if fields:
            raise ConfigurationError(
                f"Invalid {role} configuration ({', '.join(fields)})."
            ) from None
        for error in errors:
            if error["type"] == "mailpilot_policy":
                raise ConfigurationError(f"{role}: {error['msg']}.") from None
        raise ConfigurationError(f"Invalid configuration for {role}.") from None
    except SettingsError:
        raise ConfigurationError(f"Invalid environment settings for {role}.") from None
