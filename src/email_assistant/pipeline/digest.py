import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from time import monotonic
from typing import Literal, Protocol, Self
from uuid import uuid4
from zoneinfo import ZoneInfo

from google.cloud.firestore_v1 import DocumentReference, FieldFilter, Transaction
from pydantic import BaseModel, ConfigDict
from telegram import InlineKeyboardMarkup

from email_assistant.ai.classification import Classification
from email_assistant.config import Settings
from email_assistant.email.gmail import MAILBOX
from email_assistant.email.provider import ErrorCode, ProviderError, stable_key
from email_assistant.pipeline.classification import ClassificationCoordinator, _clean
from email_assistant.pipeline.sync import SyncCoordinator
from email_assistant.security.account_secrets import CredentialStoreError
from email_assistant.storage.firestore import (
    AccountAccessError,
    AccountKey,
    FirestoreRepository,
    LeaseLost,
    Record,
)
from email_assistant.telegram.delivery import Deliveries, Recipient, SendResult

ZONE = ZoneInfo("Europe/Zurich")
PAGE_SIZE = 5
RETENTION = timedelta(days=30)
LOOKBACK = timedelta(days=7)
type View = Literal["digest", "filtered"]


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    slot: str | None = None

    @classmethod
    def scheduled(cls, at: datetime, slot: str) -> Self:
        if at.utcoffset() is None or slot not in {"06:00", "21:00"}:
            raise ValueError("Invalid planned digest time.")
        local = at.astimezone(ZONE)
        if local.strftime("%H:%M") != slot:
            raise ValueError("Planned digest time does not match its slot.")
        day = local.date()
        previous = day - timedelta(days=1) if slot == "06:00" else day
        start = datetime.combine(previous, time(21 if slot == "06:00" else 6), ZONE)
        end = datetime.combine(day, time(6 if slot == "06:00" else 21), ZONE)
        return cls(start.astimezone(UTC), end.astimezone(UTC), slot)

    @classmethod
    def manual(cls, end: datetime) -> Self:
        return cls(end - timedelta(days=1), end)

    def key(self, recipient: Recipient, manual_id: str | None) -> str:
        if self.slot:
            return stable_key(
                recipient.owner_id, self.end.astimezone(ZONE).date().isoformat(), self.slot
            )
        if manual_id is None:
            raise ValueError("A manual digest requires a stable update ID.")
        return stable_key(recipient.owner_id, str(recipient.chat_id), manual_id)


class DigestEntry(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)
    number: int
    account_id: str
    email_key: str
    subject: str
    sender: str
    summary: str
    late: bool = False


@dataclass(frozen=True)
class DigestRun:
    batch_id: str
    state: str
    pages: int = 0
    emails: int = 0
    retry_after: float = 0
    accounts: int = 0
    pending_accounts: int = 0


