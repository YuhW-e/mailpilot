import re
from typing import Protocol

from google.cloud import secretmanager
from pydantic import SecretStr

SECRET_VERSION_PATTERN = (
    r"^projects/[a-z0-9-]+/secrets/[A-Za-z0-9_-]+/versions/(?:[1-9][0-9]*|latest)$"
)


class SecretReadError(RuntimeError):
    """A secret read failure safe to report without the reference or payload."""


class SecretReader(Protocol):
    def read(self, version: str) -> SecretStr: ...


class SecretManagerReader:
    def __init__(self, client: secretmanager.SecretManagerServiceClient | None = None) -> None:
        self._client = client

    def read(self, version: str) -> SecretStr:
        if re.fullmatch(SECRET_VERSION_PATTERN, version) is None:
            raise SecretReadError("Invalid secret version reference.")
        try:
            if self._client is None:
                self._client = secretmanager.SecretManagerServiceClient()
            response = self._client.access_secret_version(
                request={"name": version}, retry=None, timeout=5.0
            )
            value = response.payload.data.decode("utf-8")
        except Exception:
            # Client and decoding failures can include resource names or credential material.
            raise SecretReadError("Unable to read secret.") from None
        if not value:
            raise SecretReadError("Secret payload is empty.")
        return SecretStr(value)
