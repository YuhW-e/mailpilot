import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Protocol

from pydantic import SecretStr


def retry_after(value: str | None) -> float | None:
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None
        if -31_536_000 <= delay <= 31_536_000:
            return max(0, delay)
    return None


def stable_key(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True).encode()).hexdigest()


class ErrorCode(StrEnum):
    CONFLICT = "conflict"
    AUTHORIZATION = "authorization_rejected"
    REAUTHORIZE = "reauthorization_required"
    THROTTLED = "throttled"
    TEMPORARY = "temporarily_unavailable"
    UNREADABLE = "unreadable"
    NOT_FOUND = "not_found"
    CURSOR_EXPIRED = "cursor_expired"
    INVALID_RESPONSE = "invalid_provider_response"


class ProviderError(RuntimeError):
    def __init__(self, code: ErrorCode, *, retry_after: float | None = None) -> None:
        super().__init__(code.value)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class Identity:
    provider: str
    issuer_tenant: str
    subject: str

    @property
    def account_id(self) -> str:
        return stable_key(self.provider, self.issuer_tenant, self.subject)


@dataclass(frozen=True, repr=False)
class Authorization:
    url: str
    state: str


@dataclass(frozen=True)
class Credentials:
    identity: Identity
    access_token: SecretStr
    cache: SecretStr


@dataclass(frozen=True)
class Scope:
    id: str


@dataclass(frozen=True)
class Discovery:
    scopes: tuple[Scope, ...]
    inaccessible: tuple[str, ...] = ()


@dataclass(frozen=True)
class MessageMetadata:
    id: str
    received_at: datetime
    sender: str
    subject: str
    version: str


@dataclass(frozen=True)
class Change:
    id: str
    message: MessageMetadata | None = None


@dataclass(frozen=True)
class SyncPage:
    changes: tuple[Change, ...]
    next_page: str | None = field(default=None, repr=False)
    cursor: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if bool(self.next_page) == bool(self.cursor):
            raise ProviderError(ErrorCode.INVALID_RESPONSE)


@dataclass(frozen=True)
class MessageBody:
    metadata: MessageMetadata
    text: str = field(repr=False)


class EmailProvider(Protocol):
    def begin_authorization(self, redirect_uri: str) -> Authorization: ...

    def complete_authorization(
        self, callback_uri: str, expected: Identity | None = None
    ) -> Credentials: ...

    def refresh(
        self, identity: Identity, cache: SecretStr, *, force: bool = False
    ) -> Credentials: ...

    def list_scopes(
        self, access_token: SecretStr, *, check_budget: Callable[[], None] | None = None
    ) -> Discovery: ...

    def sync_page(
        self, access_token: SecretStr, scope: Scope, continuation: str | None = None
    ) -> SyncPage: ...

    def get_message(self, access_token: SecretStr, message_id: str) -> MessageBody: ...