class DigestStore:
    def __init__(self, db: FirestoreRepository, *, provider: str = "outlook") -> None:
        self.db = db
        self.provider = provider

    def accounts(self, recipient: Recipient) -> list[AccountKey]:
        query = self.db.client.collection("accounts").where(
            filter=FieldFilter("owner_id", "==", recipient.owner_id)
        )
        return [
            AccountKey(recipient.owner_id, doc.id)
            for doc in query.stream()
            if (doc.to_dict() or {}).get("identity", {}).get("provider") == self.provider
        ]

    def batch(self, recipient: Recipient, batch_id: str) -> Record | None:
        row = self.db.read(self.db.ref("digest_batches", batch_id))
        if (
            row
            and row["owner_id"] == recipient.owner_id
            and row["chat_id"] == str(recipient.chat_id)
            and row.get("snapshot")
            and row["expires_at"] > self.db.clock()
        ):
            return row
        return None

    def page(self, batch: Record, view: View, page: int) -> list[DigestEntry]:
        ref = self.db.ref("number_mappings", stable_key(batch["snapshot"], view, str(page)))
        row = self.db.read(ref)
        return [DigestEntry.model_validate(entry) for entry in row["entries"]] if row else []

    def latest(self, recipient: Recipient) -> Record | None:
        query = self.db.client.collection("digest_batches")
        snapshots = (
            query.where(filter=FieldFilter("owner_id", "==", recipient.owner_id))
            .where(filter=FieldFilter("chat_id", "==", str(recipient.chat_id)))
            .where(filter=FieldFilter("status", "==", "confirmed"))
            .order_by("confirmed_at", direction="DESCENDING")
            .limit(1)
            .stream()
        )
        for snapshot in snapshots:
            return self.batch(recipient, snapshot.id)
        return None

    def message(self, recipient: Recipient, message_id: int) -> Record | None:
        key = stable_key(str(recipient.chat_id), str(message_id))
        row = self.db.read(self.db.ref("telegram_messages", key))
        if not row or (row["owner_id"], row["chat_id"]) != (
            recipient.owner_id,
            str(recipient.chat_id),
        ):
            return None
        return row

    def includes(self, account: AccountKey, email_key: str) -> bool:
        if self.provider != "gmail":
            return True
        membership = self.db.read(
            self.db.ref("scope_memberships", stable_key(email_key, MAILBOX.id))
        )
        return bool(
            membership
            and membership["owner_id"] == account.owner_id
            and membership["account_id"] == account.account_id
        )

    def email(self, recipient: Recipient, entry: DigestEntry) -> tuple[AccountKey, Record]:
        account = AccountKey(recipient.owner_id, entry.account_id)
        registered = self.db.account(account)
        status = registered["credential_status"]
        row = self.db.read(self.db.ref("emails", entry.email_key))
        if (
            status == "revoked"
            or registered.get("identity", {}).get("provider") != self.provider
            or not self.includes(account, entry.email_key)
            or not row
            or row["owner_id"] != recipient.owner_id
            or row["account_id"] != account.account_id
            or stable_key(account.account_id, row["message_id"]) != entry.email_key
        ):
            raise AccountAccessError()
        return account, row

    def scheduled_emails(self, recipient: Recipient, window: Window) -> set[str]:
        query = (
            self.db.client.collection("digest_batches")
            .where(filter=FieldFilter("owner_id", "==", recipient.owner_id))
            .where(filter=FieldFilter("chat_id", "==", str(recipient.chat_id)))
            .where(filter=FieldFilter("window_end", ">", window.start - LOOKBACK))
            .where(filter=FieldFilter("window_end", "<", window.end + LOOKBACK + timedelta(days=1)))
            .order_by("window_end")
        )
        used: set[str] = set()
        for doc in query.stream():
            row = doc.to_dict()
            if not row or not row.get("snapshot") or not row.get("slot"):
                continue
            for view in ("digest", "filtered"):
                for page in range(page_count(row["counts"][view])):
                    used.update(entry.email_key for entry in self.page(row, view, page))
        return used


class PageButtons(Protocol):
    def keyboard(
        self,
        recipient: Recipient,
        batch: Record,
        view: View,
        page: int,
        entry: DigestEntry | None,
        business_key: str,
        segment: int,
        footer_entries: list[DigestEntry] | None,
    ) -> InlineKeyboardMarkup: ...


def page_count(count: int) -> int:
    return max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)


def plain(text: str, limit: int) -> str:
    return " ".join(_clean(text).split())[:limit]


def _page_header(batch: Record, view: View, page: int) -> str:
    label = "邮件摘要" if view == "digest" else "过滤清单"
    end = batch["window_end"].astimezone(ZONE).strftime("%Y-%m-%d %H:%M")
    header = (
        f"{label}｜{end}（苏黎世）\n批次 {batch['batch_id'][:10]} · "
        f"第 {page + 1}/{page_count(batch['counts'][view])} 页"
    )
    if view == "digest" and page == 0:
        accounts = batch["accounts"].values()
        success = sum(status == "完成" for status in accounts)
        partial = sum(status == "有待补收" for status in accounts)
        failed = len(batch["accounts"]) - success - partial
        header += f"\n账户完成 {success} · 待补收 {partial} · 不可用 {failed}"
        header += f"\n过滤 {batch['counts']['filtered']} 封"
    return header


def _entry_title(entry: DigestEntry) -> str:
    late = "［补收］" if entry.late else ""
    return f"{entry.number}. {entry.subject or '（无主题）'}{late}"


def render_page(batch: Record, view: View, page: int, entries: list[DigestEntry]) -> str:
    lines = [f"{_entry_title(entry)}\n{entry.sender}\n{entry.summary}" for entry in entries]
    return "\n\n".join([_page_header(batch, view, page), *(lines or ["本页没有邮件。"])])


