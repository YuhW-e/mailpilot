from google.cloud.firestore_v1 import FieldFilter, Transaction

from email_assistant.storage.firestore import (
    AccountAccessError,
    AccountKey,
    FirestoreRepository,
    Lease,
    Record,
)


class Subscriptions:
    def __init__(self, db: FirestoreRepository) -> None:
        self.db = db

    def lookup(self, field: str, value: str, provider: str) -> tuple[AccountKey, Record] | None:
        query = self.db.client.collection("subscriptions").where(
            filter=FieldFilter(field, "==", value)
        )
        matches = []
        for doc in query.stream():
            row = doc.to_dict() or {}
            if row.get("provider") != provider or row.get("expires_at") is None:
                continue
            if row["expires_at"] <= self.db.clock():
                continue
            account = AccountKey(row["owner_id"], doc.id)
            try:
                registered = self.db.account(account)
            except AccountAccessError:
                continue
            if (
                registered["credential_status"] == "active"
                and registered["identity"]["provider"] == provider
            ):
                matches.append((account, row))
                if len(matches) > 1:
                    return None
        return matches[0] if matches else None

    def read(self, account: AccountKey) -> Record:
        return self.db.read(self.db.ref("subscriptions", account.account_id)) or {}

    def save(self, lease: Lease, provider: str, fields: Record) -> None:
        account = lease.account
        ref = self.db.ref("subscriptions", account.account_id)

        def write(tx: Transaction) -> None:
            registered = self.db.fenced(lease, tx)
            if (
                registered["credential_status"] != "active"
                or registered["identity"]["provider"] != provider
            ):
                raise AccountAccessError()
            tx.set(ref, {"owner_id": account.owner_id, "provider": provider, **fields}, merge=True)

        self.db.atomic(write)
