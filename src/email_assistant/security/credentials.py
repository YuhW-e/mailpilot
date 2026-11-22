from datetime import timedelta
from uuid import uuid4

from google.cloud.firestore_v1 import Transaction
from pydantic import SecretStr

from email_assistant.email.provider import (
    Credentials,
    EmailProvider,
    ErrorCode,
    Identity,
    ProviderError,
)
from email_assistant.security.account_secrets import AccountSecrets
from email_assistant.storage.firestore import AccountKey, FirestoreRepository, Lease, Record


class CredentialCoordinator:
    def __init__(
        self, repository: FirestoreRepository, secrets: AccountSecrets, provider: EmailProvider
    ) -> None:
        self.repository = repository
        self.secrets = secrets
        self.provider = provider

    def _lease(self, account: AccountKey) -> Lease:
        lease = self.repository.acquire(account, "credential", timedelta(seconds=90))
        if lease is None:
            raise ProviderError(ErrorCode.TEMPORARY, retry_after=1)
        return lease

    def _read(self, lease: Lease) -> Record:
        return self.repository.atomic(lambda tx: self.repository.fenced(lease, tx))

    def _update(self, lease: Lease, values: Record) -> None:
        def update(tx: Transaction) -> None:
            self.repository.fenced(lease, tx)
            tx.update(self.repository.ref("accounts", lease.account.account_id), values)

        self.repository.atomic(update)

    @staticmethod
    def identity(row: Record) -> Identity:
        return Identity(**row["identity"])

    def _commit(self, lease: Lease, version: str) -> None:
        def commit(tx: Transaction) -> None:
            row = self.repository.fenced(lease, tx)
            fields: Record = {
                "credential_status": "active",
                "credential_version": version,
                "credential_operation": None,
            }
            if row["credential_status"] != "active":
                fields["wake_generation"] = int(row["wake_generation"]) + 1
            tx.update(self.repository.ref("accounts", lease.account.account_id), fields)

        self.repository.atomic(commit)

    def _write(self, lease: Lease, operation: str, cache: SecretStr) -> str:
        row = self._read(lease)
        if row.get("credential_operation") != operation:
            raise ProviderError(ErrorCode.TEMPORARY)
        version = self.secrets.append(lease.account.account_id, operation, cache)
        self._commit(lease, version)
        return version

    def _disable(self, lease: Lease, *, keep: str | None = None) -> None:
        def valid() -> None:
            self._read(lease)

        self.secrets.disable(lease.account.account_id, check_lease=valid, keep=keep)

    def authorize(self, account: AccountKey, credentials: Credentials) -> None:
        lease = self._lease(account)
        try:
            row = self._read(lease)
            if row.get("revocation_pending") or self.identity(row) != credentials.identity:
                raise ProviderError(ErrorCode.AUTHORIZATION)
            operation = uuid4().hex
            self._update(lease, {"credential_operation": operation, "revocation_pending": False})
            version = self._write(lease, operation, credentials.cache)
            self._disable(lease, keep=version)
        finally:
            self.repository.release(lease)

    def _recover(self, lease: Lease, row: Record) -> str | None:
        version: str | None = row["credential_version"]
        operation: str | None = row.get("credential_operation")
        if operation:
            candidate = self.secrets.recover(lease.account.account_id, operation, version)
            if candidate:
                self._commit(lease, candidate)
                return candidate
        return version

    def access(self, account: AccountKey, *, force: bool = False) -> SecretStr:
        lease = self._lease(account)
        try:
            row = self._read(lease)
            if row["credential_status"] == "revoked":
                raise ProviderError(ErrorCode.REAUTHORIZE)
            version = self._recover(lease, row)
            if version is None or (
                row["credential_status"] != "active" and version == row["credential_version"]
            ):
                raise ProviderError(ErrorCode.REAUTHORIZE)
            cache = self.secrets.read(account.account_id, version)
            operation = uuid4().hex
            self._update(lease, {"credential_operation": operation})
            try:
                credentials = self.provider.refresh(self.identity(row), cache, force=force)
            except ProviderError as exc:
                if exc.code == ErrorCode.REAUTHORIZE:
                    self._update(
                        lease,
                        {
                            "credential_status": "reauthorize",
                            "credential_operation": None,
                        },
                    )
                raise
            if credentials.identity != self.identity(row):
                self._update(
                    lease,
                    {
                        "credential_status": "reauthorize",
                        "credential_operation": None,
                    },
                )
                raise ProviderError(ErrorCode.REAUTHORIZE)
            if credentials.cache == cache:
                self._update(lease, {"credential_operation": None})
            else:
                self._write(lease, operation, credentials.cache)
            return credentials.access_token
        finally:
            self.repository.release(lease)

    def rotate(self, account: AccountKey) -> None:
        self.access(account, force=True)
        lease = self._lease(account)
        try:
            row = self._read(lease)
            if row["credential_status"] != "active":
                raise ProviderError(ErrorCode.REAUTHORIZE)
            version = self._recover(lease, row)
            if version is None:
                raise ProviderError(ErrorCode.REAUTHORIZE)
            self._disable(lease, keep=version)
        finally:
            self.repository.release(lease)

    def revoke(self, account: AccountKey) -> None:
        lease = self._lease(account)
        try:
            self._update(
                lease,
                {
                    "credential_status": "revoked",
                    "credential_version": None,
                    "credential_operation": None,
                    "revocation_pending": True,
                },
            )
            self._disable(lease)
            self._update(lease, {"revocation_pending": False})
        finally:
            self.repository.release(lease)