class DigestCoordinator:
    def __init__(
        self,
        settings: Settings,
        store: DigestStore,
        sync: SyncCoordinator,
        classify: ClassificationCoordinator,
        deliveries: Deliveries,
        buttons: PageButtons | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.sync = sync
        self.classify = classify
        self.deliveries = deliveries
        self.buttons = buttons

    async def run(
        self,
        recipient: Recipient,
        window: Window,
        *,
        manual_id: str | None = None,
    ) -> DigestRun:
        if not recipient.allowed(self.settings):
            raise AccountAccessError()
        batch_id = window.key(recipient, manual_id)
        db = self.store.db
        ref = db.ref("digest_batches", batch_id)
        lock_id = (
            stable_key("scheduled", recipient.owner_id, str(recipient.chat_id))
            if window.slot
            else batch_id
        )
        lock = db.ref("digest_leases", lock_id)
        deadline = monotonic() + self.settings.work_budget_seconds
        token = uuid4().hex

        def claim(tx: Transaction) -> Record | None:
            old = db.read(ref, tx)
            held = db.read(lock, tx)
            if old and (old["owner_id"], old["chat_id"]) != (
                recipient.owner_id,
                str(recipient.chat_id),
            ):
                raise AccountAccessError()
            if held and held["until"] > db.clock():
                return None
            tx.set(
                lock,
                {
                    "token": token,
                    "until": db.clock() + timedelta(seconds=self.settings.hard_timeout_seconds),
                },
            )
            return old or {}

        def release(tx: Transaction) -> None:
            held = db.read(lock, tx)
            if held and held["token"] == token:
                tx.delete(lock)

        old = db.atomic(claim)
        if old is None:
            return DigestRun(batch_id, "busy", retry_after=2)
        try:
            if old.get("status") in {"confirmed", "failed", "unknown"}:
                return DigestRun(batch_id, old["status"])
            pages = 0
            if not old.get("snapshot"):
                attempt = old.get("build_attempt", 0) + 1
                old, pages = await self._build(
                    recipient, window, batch_id, token, deadline, attempt
                )
                self._seal(
                    lock,
                    token,
                    ref,
                    old
                    if old.get("snapshot")
                    else {
                        "owner_id": recipient.owner_id,
                        "chat_id": str(recipient.chat_id),
                        "window_start": window.start,
                        "window_end": window.end,
                        "slot": window.slot,
                        "build_attempt": attempt,
                    },
                )
            if not old.get("snapshot") or (window.slot and monotonic() >= deadline):
                result = SendResult("retry", retry_after=5)
            else:
                result = await self.send_page(recipient, old, "digest", 0, batch_id)
            if result.state in {"confirmed", "failed", "unknown"}:
                values: Record = {"status": result.state}
                if result.state == "confirmed":
                    values["confirmed_at"] = db.clock()
                db.atomic(lambda tx: self._finish_batch(tx, lock, token, ref, values))
            return DigestRun(
                batch_id,
                result.state,
                pages,
                sum(old["counts"].values()),
                result.retry_after,
                accounts=len(old["accounts"]),
                pending_accounts=list(old["accounts"].values()).count("有待补收"),
            )
        finally:
            db.atomic(release)

    def _finish_batch(
        self,
        tx: Transaction,
        lock: DocumentReference,
        token: str,
        ref: DocumentReference,
        values: Record,
    ) -> None:
        held = self.store.db.read(lock, tx)
        if held and held["token"] == token and held["until"] > self.store.db.clock():
            tx.update(ref, values)

    def _seal(
        self,
        lock: DocumentReference,
        token: str,
        ref: DocumentReference,
        record: Record,
    ) -> None:
        db = self.store.db

        def save(tx: Transaction) -> None:
            held = db.read(lock, tx)
            if not held or held["token"] != token or held["until"] <= db.clock():
                raise LeaseLost()
            tx.set(ref, record)

        db.atomic(save)

    async def _build(
        self,
        recipient: Recipient,
        window: Window,
        batch_id: str,
        snapshot: str,
        deadline: float,
        attempt: int,
    ) -> tuple[Record, int]:
        db = self.store.db
        statuses = {}
        views: dict[View, list[tuple[datetime, DigestEntry]]] = {"digest": [], "filtered": []}
        start = window.start - LOOKBACK if window.slot else window.start
        seen = self.store.scheduled_emails(recipient, window) if window.slot else set()
        pages = 0
        for account in self.store.accounts(recipient):
            if monotonic() >= deadline:
                statuses[account.account_id] = "有待补收"
                continue
            try:
                self.sync.store.request(account, batch_id)
                result = await asyncio.to_thread(
                    self.sync.run, account, budget_seconds=max(1, int(deadline - monotonic()))
                )
                statuses[account.account_id] = "有待补收" if result.pending else "完成"
                pages += result.pages
            except ProviderError as exc:
                statuses[account.account_id] = (
                    "有待补收"
                    if exc.code in {ErrorCode.THROTTLED, ErrorCode.TEMPORARY}
                    else "不可用"
                )
                if statuses[account.account_id] == "不可用":
                    continue
            except (CredentialStoreError, LeaseLost, AccountAccessError):
                statuses[account.account_id] = "不可用"
                continue
            query = db.client.collection("emails").where(
                filter=FieldFilter("owner_id", "==", recipient.owner_id)
            )
            query = query.where(filter=FieldFilter("account_id", "==", account.account_id))
            query = query.where(filter=FieldFilter("received_at", ">=", start))
            query = query.where(filter=FieldFilter("received_at", "<", window.end))
            for doc in query.order_by("received_at", direction="DESCENDING").stream():
                if monotonic() >= deadline:
                    statuses[account.account_id] = "有待补收"
                    break
                row = doc.to_dict()
                if not row or doc.id in seen or not self.store.includes(account, doc.id):
                    continue
                try:
                    classification = (
                        Classification.model_validate(row["classification"])
                        if "classification" in row
                        else await self.classify.run(account, row["message_id"], deadline=deadline)
                    )
                except AccountAccessError:
                    statuses[account.account_id] = "不可用"
                    break
                if classification is None:
                    statuses[account.account_id] = "有待补收"
                    continue
                view: View = "filtered" if classification.status == "filtered" else "digest"
                summary = classification.summary_zh or classification.reason
                if classification.needs_review and classification.summary_zh:
                    summary = f"［待复核］{summary}"
                entry = DigestEntry(
                    number=0,
                    account_id=account.account_id,
                    email_key=doc.id,
                    subject=plain(row["subject"], 95),
                    sender=plain(row["sender"], 60),
                    summary=plain(summary, 190),
                    late=row["received_at"] < window.start,
                )
                views[view].append((row["received_at"], entry))

        counts = {view: len(rows) for view, rows in views.items()}
        if "有待补收" in statuses.values() and (
            attempt < 3 or (window.slot and monotonic() >= deadline)
        ):
            return {"accounts": statuses, "counts": counts}, pages

        expires = db.clock() + RETENTION
        for view, rows in views.items():
            rows.sort(key=lambda row: (row[0], row[1].email_key))
            for offset in range(0, len(rows), PAGE_SIZE):
                page = offset // PAGE_SIZE
                entries = [
                    entry.model_copy(update={"number": number}).model_dump()
                    for number, (_, entry) in enumerate(
                        rows[offset : offset + PAGE_SIZE], offset + 1
                    )
                ]
                db.ref("number_mappings", stable_key(snapshot, view, str(page))).set(
                    {
                        "owner_id": recipient.owner_id,
                        "chat_id": str(recipient.chat_id),
                        "batch_id": batch_id,
                        "view": view,
                        "page": page,
                        "entries": entries,
                        "expires_at": expires,
                    }
                )
        return {
            "batch_id": batch_id,
            "owner_id": recipient.owner_id,
            "chat_id": str(recipient.chat_id),
            "snapshot": snapshot,
            "status": "ready",
            "slot": window.slot,
            "accounts": statuses,
            "counts": counts,
            "window_start": window.start,
            "window_end": window.end,
            "expires_at": expires,
            "created_at": db.clock(),
        }, pages

    async def send_page(
        self,
        recipient: Recipient,
        batch: Record,
        view: View,
        page: int,
        business_key: str,
    ) -> SendResult:
        if outcome := self.deliveries.outcome(recipient, business_key):
            return outcome
        if self.buttons is not None:
            # Older releases used segment zero for the whole page. An in-flight
            # legacy send may already have delivered all entries.
            db = self.store.db
            legacy = db.read(db.ref("deliveries", Deliveries.key(recipient, business_key, 0)))
            if legacy and legacy["state"] == "sending":
                delay = (legacy["next_check_at"] - db.clock()).total_seconds()
                return SendResult("busy", retry_after=delay) if delay > 0 else SendResult("unknown")
        visible = []
        # Preserve a message's segment across retries even if another entry loses access.
        for segment, entry in enumerate(self.store.page(batch, view, page), start=1):
            try:
                self.store.email(recipient, entry)
            except AccountAccessError:
                continue
            visible.append((segment, entry))
        entries = [entry for _, entry in visible]
        if self.buttons is None:
            return await self.deliveries.send(
                recipient,
                business_key,
                0,
                render_page(batch, view, page, entries),
                batch_id=batch["batch_id"],
                view=view,
            )

        segments: list[tuple[int, DigestEntry | None]] = [(n, entry) for n, entry in visible]
        if not segments:
            segments.append((PAGE_SIZE + 1, None))
        result = SendResult("confirmed")
        for index, (segment, item) in enumerate(segments):
            if previous := self.deliveries.outcome(recipient, business_key, segment):
                result = previous
            else:
                text = (
                    f"{_entry_title(item)}\n{item.sender}\n{item.summary}"
                    if item
                    else "本页没有邮件。"
                )
                if index == 0:
                    text = f"{_page_header(batch, view, page)}\n\n{text}"
                keyboard = self.buttons.keyboard(
                    recipient,
                    batch,
                    view,
                    page,
                    item,
                    business_key,
                    segment,
                    entries if index == len(segments) - 1 else None,
                )
                result = await self.deliveries.send(
                    recipient,
                    business_key,
                    segment,
                    text,
                    batch_id=batch["batch_id"],
                    keyboard=keyboard,
                    view=view,
                )
            if result.state != "confirmed":
                return result
        return result
