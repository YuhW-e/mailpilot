import json
import re
from collections.abc import Callable, Iterator
from typing import Literal, Protocol

from google.api_core.exceptions import AlreadyExists
from google.cloud import secretmanager
from pydantic import SecretStr

from email_assistant.security.secrets import SecretManagerReader, SecretReadError


class CredentialStoreError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Unable to access account credential storage.")


class AccountSecrets(Protocol):
    def append(self, account_id: str, operation: str, cache: SecretStr) -> str: ...

    def recover(self, account_id: str, operation: str, committed: str | None) -> str | None: ...

    def read(self, account_id: str, version: str) -> SecretStr: ...

    def disable(
        self, account_id: str, *, check_lease: Callable[[], None], keep: str | None = None
    ) -> None: ...


class SecretManagerAccountSecrets:
    def __init__(
        self,
        project: str,
        client: secretmanager.SecretManagerServiceClient | None = None,
        *,
        provider: Literal["outlook", "gmail"] = "outlook",
    ) -> None:
        if not re.fullmatch(r"[a-z0-9-]+", project):
            raise ValueError("Invalid credential project.")
        self._provider = provider
        self._project = f"projects/{project}"
        self._client = client or secretmanager.SecretManagerServiceClient()
        self._reader = SecretManagerReader(self._client)

    def _parent(self, account_id: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", account_id):
            raise CredentialStoreError()
        return f"{self._project}/secrets/mailpilot-{self._provider}-{account_id}"

    def ensure(self, account_id: str) -> None:
        parent = self._parent(account_id)
        try:
            self._client.create_secret(
                request={
                    "parent": self._project,
                    "secret_id": parent.rsplit("/", 1)[-1],
                    "secret": {"replication": {"automatic": {}}},
                },
                retry=None,
                timeout=5,
            )
        except AlreadyExists:
            pass
        except Exception:
            raise CredentialStoreError() from None

    def _validate_version(self, account_id: str, version: str) -> None:
        project, _, suffix = version.partition("/secrets/")
        parent = self._parent(account_id)
        # Secret Manager may canonicalize a project ID to its project number. Use our project
        # for every access and validate the account-specific resource suffix separately.
        if not re.fullmatch(r"projects/[a-z0-9-]+", project) or not re.fullmatch(
            re.escape(parent.rsplit("/", 1)[-1]) + r"/versions/[1-9][0-9]*", suffix
        ):
            raise CredentialStoreError()

    def _canonical(self, account_id: str, version: str) -> str:
        self._validate_version(account_id, version)
        return f"{self._parent(account_id)}/versions/{version.rsplit('/', 1)[-1]}"

    def append(self, account_id: str, operation: str, cache: SecretStr) -> str:
        data = json.dumps({"operation": operation, "cache": cache.get_secret_value()})
        try:
            version = self._client.add_secret_version(
                request={"parent": self._parent(account_id), "payload": {"data": data.encode()}},
                retry=None,
                timeout=8,
            )
            return self._canonical(account_id, version.name)
        except Exception:
            raise CredentialStoreError() from None

    def _payload(self, account_id: str, version: str) -> tuple[str, SecretStr]:
        try:
            raw = self._reader.read(self._canonical(account_id, version)).get_secret_value()
            payload = json.loads(raw)
            if not all(isinstance(payload.get(field), str) for field in ("operation", "cache")):
                raise ValueError
            return payload["operation"], SecretStr(payload["cache"])
        except (ValueError, AttributeError, SecretReadError):
            raise CredentialStoreError() from None

    def read(self, account_id: str, version: str) -> SecretStr:
        return self._payload(account_id, version)[1]

    def _versions(self, account_id: str) -> Iterator[str]:
        try:
            for version in self._client.list_secret_versions(
                request={"parent": self._parent(account_id), "filter": "state=ENABLED"},
                retry=None,
                timeout=5,
            ):
                yield self._canonical(account_id, version.name)
        except Exception:
            raise CredentialStoreError() from None

    def recover(self, account_id: str, operation: str, committed: str | None) -> str | None:
        if committed:
            self._validate_version(account_id, committed)
        lower_bound = int(committed.rsplit("/", 1)[-1]) if committed else 0
        for version in self._versions(account_id):
            if int(version.rsplit("/", 1)[-1]) <= lower_bound:
                break
            if self._payload(account_id, version)[0] == operation:
                return version
        return None

    def disable(
        self, account_id: str, *, check_lease: Callable[[], None], keep: str | None = None
    ) -> None:
        try:
            if keep:
                self._validate_version(account_id, keep)
            keep_number = int(keep.rsplit("/", 1)[-1]) if keep else None
            for version in self._versions(account_id):
                if keep_number is None or int(version.rsplit("/", 1)[-1]) < keep_number:
                    check_lease()
                    self._client.disable_secret_version(
                        request={"name": version}, retry=None, timeout=5
                    )
        except Exception:
            raise CredentialStoreError() from None
