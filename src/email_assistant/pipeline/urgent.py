import asyncio
import logging
from datetime import timedelta
from time import monotonic

from google.cloud.firestore_v1 import DELETE_FIELD, DocumentReference, FieldFilter, Transaction

from email_assistant.config import Settings
from email_assistant.email.provider import ErrorCode, ProviderError
from email_assistant.pipeline.digest import DigestCoordinator, plain
from email_assistant.pipeline.subscriptions import SubscriptionMaintenance
from email_assistant.security.account_secrets import CredentialStoreError
from email_assistant.security.secrets import SecretReader
from email_assistant.storage.firestore import AccountAccessError, AccountKey, Lease, LeaseLost
from email_assistant.tasks.telegram import TaskLease
from email_assistant.telegram.delivery import Recipient, urgent_key

logger = logging.getLogger("uvicorn.error.mailpilot.email")
_MAX_ALERT_AGE = timedelta(days=1)
_PAGE_SIZE = 24


class UrgentWorker:
    def __init__(
        self, settings: Settings, digest: DigestCoordinator, secrets: SecretReader
    ) -> None:
        self.settings = settings
        self.digest = digest
        self.maintenance = SubscriptionMaintenance(settings, digest.sync, secrets)

    async def run(self, task: TaskLease) -> float:
        db = self.digest.store.db
        row = task.row
        account = AccountKey(row["owner_id"], row["account_id"])
        try:
            user = int(account.owner_id)
        except ValueError:
            return 0
        recipient = Recipient(user, user)
        if recipient.owner_id != account.owner_id or not recipient.allowed(self.settings):
            return 0

        deadline = monotonic() + self.settings.work_budget_seconds
        held = None
        try:
            current = db.account(account)
            if (
                current["credential_status"] != "active"
                or current["identity"]["provider"] != self.settings.email_provider
            ):
                return 0
            held = db.acquire(
                account, "urgent", timedelta(seconds=self.settings.hard_timeout_seconds)
            )
            if held is None:
                return 5
            logger.info(
                "email_wake task=%s provider=%s channel=%s received_at=%s started_at=%s",
                task.task_id[:16],
                self.settings.email_provider,
                row["channel"],
                row["created_at"],
                db.clock(),
            )
            self.digest.sync.store.request(account, task.task_id)
            # A notification is only a wake-up. Provider checkpoints govern all reads.
            # Process completed pages even when further sync pages are waiting.
            sync_delay = 0.0
            try:
                result = await asyncio.to_thread(
                    self.digest.sync.run,
                    account,
                    budget_seconds=max(1, min(75, int(deadline - monotonic()))),
                )
                if result.pending:
                    sync_delay = 5
            except ProviderError as exc:
                logger.warning(
                    "email_sync_failure provider=%s code=%s",
                    self.settings.email_provider,
                    exc.code.value,
                )
                if exc.code in {
                    ErrorCode.THROTTLED,
                    ErrorCode.TEMPORARY,
                    ErrorCode.INVALID_RESPONSE,
                }:
                    sync_delay = max(5, exc.retry_after or 0)

            if monotonic() < deadline:
                try:
                    await asyncio.to_thread(self.maintenance.run, held, row)
                except CredentialStoreError:
                    logger.warning(
                        "email_subscription_failure provider=%s code=credentials",
                        self.settings.email_provider,
                    )
            delay = await self._alerts(held, recipient, task, deadline, bool(sync_delay))
            return min(sync_delay, delay) if sync_delay and delay else sync_delay or delay
        except AccountAccessError:
            return 0
        except (CredentialStoreError, LeaseLost):
            return 5
        finally:
            if held is not None:
                db.release(held)

    async def _alerts(
        self,
        lease: Lease,
        recipient: Recipient,
        task: TaskLease,
        deadline: float,
        sync_pending: bool,
    ) -> float:
        db = self.digest.store.db
        account = lease.account
        query = db.client.collection("emails")
        candidates = (
            query.where(filter=FieldFilter("owner_id", "==", account.owner_id))
            .where(filter=FieldFilter("account_id", "==", account.account_id))
            .where(filter=FieldFilter("urgent_check_at", "<=", db.clock()))
            .order_by("urgent_check_at")
            .limit(_PAGE_SIZE)
        )

        def checkpoint(ref: DocumentReference, wait: float = 0) -> None:
            def update(tx: Transaction) -> None:
                db.fenced(lease, tx)
                tx.update(
                    ref,
                    {
                        "urgent_check_at": db.clock() + timedelta(seconds=wait)
                        if wait
                        else DELETE_FIELD,
                    },
                )

            db.atomic(update)

        delay = 0.0
        rows = list(candidates.stream())
        for doc in rows:
            if monotonic() >= deadline:
                return 1
            key = urgent_key(doc.id)
            if self.digest.deliveries.outcome(recipient, key):
                checkpoint(doc.reference)
                continue
            row = doc.to_dict() or {}
            received = row["received_at"]
            try:
                if (
                    db.clock() - received >= _MAX_ALERT_AGE
                    or received < db.account(account)["registered_at"]
                ):
                    checkpoint(doc.reference)
                    continue
                self.digest.sync.store.indexed_message(account, row["message_id"])
                memberships = db.client.collection("scope_memberships").where(
                    filter=FieldFilter("email_key", "==", doc.id)
                )
                present = any(
                    (membership := snapshot.to_dict()) is not None
                    and membership["owner_id"] == account.owner_id
                    and membership["account_id"] == account.account_id
                    for snapshot in memberships.stream()
                )
                if not present and not sync_pending:
                    checkpoint(doc.reference)
                    continue
                classification = (
                    await self.digest.classify.run(account, row["message_id"], deadline=deadline)
                    if present
                    else None
                )
            except AccountAccessError:
                checkpoint(doc.reference)
                continue
            if classification is None:
                wait = 5.0
            elif classification.status == "filtered" or classification.importance != "urgent":
                checkpoint(doc.reference)
                continue
            else:
                if db.account(account)["credential_status"] != "active":
                    return 0
                classified_at = db.clock()
                result = await self.digest.deliveries.send(
                    recipient,
                    key,
                    0,
                    f"紧急邮件\n{plain(row['subject'], 150)}\n"
                    f"{plain(row['sender'], 100)}\n"
                    f"{plain(classification.summary_zh or classification.reason, 400)}",
                )
                wait = max(1, result.retry_after) if result.state in {"retry", "busy"} else 0
                logger.info(
                    "email_urgent_result task=%s email=%s provider=%s channel=%s state=%s "
                    "classified_at=%s finished_at=%s latency_seconds=%.3f",
                    task.task_id[:16],
                    doc.id[:16],
                    self.settings.email_provider,
                    task.row["channel"],
                    result.state,
                    classified_at,
                    db.clock(),
                    (db.clock() - received).total_seconds(),
                )
            if wait:
                checkpoint(doc.reference, wait)
                delay = min(delay or wait, wait)
            else:
                checkpoint(doc.reference)
        return min(delay or 1, 1) if len(rows) == _PAGE_SIZE else delay
