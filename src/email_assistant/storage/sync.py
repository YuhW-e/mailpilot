from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from itertools import batched
from typing import Literal, cast

from google.cloud.firestore_v1 import DocumentReference, Query, Transaction
from google.cloud.firestore_v1.base_query import FieldFilter

from email_assistant.email.provider import Change, Discovery, Scope, SyncPage, stable_key
from email_assistant.storage.business import BusinessRepository, TaskRecord
from email_assistant.storage.firestore import (
    AccountAccessError,
    AccountKey,
    FirestoreRepository,
    Lease,
    LeaseLost,
    Record,
)

CHUNK_SIZE = 75


@dataclass(frozen=True)
class Checkpoint:
    scope: Scope
    data: Record = field(repr=False)

    @property
    def continuation(self) -> str | None:
        return cast(str | None, self.data.get("next_page") or self.data.get("cursor"))

    @property
    def reconciling(self) -> bool:
        return bool(self.data.get("rebuild_candidate"))


@dataclass(frozen=True)
class ScopeStatus:
    scope: Scope
    availability: Literal["active", "inaccessible", "missing"]
    checked_since: datetime | None
    coverage_gap: tuple[datetime, datetime] | None


class SyncRepository:
    def __init__(self, db: FirestoreRepository, *, urgent_enabled: bool = False) -> None:
        self.db = db
        self.urgent_enabled = urgent_enabled
        self._business = BusinessRepository(db)

    def _account_ref(self, account: AccountKey) -> DocumentReference:
        return self.db.ref("accounts", account.account_id)

    def _scope_ref(self, account: AccountKey, scope: Scope) -> DocumentReference:
        return self.db.ref("cursors", stable_key(account.account_id, scope.id))

    def _owned(self, collection: str, account: AccountKey) -> Query:
        return (
            self.db.client.collection(collection)
            .where(filter=FieldFilter("owner_id", "==", account.owner_id))
            .where(filter=FieldFilter("account_id", "==", account.account_id))
        )

    def request(self, account: AccountKey, event_id: str | None = None) -> int:
        def wake(tx: Transaction) -> int:
            row = self.db.account(account, tx)
            current = int(row["wake_generation"])
            if event_id is not None:
                ref = self.db.ref("sync_wakeups", stable_key(account.account_id, event_id))
                previous = self.db.read(ref, tx)
                now = self.db.clock()
                if previous is not None and previous["expires_at"] > now:
                    return current
                tx.set(
                    ref,
                    {
                        "owner_id": account.owner_id,
                        "account_id": account.account_id,
                        "expires_at": now + timedelta(days=7),
                    },
                )
            tx.update(self._account_ref(account), {"wake_generation": current + 1})
            return current + 1

        return self.db.atomic(wake)

    def _active(self, lease: Lease, tx: Transaction) -> Record:
        if lease.purpose != "sync":
            raise LeaseLost()
        row = self.db.fenced(lease, tx)
        if row["credential_status"] != "active":
            raise AccountAccessError()
        return row

    def postpone(self, lease: Lease, delay_seconds: float) -> None:
        retry_at = self.db.clock() + timedelta(seconds=delay_seconds)

        def save(tx: Transaction) -> None:
            row = self.db.fenced(lease, tx)
            previous = row.get("sync_retry_at")
            if previous is None or previous < retry_at:
                tx.update(self._account_ref(lease.account), {"sync_retry_at": retry_at})

        self.db.atomic(save)

    def begin(self, lease: Lease) -> tuple[int, bool] | None:
        def begin(tx: Transaction) -> tuple[int, bool] | None:
            row = self._active(lease, tx)
            active = row.get("active_generation")
            if active is not None:
                return int(active), row.get("discovered_generation") == active
            if row["wake_generation"] == row["completed_generation"]:
                return None
            generation = int(row["wake_generation"])
            tx.update(self._account_ref(lease.account), {"active_generation": generation})
            return generation, False

        return self.db.atomic(begin)

    def discover(
        self,
        lease: Lease,
        generation: int,
        discovery: Discovery,
        start: datetime | None,
        end: datetime | None,
    ) -> None:
        account = lease.account
        present = {scope.id: scope for scope in discovery.scopes}
        inaccessible = set(discovery.inaccessible)
        for snapshot in self._owned("cursors", account).stream():
            existing = snapshot.to_dict()
            if existing:
                folder = existing["scope_id"]
                present.setdefault(folder, Scope(folder))
        found = {scope.id for scope in discovery.scopes}

        def register(scopes: tuple[Scope, ...], tx: Transaction) -> None:
            self._round(lease, tx, generation)
            refs = [self._scope_ref(account, scope) for scope in scopes]
            previous = [self.db.read(ref, tx) for ref in refs]
            now = self.db.clock()
            for scope, ref, row in zip(scopes, refs, previous, strict=True):
                observed = scope.id in found
                status = "active" if observed else "missing"
                if scope.id in inaccessible or (not observed and inaccessible):
                    status = "inaccessible"
                # Accessible messages of a folder can still be read when its children cannot.
                fields = {
                    "pending_generation": generation if observed else 0,
                    "availability": status,
                }
                if row is None:
                    tx.create(
                        ref,
                        {
                            **fields,
                            "owner_id": account.owner_id,
                            "account_id": account.account_id,
                            "scope_id": scope.id,
                            "revision": 0,
                            "scan": 1,
                            "last_turn": 0,
                            "bootstrap_at": now,
                            "window_start": start or now - timedelta(days=7),
                            "window_end": end,
                        },
                    )
                else:
                    tx.update(ref, fields)

        for group in batched(present.values(), CHUNK_SIZE):
            self.db.atomic(partial(register, group))

        def finish_discovery(tx: Transaction) -> None:
            self._round(lease, tx, generation)
            tx.update(self._account_ref(account), {"discovered_generation": generation})

        self.db.atomic(finish_discovery)

    def _round(self, lease: Lease, tx: Transaction, generation: int) -> Record:
        row = self._active(lease, tx)
        if row.get("active_generation") != generation:
            raise LeaseLost()
        return row

    def _pending(self, account: AccountKey, generation: int) -> Query:
        return self._owned("cursors", account).where(
            filter=FieldFilter("pending_generation", "==", generation)
        )

    def next_scope(self, lease: Lease, generation: int) -> Checkpoint | None:
        query = self._pending(lease.account, generation).order_by("last_turn").limit(1)
        for snapshot in query.stream():
            row = snapshot.to_dict()
            if row is not None:
                return Checkpoint(Scope(row["scope_id"]), row)
        return None

    def _checked(
        self, lease: Lease, tx: Transaction, generation: int, checkpoint: Checkpoint
    ) -> tuple[DocumentReference, Record, Record]:
        account = self._round(lease, tx, generation)
        ref = self._scope_ref(lease.account, checkpoint.scope)
        row = self.db.read(ref, tx)
        if (
            row is None
            or row["revision"] != checkpoint.data["revision"]
            or row["pending_generation"] != generation
        ):
            raise LeaseLost()
        return ref, row, account

    def _advance(
        self, lease: Lease, generation: int, checkpoint: Checkpoint, changes: Record
    ) -> None:
        def advance(tx: Transaction) -> None:
            ref, row, account = self._checked(lease, tx, generation, checkpoint)
            turn = account.get("sync_turn", 0) + 1
            tx.update(ref, {**changes, "revision": row["revision"] + 1, "last_turn": turn})
            tx.update(self._account_ref(lease.account), {"sync_turn": turn})

        self.db.atomic(advance)

    def reset(self, lease: Lease, generation: int, checkpoint: Checkpoint) -> None:
        row = checkpoint.data
        now = self.db.clock()
        recovered_from = row.get("last_success")
        if not recovered_from:
            self._advance(
                lease,
                generation,
                checkpoint,
                {
                    "scan": row["scan"] + 1,
                    "scan_started_at": None,
                    "cursor": None,
                    "next_page": None,
                    "recovery_start": row["window_start"],
                },
            )
            return
        wanted = recovered_from - timedelta(days=1)
        start = max(wanted, now - timedelta(days=14))
        # Preserve the original recovery range when a rebuild's continuation itself expires.
        start = row.get("recovery_start") or start
        gap = row.get("coverage_gap")
        if start > wanted:
            gap = {
                "start": min(wanted, gap["start"]) if gap else wanted,
                "end": max(start, gap["end"]) if gap else start,
            }
        self._advance(
            lease,
            generation,
            checkpoint,
            {
                "scan": row["scan"] + 1,
                "scan_started_at": None,
                "cursor": None,
                "next_page": None,
                "rebuild_candidate": None,
                "recovery_start": start,
                "coverage_gap": gap,
            },
        )

    def defer_scope(self, lease: Lease, generation: int, checkpoint: Checkpoint) -> None:
        self._advance(
            lease,
            generation,
            checkpoint,
            {
                "availability": "inaccessible",
                "pending_generation": 0,
            },
        )

    def _write_changes(
        self,
        lease: Lease,
        generation: int,
        checkpoint: Checkpoint,
        changes: tuple[Change, ...],
    ) -> None:
        account, scope = lease.account, checkpoint.scope

        def write(tx: Transaction) -> None:
            _, row, _ = self._checked(lease, tx, generation, checkpoint)
            selected: list[
                tuple[Change, DocumentReference, DocumentReference, DocumentReference]
            ] = []
            for change in changes:
                message = change.message
                if message is not None:
                    if message.id != change.id:
                        raise ValueError("Mismatched message change.")
                    if row.get("recovery_start"):
                        if message.received_at < row["recovery_start"]:
                            continue
                    if not row.get("last_success"):
                        end = row.get("window_end")
                        if message.received_at < row["window_start"] or (
                            end and end <= message.received_at < row["bootstrap_at"]
                        ):
                            continue
                key = stable_key(account.account_id, change.id)
                selected.append(
                    (
                        change,
                        self.db.ref("scope_memberships", stable_key(key, scope.id)),
                        self.db.ref("emails", key),
                        self._business.ref(
                            TaskRecord,
                            account.owner_id,
                            account.account_id,
                            stable_key("classify", key),
                        ),
                    )
                )
            stored = [
                self.db.read(email_ref, tx) if change.message else None
                for change, _, email_ref, _ in selected
            ]
            owned = {"owner_id": account.owner_id, "account_id": account.account_id}
            for (change, member_ref, email_ref, task_ref), existing in zip(
                selected, stored, strict=True
            ):
                message = change.message
                if message is None:
                    tx.delete(member_ref)
                    continue
                tx.set(
                    member_ref,
                    {
                        **owned,
                        "scope_id": scope.id,
                        "email_key": email_ref.id,
                        "received_at": message.received_at,
                        "scan": row["scan"],
                    },
                )
                historical = not row.get("last_success") and (
                    message.received_at < row["bootstrap_at"]
                )
                tx.set(
                    email_ref,
                    {
                        **owned,
                        "message_id": message.id,
                        "received_at": message.received_at,
                        "sender": message.sender,
                        "subject": message.subject,
                        "source_version": message.version,
                        **(
                            {"urgent_check_at": self.db.clock()}
                            if self.urgent_enabled and existing is None and not historical
                            else {}
                        ),
                    },
                    merge=True,
                )
                if existing is None:
                    tx.create(
                        task_ref,
                        {
                            **owned,
                            "email_key": email_ref.id,
                            "action": "classify",
                            "revision": 1,
                            "state": "pending",
                            "next_check_at": self.db.clock(),
                            "historical": historical,
                        },
                    )

        self.db.atomic(write)

    def apply_page(
        self,
        lease: Lease,
        generation: int,
        checkpoint: Checkpoint,
        page: SyncPage,
        *,
        check_budget: Callable[[], None],
        request_started_at: datetime | None = None,
    ) -> None:
        # Delta can repeat an ID in a page. The last occurrence applies to this scope.
        changes = {change.id: change for change in page.changes}
        for group in batched(changes.values(), CHUNK_SIZE):
            check_budget()
            self._write_changes(lease, generation, checkpoint, group)
        check_budget()
        started = checkpoint.data.get("scan_started_at") or request_started_at or self.db.clock()
        if page.next_page:
            self._advance(
                lease,
                generation,
                checkpoint,
                {
                    "next_page": page.next_page,
                    "scan_started_at": started,
                },
            )
        elif checkpoint.data.get("recovery_start"):
            self._advance(
                lease,
                generation,
                checkpoint,
                {
                    "next_page": None,
                    "rebuild_candidate": page.cursor,
                    "scan_started_at": started,
                },
            )
        else:
            self._complete_scope(lease, generation, checkpoint, page.cursor, started)

    def _complete_scope(
        self,
        lease: Lease,
        generation: int,
        checkpoint: Checkpoint,
        cursor: str | None,
        started: datetime,
    ) -> None:
        self._advance(
            lease,
            generation,
            checkpoint,
            {
                "cursor": cursor,
                "next_page": None,
                "rebuild_candidate": None,
                "recovery_start": None,
                "scan_started_at": None,
                "last_success": started,
                "pending_generation": 0,
            },
        )

    def reconcile(self, lease: Lease, generation: int, checkpoint: Checkpoint) -> None:
        row = checkpoint.data
        query = (
            self._owned("scope_memberships", lease.account)
            .where(filter=FieldFilter("scope_id", "==", checkpoint.scope.id))
            .where(filter=FieldFilter("received_at", ">=", row["recovery_start"]))
            .where(filter=FieldFilter("scan", "<", row["scan"]))
            .order_by("received_at")
            .order_by("scan")
            .limit(CHUNK_SIZE)
        )

        def delete_stale(tx: Transaction) -> bool:
            self._checked(lease, tx, generation, checkpoint)
            stale = list(query.stream(transaction=tx))
            for member in stale:
                tx.delete(member.reference)
            return bool(stale)

        if not self.db.atomic(delete_stale):
            self._complete_scope(
                lease, generation, checkpoint, row["rebuild_candidate"], row["scan_started_at"]
            )
        else:
            self._advance(lease, generation, checkpoint, {})

    def finish(self, lease: Lease, generation: int) -> bool:
        def complete(tx: Transaction) -> bool:
            row = self._round(lease, tx, generation)
            if row.get("discovered_generation") != generation:
                raise LeaseLost()
            if self._pending(lease.account, generation).limit(1).get(transaction=tx):
                raise LeaseLost()
            tx.update(
                self._account_ref(lease.account),
                {
                    "active_generation": None,
                    "completed_generation": generation,
                },
            )
            return bool(row["wake_generation"] > generation)

        return self.db.atomic(complete)

    def scope_statuses(self, account: AccountKey) -> tuple[ScopeStatus, ...]:
        self.db.account(account)
        statuses: list[ScopeStatus] = []
        for snapshot in self._owned("cursors", account).stream():
            row = snapshot.to_dict()
            if row is None:
                continue
            gap = row.get("coverage_gap")
            statuses.append(
                ScopeStatus(
                    Scope(row["scope_id"]),
                    row["availability"],
                    row.get("last_success"),
                    (gap["start"], gap["end"]) if gap else None,
                )
            )
        return tuple(statuses)

    def indexed_message(self, account: AccountKey, message_id: str) -> Record:
        self.db.account(account)
        row = self.db.read(self.db.ref("emails", stable_key(account.account_id, message_id)))
        if (
            row is None
            or row["owner_id"] != account.owner_id
            or row["account_id"] != account.account_id
            or row["message_id"] != message_id
        ):
            raise AccountAccessError()
        return row
