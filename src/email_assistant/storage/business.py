from collections.abc import Collection
from typing import ClassVar, Literal

from google.cloud.firestore_v1 import DocumentReference, FieldFilter, Transaction
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from email_assistant.ai.classification import Importance
from email_assistant.email.provider import stable_key
from email_assistant.storage.firestore import AccountAccessError, AccountKey, FirestoreRepository


class OwnedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)

    collection: ClassVar[str]
    owner_id: str = Field(min_length=1)
    account_id: str | None = None


class RulePredicate(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)

    field: Literal["sender", "subject", "scope"]
    operator: Literal["equals", "contains", "ends_with"]
    value: str = Field(min_length=1, max_length=1024)


class RuleAction(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)

    visible: bool | None = None
    category: str | None = Field(default=None, max_length=128)
    importance: Importance | None = None
    protect: bool = False


class Rule(OwnedRecord):
    collection = "rules"
    enabled: bool
    scope_id: str | None = None
    priority: int
    override: bool = False
    predicate: RulePredicate
    action: RuleAction
    updated_by: str


class DigestBatch(OwnedRecord):
    collection = "digest_batches"
    chat_id: str
    window_start: AwareDatetime
    window_end: AwareDatetime
    status: str = Field(max_length=48)
    telegram_message_ids: tuple[int, ...] = ()


class NumberMapping(OwnedRecord):
    collection = "number_mappings"
    chat_id: str
    batch_id: str
    view: Literal["digest", "filtered"]
    number: int = Field(gt=0)
    email_key: str
    expires_at: AwareDatetime
    account_id: str


class Subscription(OwnedRecord):
    collection = "subscriptions"
    account_id: str
    provider_subscription_id: str
    expires_at: AwareDatetime
    state: str = Field(max_length=48)


class TaskRecord(OwnedRecord):
    collection = "tasks"
    action: str = Field(max_length=48)
    state: str = Field(max_length=48)
    account_id: str
    email_key: str | None = None
    next_check_at: AwareDatetime
    historical: bool = False
    attempts: int = Field(default=0, ge=0)


class Delivery(OwnedRecord):
    collection = "deliveries"
    chat_id: str
    segment: int = Field(ge=0)
    state: str = Field(max_length=48)
    telegram_message_id: int | None = None
    checked_at: AwareDatetime | None = None


class RecordConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Business record revision no longer matches.")


class BusinessRepository:
    def __init__(self, db: FirestoreRepository) -> None:
        self.db = db

    def ref(
        self, record_type: type[OwnedRecord], owner_id: str, account_id: str | None, key: str
    ) -> DocumentReference:
        return self.db.ref(record_type.collection, stable_key(owner_id, account_id or "", key))

    def _authorize(
        self, owner_id: str, account_id: str | None, tx: Transaction | None = None
    ) -> None:
        if not owner_id:
            raise AccountAccessError()
        if account_id is not None:
            self.db.account(AccountKey(owner_id, account_id), tx)

    def put(self, key: str, record: OwnedRecord, *, expected_revision: int | None = None) -> int:
        """Create a record or replace it under the caller's last observed revision."""
        ref = self.ref(type(record), record.owner_id, record.account_id, key)

        def save(tx: Transaction) -> int:
            self._authorize(record.owner_id, record.account_id, tx)
            previous = self.db.read(ref, tx)
            if previous and (previous["owner_id"], previous.get("account_id")) != (
                record.owner_id,
                record.account_id,
            ):
                raise AccountAccessError()
            revision = int(previous["revision"]) if previous else None
            if revision != expected_revision:
                raise RecordConflict()
            if isinstance(record, NumberMapping):
                batch = self.db.read(
                    self.ref(DigestBatch, record.owner_id, None, record.batch_id), tx
                )
                email = self.db.read(self.db.ref("emails", record.email_key), tx)
                if (
                    batch is None
                    or batch["owner_id"] != record.owner_id
                    or batch["chat_id"] != record.chat_id
                    or email is None
                    or email["owner_id"] != record.owner_id
                    or email["account_id"] != record.account_id
                ):
                    raise AccountAccessError()
            updated = (revision or 0) + 1
            tx.set(ref, {**record.model_dump(), "revision": updated})
            return updated

        return self.db.atomic(save)

    def list_rules(self, account: AccountKey) -> tuple[tuple[str, Rule], ...]:
        self.db.account(account)
        return self.owner_rules(account.owner_id, (account.account_id,))

    def owner_rules(
        self,
        owner_id: str,
        account_ids: Collection[str],
    ) -> tuple[tuple[str, Rule], ...]:
        if not owner_id:
            raise AccountAccessError()
        query = self.db.client.collection(Rule.collection).where(
            filter=FieldFilter("owner_id", "==", owner_id)
        )
        rules = []
        for snapshot in query.stream():
            row = snapshot.to_dict()
            if row is not None and (
                row.get("account_id") is None or row["account_id"] in account_ids
            ):
                row.pop("revision", None)
                rules.append((snapshot.id, Rule.model_validate(row)))
        return tuple(rules)

    def get[T: OwnedRecord](
        self, record_type: type[T], owner_id: str, key: str, *, account_id: str | None = None
    ) -> tuple[T, int] | None:
        self._authorize(owner_id, account_id)
        row = self.db.read(self.ref(record_type, owner_id, account_id, key))
        if row is None:
            return None
        if (row["owner_id"], row.get("account_id")) != (owner_id, account_id):
            raise AccountAccessError()
        revision = int(row.pop("revision"))
        record = record_type.model_validate(row)
        if isinstance(record, NumberMapping) and record.expires_at <= self.db.clock():
            return None
        return record, revision
