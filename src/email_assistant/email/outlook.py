import json
from collections import deque
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import SecretStr

from email_assistant.config import Settings
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
from email_assistant.security.outlook_oauth import OutlookOAuth

GRAPH = "https://graph.microsoft.com/v1.0"
EXCLUDED = ("sentitems", "drafts", "outbox", "junkemail", "deleteditems")
FIELDS = "id,receivedDateTime,from,subject,changeKey,isDraft"


def _id(data: Any) -> str:
    if not isinstance(data, str) or not data or len(data) > 1024:
        raise ProviderError(ErrorCode.INVALID_RESPONSE)
    return data


def _segment(value: str) -> str:
    if value in {".", ".."} or any(c in value for c in "/\\\x00"):
        raise ProviderError(ErrorCode.INVALID_RESPONSE)
    return quote(_id(value), safe="")


def _metadata(item: dict[str, Any]) -> MessageMetadata:
    received = datetime.fromisoformat(item["receivedDateTime"])
    if received.tzinfo is None:
        raise ValueError
    address = (item.get("from") or {}).get("emailAddress") or {}
    return MessageMetadata(
        _id(item["id"]),
        received.astimezone(UTC),
        (address.get("address") or "")[:1024],
        (item.get("subject") or "")[:2048],
        _id(item["changeKey"]),
    )


