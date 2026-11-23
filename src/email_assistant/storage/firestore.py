from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from google.cloud import firestore
from google.cloud.firestore_v1 import Client, DocumentReference, Transaction

from email_assistant.email.provider import Identity

type Record = dict[str, Any]


class AccountAccessError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Account is unavailable for this owner.")


class LeaseLost(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Account lease or checkpoint no longer matches.")


@dataclass(frozen=True)
class AccountKey:
    owner_id: str
    account_id: str


@dataclass(frozen=True)
class Lease:
    account: AccountKey
    purpose: str
    fence: int


def utc_now() -> datetime:
    return datetime.now(UTC)


class FirestoreRepository:
    def __init__(self, client: Client, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.client = client
        self.clock = clock

    def ref(self, collection: str, key: str) -> DocumentReference:
        return self.client.collection(collection).document(key)

    def atomic[T](self, action: Callable[[Transaction], T]) -> T:
        return cast(T, firestore.transactional(action)(self.client.transaction()))

    @staticmethod
    def read(ref: DocumentReference, tx: Transaction | None = None) -> Record | None:
        return ref.get(transaction=tx).to_dict()

    def account(self, key: AccountKey, tx: Transaction | None = None) -> Record:
        if (
            not key.owner_id
            or len(key.account_id) != 64
            or any(c not in "0123456789abcdef" for c in key.account_id)
        ):
            raise AccountAccessError()
        row = self.read(self.ref("accounts", key.account_id), tx)
        if row is None or row["owner_id"] != key.owner_id:
            raise AccountAccessError()
        return row

    def register(self, owner_id: str, identity: Identity) -> AccountKey:
        if not owner_id:
            raise AccountAccessError()
        key = AccountKey(owner_id, identity.account_id)
        ref = self.ref("accounts", key.account_id)

        def create(tx: Transaction) -> None:
            existing = self.read(ref, tx)
            identity_data = {
                "provider": identity.provider,
                "issuer_tenant": identity.issuer_tenant,
                "subject": identity.subject,
            }
            if existing is None:
                tx.create(
                    ref,
                    {
                        "owner_id": owner_id,
                        "identity": identity_data,
                        "credential_status": "reauthorize",
                        "credential_version": None,
                        "registered_at": self.clock(),
                        "wake_generation": 0,
                        "completed_generation": 0,
                    },
                )
            elif existing["owner_id"] != owner_id or existing["identity"] != identity_data:
                raise AccountAccessError()

        self.atomic(create)
        return key

    def acquire(self, key: AccountKey, purpose: str, ttl: timedelta) -> Lease | None:
        if purpose not in {"sync", "credential", "urgent"} or ttl <= timedelta(0):
            raise ValueError("Invalid account lease.")
        field = f"{purpose}_lease"

        def claim(tx: Transaction) -> Lease | None:
            row = self.account(key, tx)
            now = self.clock()
            previous = row.get(field, {})
            if previous.get("until", now) > now:
                return None
            fence = previous.get("fence", 0) + 1
            tx.update(
                self.ref("accounts", key.account_id),
                {
                    field: {"fence": fence, "until": now + ttl},
                },
            )
            return Lease(key, purpose, fence)

        return self.atomic(claim)

    def fenced(self, lease: Lease, tx: Transaction) -> Record:
        row = self.account(lease.account, tx)
        held = row.get(f"{lease.purpose}_lease", {})
        if held.get("fence") != lease.fence or held["until"] <= self.clock():
            raise LeaseLost()
        return row

    def release(self, lease: Lease) -> None:
        def release(tx: Transaction) -> None:
            row = self.account(lease.account, tx)
            field = f"{lease.purpose}_lease"
            if row.get(field, {}).get("fence") == lease.fence:
                tx.update(
                    self.ref("accounts", lease.account.account_id),
                    {
                        f"{field}.until": self.clock(),
                    },
                )

        self.atomic(release)
