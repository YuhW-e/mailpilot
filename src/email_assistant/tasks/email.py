from datetime import datetime, timedelta
from typing import Literal

from google.cloud.firestore_v1 import Transaction

from email_assistant.config import Settings
from email_assistant.email.provider import stable_key
from email_assistant.pipeline.digest import DigestStore
from email_assistant.storage.firestore import AccountKey
from email_assistant.tasks.telegram import TaskDispatch
from email_assistant.telegram.delivery import Recipient

type WakeSource = Literal["graph", "gmail", "poll"]
type Lifecycle = Literal["subscriptionRemoved", "reauthorizationRequired", "missed"]


class EmailTasks(TaskDispatch):
    source = "email_notification"
    intents = "email_dispatch_intents"
    terminal_retention = timedelta(days=7)

    def wake(
        self,
        account: AccountKey,
        channel: WakeSource,
        event_id: str,
        *,
        lifecycle: Lifecycle | None = None,
        subscription: str | None = None,
    ) -> str:
        db = self.db
        key = stable_key(self.source, account.account_id, channel, event_id)
        job = db.ref("tasks", key)
        intent = db.ref(self.intents, key)

        def save(tx: Transaction) -> None:
            if db.read(job, tx) is not None:
                return
            now = db.clock()
            tx.create(
                job,
                {
                    "source": self.source,
                    "owner_id": account.owner_id,
                    "account_id": account.account_id,
                    "channel": channel,
                    "lifecycle": lifecycle,
                    "subscription": subscription,
                    "created_at": now,
                    "state": "pending",
                    "next_check_at": now,
                    "fence": 0,
                },
            )
            tx.create(intent, {"generation": 0, "next_check_at": now, "dispatched": False})

        db.atomic(save)
        self.dispatch(key)
        return key

    def poll(self, settings: Settings, scheduled: datetime) -> int:
        store = DigestStore(self.db, provider=settings.email_provider)
        recipients = sorted(set(settings.allowed_user_ids).intersection(settings.allowed_chat_ids))
        event = str(int(scheduled.timestamp()) // 900)
        count = 0
        for user in recipients:
            for account in store.accounts(Recipient(user, user)):
                if self.db.account(account)["credential_status"] == "active":
                    self.wake(account, "poll", event)
                    count += 1
        return count