class OutlookProvider:
    def __init__(
        self,
        oauth: OutlookOAuth,
        *,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._oauth = oauth
        override = (
            settings.outlook_endpoint if settings and settings.environment == "test" else None
        )
        self._base = str(override).rstrip("/") if override else GRAPH
        self._transport = transport or httpx.HTTPTransport(retries=0, trust_env=False)

    def close(self) -> None:
        self._transport.close()

    def begin_authorization(self, redirect_uri: str) -> Authorization:
        return self._oauth.begin_authorization(redirect_uri)

    def complete_authorization(
        self, callback_uri: str, expected: Identity | None = None
    ) -> Credentials:
        return self._oauth.complete_authorization(callback_uri, expected)

    def refresh(self, identity: Identity, cache: SecretStr, *, force: bool = False) -> Credentials:
        return self._oauth.refresh(identity, cache, force=force)

    def _link(self, value: Any, path: str) -> str:
        try:
            if not isinstance(value, str) or len(value) > 16000:
                raise ValueError
            link = urlsplit(value)
            target = urlsplit(self._base + path)
            if (
                link.scheme != target.scheme
                or link.netloc.casefold() != target.netloc.casefold()
                or not link.query
                or any(c in value for c in "#\\\r\n\t")
                or not value.startswith(f"{target.scheme}://")
            ):
                raise ValueError
            actual = [unquote(part) for part in link.path.split("/")]
            expected = [unquote(part) for part in target.path.split("/")]
            if len(expected) > (index := expected.index("mailFolders") + 1):
                collection = actual[index - 1]
                if collection.casefold().startswith("mailfolders('") and collection.endswith("')"):
                    actual[index - 1 : index] = [collection[:11], collection[13:-2]]
                if actual[index] != expected[index]:
                    raise ValueError
                actual[index] = expected[index] = ""
            if [part.casefold() for part in actual] != [part.casefold() for part in expected]:
                raise ValueError
            return value
        except (TypeError, ValueError, IndexError):
            raise ProviderError(ErrorCode.INVALID_RESPONSE) from None

    def _get(
        self,
        url: str,
        token: SecretStr,
        *,
        body: bool = False,
        delta: bool = False,
    ) -> dict[str, Any]:
        prefer = 'IdType="ImmutableId"'
        if body:
            prefer += ', outlook.body-content-type="text"'
        if delta:
            prefer += ", odata.maxpagesize=100"
        try:
            request = httpx.Request(
                "GET",
                url,
                headers={"Authorization": f"Bearer {token.get_secret_value()}", "Prefer": prefer},
                extensions={"timeout": dict.fromkeys(("connect", "read", "write", "pool"), 10.0)},
            )
            response = self._transport.handle_request(request)
            try:
                status = response.status_code
                if status == 429 or status >= 500:
                    raise ProviderError(
                        ErrorCode.THROTTLED if status == 429 else ErrorCode.TEMPORARY,
                        retry_after=retry_after(response.headers.get("Retry-After")),
                    )
                if status == 401:
                    raise ProviderError(ErrorCode.REAUTHORIZE)
                if status == 410 and delta:
                    raise ProviderError(ErrorCode.CURSOR_EXPIRED)
                if status in {200, 400, 403, 404}:
                    limit = 32 * 1024 * 1024 if body else 4 * 1024 * 1024
                    payload = bytearray()
                    for chunk in response.iter_bytes():
                        payload.extend(chunk)
                        if len(payload) > limit:
                            raise ProviderError(ErrorCode.INVALID_RESPONSE)
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        data = {}
                    if isinstance(data, dict):
                        error = data.get("error")
                        if (
                            delta
                            and isinstance(error, dict)
                            and error.get("code", "").lower()
                            in {
                                "syncstatenotfound",
                                "resyncrequired",
                                "errorinvalidsyncstatedata",
                                "errorinvaliddeltatoken",
                            }
                        ):
                            raise ProviderError(ErrorCode.CURSOR_EXPIRED)
                        if status == 200:
                            return data
                if status in {403, 404}:
                    raise ProviderError(
                        ErrorCode.UNREADABLE if status == 403 else ErrorCode.NOT_FOUND
                    )
                raise ProviderError(ErrorCode.INVALID_RESPONSE)
            finally:
                response.close()
        except httpx.TransportError:
            raise ProviderError(ErrorCode.TEMPORARY) from None
        except (AttributeError, TypeError, ValueError, httpx.HTTPError):
            raise ProviderError(ErrorCode.INVALID_RESPONSE) from None

    def _folders(
        self, token: SecretStr, path: str, check_budget: Callable[[], None] | None
    ) -> Iterator[dict[str, Any]]:
        url = self._base + path
        visited: set[str] = set()
        while True:
            if url in visited:
                raise ProviderError(ErrorCode.INVALID_RESPONSE)
            visited.add(url)
            if check_budget is not None:
                check_budget()
            data = self._get(url, token)
            folders = data.get("value")
            if not isinstance(folders, list) or any(not isinstance(f, dict) for f in folders):
                raise ProviderError(ErrorCode.INVALID_RESPONSE)
            yield from folders
            if "@odata.nextLink" not in data:
                return
            url = self._link(data["@odata.nextLink"], path)

    def list_scopes(
        self, access_token: SecretStr, *, check_budget: Callable[[], None] | None = None
    ) -> Discovery:
        root = "/me/mailFolders"
        excluded: set[str] = set()
        for name in EXCLUDED:
            if check_budget is not None:
                check_budget()
            try:
                excluded.add(_id(self._get(f"{self._base}{root}/{name}", access_token)["id"]))
            except ProviderError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
            except KeyError:
                raise ProviderError(ErrorCode.INVALID_RESPONSE) from None

        queue: deque[tuple[str | None, str]] = deque([(None, root)])
        seen: set[str] = set()
        scopes: list[Scope] = []
        inaccessible: list[str] = []
        while queue:
            parent, path = queue.popleft()
            try:
                for item in self._folders(access_token, path, check_budget):
                    folder_id = _id(item.get("id"))
                    if (
                        folder_id in seen
                        or folder_id in excluded
                        or item.get("@odata.type", "") == "#microsoft.graph.mailSearchFolder"
                    ):
                        continue
                    seen.add(folder_id)
                    scopes.append(Scope(folder_id))
                    child_count = item.get("childFolderCount", 0)
                    if not isinstance(child_count, int) or child_count < 0:
                        raise ProviderError(ErrorCode.INVALID_RESPONSE)
                    if child_count:
                        queue.append((folder_id, f"{root}/{_segment(folder_id)}/childFolders"))
            except ProviderError as exc:
                if parent is None or exc.code not in {ErrorCode.UNREADABLE, ErrorCode.NOT_FOUND}:
                    raise
                inaccessible.append(parent)
        return Discovery(tuple(scopes), tuple(inaccessible))

    def sync_page(
        self, access_token: SecretStr, scope: Scope, continuation: str | None = None
    ) -> SyncPage:
        path = f"/me/mailFolders/{_segment(scope.id)}/messages/delta"
        url = (
            self._link(continuation, path)
            if continuation
            else f"{self._base}{path}?$select={FIELDS}"
        )
        data = self._get(url, access_token, delta=True)
        try:
            if not isinstance(data["value"], list):
                raise ValueError
            changes = tuple(
                Change(
                    _id(item["id"]),
                    None if "@removed" in item or item.get("isDraft") else _metadata(item),
                )
                for item in data["value"]
            )
            return SyncPage(
                changes,
                next_page=self._link(data["@odata.nextLink"], path)
                if "@odata.nextLink" in data
                else None,
                cursor=self._link(data["@odata.deltaLink"], path)
                if "@odata.deltaLink" in data
                else None,
            )
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise ProviderError(ErrorCode.INVALID_RESPONSE) from None

    def get_message(self, access_token: SecretStr, message_id: str) -> MessageBody:
        url = f"{self._base}/me/messages/{_segment(message_id)}?$select={FIELDS},body"
        data = self._get(url, access_token, body=True)
        try:
            metadata = _metadata(data)
            if metadata.id != message_id or data.get("isDraft"):
                raise ProviderError(ErrorCode.NOT_FOUND)
            content = data["body"]["content"]
            kind = data["body"]["contentType"].lower()
            if not isinstance(content, str) or kind not in {"text", "html"}:
                raise ValueError
            if kind == "html":
                content = html_text(content)
            return MessageBody(metadata, content)
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            raise ProviderError(ErrorCode.INVALID_RESPONSE) from None
