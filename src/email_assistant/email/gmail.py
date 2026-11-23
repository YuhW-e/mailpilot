import base64
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import Message
from typing import Any

import httpx
from pydantic import SecretStr

from email_assistant.email.message_text import html_text
from email_assistant.email.provider import (
    Authorization,
    Change,
    Credentials,
    Discovery,
    ErrorCode,
    Identity,
    MessageBody,
    MessageMetadata,
    ProviderError,
    Scope,
    SyncPage,
    retry_after,
)
from email_assistant.security.gmail_oauth import GmailOAuth

GMAIL = "https://gmail.googleapis.com/gmail/v1"
MAILBOX = Scope("gmail-mailbox")
PAGE_SIZE = 8
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
EXCLUDED = {"DRAFT", "SPAM", "TRASH"}


def _string(value: Any, pattern: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(pattern, value, re.ASCII):
        raise ValueError
    return value


def _history_id(value: Any) -> str:
    return _string(value, r"[0-9]{1,30}")


def _message_id(value: Any) -> str:
    return _string(value, r"[a-zA-Z0-9_-]{1,128}")


def _part_fields(depth: int) -> str:
    fields = "mimeType,filename,headers,body(data)"
    return fields + (f",parts({_part_fields(depth - 1)})" if depth else "")


def _headers(part: dict[str, Any]) -> dict[str, str]:
    return {header["name"].lower(): header["value"] for header in part.get("headers", [])}


def _body(part: dict[str, Any]) -> str:
    headers = _headers(part)
    if part.get("filename") or headers.get("content-disposition", "").lower().startswith(
        "attachment"
    ):
        return ""
    kind = part.get("mimeType", "").lower()
    if kind.startswith("multipart/"):
        parts = part.get("parts", [])
        if kind == "multipart/alternative":
            parts = sorted(parts, key=lambda child: child.get("mimeType") != "text/plain")
            for child in parts:
                if value := _body(child):
                    return value
            return ""
        return "\n".join(filter(None, (_body(child) for child in parts)))
    encoded = part.get("body", {}).get("data")
    if kind not in {"text/plain", "text/html"} or not encoded:
        return ""
    message = Message()
    message["Content-Type"] = headers.get("content-type", kind)
    raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    try:
        text = raw.decode(message.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    return html_text(text) if kind == "text/html" else text


def _checkpoint(phase: str, baseline: str, token: str | None = None, offset: int = 0) -> str:
    return json.dumps([phase, baseline, token, offset], separators=(",", ":"))


class GmailProvider:
    def __init__(
        self,
        oauth: GmailOAuth,
        *,
        endpoint: str = GMAIL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._oauth = oauth
        self._base = endpoint.rstrip("/") + "/users/me"
        self._http = httpx.Client(
            transport=transport, timeout=5, follow_redirects=False, trust_env=False
        )

    def close(self) -> None:
        self._http.close()
        self._oauth.close()

    def begin_authorization(self, redirect_uri: str) -> Authorization:
        return self._oauth.begin_authorization(redirect_uri)

    def complete_authorization(
        self, callback_uri: str, expected: Identity | None = None
    ) -> Credentials:
        return self._oauth.complete_authorization(callback_uri, expected)

    def refresh(self, identity: Identity, cache: SecretStr, *, force: bool = False) -> Credentials:
        return self._oauth.refresh(identity, cache, force=force)

    def list_scopes(
        self, access_token: SecretStr, *, check_budget: Callable[[], None] | None = None
    ) -> Discovery:
        return Discovery((MAILBOX,))

    def _get(
        self,
        token: SecretStr,
        path: str,
        *,
        params: dict[str, str | int],
    ) -> dict[str, Any]:
        try:
            with self._http.stream(
                "GET",
                self._base + path,
                params=params,
                headers={"Authorization": f"Bearer {token.get_secret_value()}"},
            ) as response:
                status = response.status_code
                if status == 401:
                    raise ProviderError(ErrorCode.REAUTHORIZE)
                if status == 429 or status >= 500:
                    code = ErrorCode.THROTTLED if status == 429 else ErrorCode.TEMPORARY
                    raise ProviderError(
                        code, retry_after=retry_after(response.headers.get("Retry-After"))
                    )
                if status == 404 and path == "/history":
                    raise ProviderError(ErrorCode.CURSOR_EXPIRED)
                if status == 404 and path.startswith("/messages/"):
                    raise ProviderError(ErrorCode.NOT_FOUND)
                if not response.is_success:
                    error = (
                        next(response.iter_bytes(chunk_size=8192), b"")
                        if status in {400, 403}
                        else b""
                    )
                    reasons = {"userRateLimitExceeded", "rateLimitExceeded", "quotaExceeded"}
                    try:
                        detail = json.loads(error)["error"].get("errors", [])
                    except (ValueError, KeyError, TypeError, AttributeError):
                        detail = []
                    if any(
                        item.get("reason") in reasons for item in detail if isinstance(item, dict)
                    ):
                        raise ProviderError(
                            ErrorCode.THROTTLED,
                            retry_after=retry_after(response.headers.get("Retry-After")),
                        )
                    if status == 400 and "pageToken" in params:
                        raise ProviderError(ErrorCode.CURSOR_EXPIRED)
                    raise ProviderError(ErrorCode.AUTHORIZATION)
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ValueError
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except httpx.TransportError:
            raise ProviderError(ErrorCode.TEMPORARY) from None
        except ValueError:
            raise ProviderError(ErrorCode.INVALID_RESPONSE) from None

    def sync_page(
        self,
        access_token: SecretStr,
        scope: Scope,
        continuation: str | None = None,
        *,
        start: datetime | None = None,
        check_budget: Callable[[], None] = lambda: None,
    ) -> SyncPage:
        try:
            if scope != MAILBOX:
                raise ValueError
            if continuation is None:
                profile = self._get(access_token, "/profile", params={"fields": "historyId"})
                return SyncPage(
                    (), next_page=_checkpoint("list", _history_id(profile["historyId"]))
                )
            if continuation.isdecimal():
                phase, baseline, page, offset = "history", _history_id(continuation), None, 0
            else:
                phase, baseline, page, offset = json.loads(continuation)
                baseline = _history_id(baseline)
                if (
                    phase not in {"list", "history"}
                    or page is not None
                    and not isinstance(page, str)
                    or type(offset) is not int
                    or offset < 0
                ):
                    raise ValueError
            if phase == "list":
                params: dict[str, str | int] = {
                    "maxResults": PAGE_SIZE,
                    "fields": "messages(id),nextPageToken",
                }
                if start is not None:
                    params["q"] = f"after:{int(start.timestamp()) - 1}"
                if page:
                    params["pageToken"] = page
                listing = self._get(access_token, "/messages", params=params)
                ids = [_message_id(row["id"]) for row in listing.get("messages", [])]
                follow = listing.get("nextPageToken")
                return SyncPage(
                    self._changes(access_token, ids, check_budget),
                    next_page=_checkpoint("list" if follow else "history", baseline, follow),
                )
            return self._history(access_token, baseline, page, offset, check_budget)
        except (TypeError, KeyError, ValueError, AttributeError, OverflowError):
            raise ProviderError(ErrorCode.INVALID_RESPONSE) from None

    def _history(
        self,
        token: SecretStr,
        baseline: str,
        page: str | None,
        offset: int,
        check_budget: Callable[[], None],
    ) -> SyncPage:
        params: dict[str, str | int] = {
            "startHistoryId": baseline,
            "maxResults": PAGE_SIZE,
            "fields": (
                "history(messagesAdded(message(id)),messagesDeleted(message(id)),"
                "labelsAdded(message(id)),labelsRemoved(message(id))),nextPageToken,historyId"
            ),
        }
        if page:
            params["pageToken"] = page
        result = self._get(token, "/history", params=params)
        ids = [
            _message_id(change["message"]["id"])
            for entry in result.get("history", [])
            for field in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved")
            for change in entry.get(field, [])
        ]
        if offset > len(ids):
            raise ProviderError(ErrorCode.CURSOR_EXPIRED)
        selected = list(dict.fromkeys(ids[offset : offset + PAGE_SIZE]))
        changes = self._changes(token, selected, check_budget)
        if offset + PAGE_SIZE < len(ids):
            return SyncPage(
                changes, next_page=_checkpoint("history", baseline, page, offset + PAGE_SIZE)
            )
        if follow := result.get("nextPageToken"):
            return SyncPage(changes, next_page=_checkpoint("history", baseline, follow))
        cursor = _history_id(result["historyId"])
        if int(cursor) < int(baseline):
            raise ValueError
        return SyncPage(changes, cursor=cursor)

    def _changes(
        self, token: SecretStr, ids: list[str], check_budget: Callable[[], None]
    ) -> tuple[Change, ...]:
        changes = []
        for message_id in ids:
            check_budget()
            try:
                row = self._read(token, message_id, full=False)
                metadata = self._metadata(token, row, message_id)
            except ProviderError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
                metadata = None
            changes.append(Change(message_id, metadata))
        return tuple(changes)

    def _read(self, token: SecretStr, message_id: str, *, full: bool) -> dict[str, Any]:
        fields = _part_fields(6) if full else "headers"
        return self._get(
            token,
            f"/messages/{_message_id(message_id)}",
            params={
                "format": "full" if full else "metadata",
                "fields": f"id,labelIds,internalDate,payload({fields})",
            },
        )

    def _metadata(
        self, token: SecretStr, row: dict[str, Any], message_id: str
    ) -> MessageMetadata | None:
        if row["id"] != message_id:
            raise ValueError
        labels = set(row.get("labelIds", []))
        if labels & EXCLUDED:
            return None
        headers = _headers(row["payload"])
        if "SENT" in labels and "INBOX" not in labels:
            delivered_to = headers.get("delivered-to")
            if not delivered_to:
                return None
            address = self._get(token, "/profile", params={"fields": "emailAddress"})[
                "emailAddress"
            ]
            if delivered_to.strip().casefold() != address.casefold():
                return None
        received = datetime.fromtimestamp(int(row["internalDate"]) / 1000, UTC)
        # Gmail IDs identify immutable message content. Label changes do not change its body.
        return MessageMetadata(
            message_id,
            received,
            headers.get("from", "")[:512],
            headers.get("subject", "")[:1000],
            message_id,
        )

    def get_message(self, access_token: SecretStr, message_id: str) -> MessageBody:
        try:
            row = self._read(access_token, message_id, full=True)
            metadata = self._metadata(access_token, row, message_id)
            if metadata is None:
                raise ProviderError(ErrorCode.NOT_FOUND)
            return MessageBody(metadata, _body(row["payload"]))
        except (TypeError, KeyError, ValueError, AttributeError, OverflowError, RecursionError):
            raise ProviderError(ErrorCode.INVALID_RESPONSE) from None
