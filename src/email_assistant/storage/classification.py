from dataclasses import dataclass

from google.cloud.firestore_v1 import DocumentReference, FieldFilter, Transaction

from email_assistant.ai.classification import Classification
from email_assistant.email.provider import MessageMetadata, stable_key
from email_assistant.storage.business import BusinessRepository, TaskRecord
from email_assistant.storage.firestore import AccountAccessError, Lease, LeaseLost
from email_assistant.storage.sync import SyncRepository


@dataclass(frozen=True)
class ClassificationWork:
    metadata: MessageMetadata
    scope_ids: tuple[str, ...]
    revision: int


class ClassificationRepository:
    def __init__(self, sync: SyncRepository) -> None:
        self.sync = sync
        self.db = sync.db
        self.rules = BusinessRepository(self.db)

    def _refs(self, lease: Lease, message_id: str) -> tuple[DocumentReference, DocumentReference]:
        account = lease.account
        key = stable_key(account.account_id, message_id)
        return (
            self.db.ref("emails", key),
            self.rules.ref(
                TaskRecord, account.owner_id, account.account_id, stable_key("classify", key)
            ),
        )

    def load(self, lease: Lease, message_id: str) -> ClassificationWork | Classification | None:
        if self.db.account(lease.account)["credential_status"] == "revoked":
            raise AccountAccessError()
        row = self.sync.indexed_message(lease.account, message_id)
        email_ref, task_ref = self._refs(lease, message_id)
        task = self.db.read(task_ref)
        if task is None:
            return None
        if (
            task["owner_id"] != lease.account.owner_id
            or task["account_id"] != lease.account.account_id
            or task["email_key"] != email_ref.id
            or task["action"] != "classify"
        ):
            raise AccountAccessError()
        if task["state"] == "done":
            return Classification.model_validate(row["classification"])
        if task["state"] != "pending" or task["next_check_at"] > self.db.clock():
            return None
        scopes = self.db.client.collection("scope_memberships").where(
            filter=FieldFilter("email_key", "==", email_ref.id)
        )
        scope_ids = set()
        for snapshot in scopes.stream():
            membership = snapshot.to_dict()
            if membership is not None:
                if (
                    membership["owner_id"] != lease.account.owner_id
                    or membership["account_id"] != lease.account.account_id
                ):
                    raise AccountAccessError()
                scope_ids.add(str(membership["scope_id"]))
        return ClassificationWork(
            MessageMetadata(
                message_id, row["received_at"], row["sender"], row["subject"], row["source_version"]
            ),
            tuple(sorted(scope_ids)),
            int(task["revision"]),
        )

    def complete(self, lease: Lease, work: ClassificationWork, result: Classification) -> None:
        email_ref, task_ref = self._refs(lease, work.metadata.id)

        def save(tx: Transaction) -> None:
            account = self.db.fenced(lease, tx)
            email = self.db.read(email_ref, tx)
            task = self.db.read(task_ref, tx)
            if (
                account["credential_status"] == "revoked"
                or email is None
                or task is None
                or email["owner_id"] != lease.account.owner_id
                or email["account_id"] != lease.account.account_id
                or email["message_id"] != work.metadata.id
                or task["owner_id"] != lease.account.owner_id
                or task["account_id"] != lease.account.account_id
                or task["email_key"] != email_ref.id
                or task["action"] != "classify"
            ):
                raise AccountAccessError()
            if (
                email["source_version"] != work.metadata.version
                or task["state"] != "pending"
                or task["revision"] != work.revision
            ):
                raise LeaseLost()
            tx.update(email_ref, {"classification": result.model_dump(mode="json")})
            tx.update(task_ref, {"state": "done", "revision": work.revision + 1})

        self.db.atomic(save)
