from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from google.cloud.firestore_v1 import FieldFilter, Transaction
from pydantic import BaseModel, ConfigDict, Field

from email_assistant.email.provider import stable_key
from email_assistant.pipeline.digest import View
from email_assistant.storage.firestore import FirestoreRepository, LeaseLost, Record
from email_assistant.tasks.queue import QueueUnavailable, TaskQueue
from email_assistant.telegram.delivery import Recipient

type Action = Literal["digest", "detail", "translate", "filtered", "page", "rules", "help", "error"]
CHECK_DISPATCH = timedelta(minutes=2)


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    action: Action
    batch_id: str | None = None
    view: View = "digest"
    numbers: tuple[int, ...] = ()
    page: int = 0
    error: str = ""


@dataclass(frozen=True)
class TaskLease:
    task_id: str
    fence: int
    row: Record


class TaskDispatch:
    source: str
    intents: str
    terminal_retention: timedelta | None = None

    def __init__(self, db: FirestoreRepository, queue: TaskQueue, *, lease_seconds: int) -> None:
        self.db = db
        self.queue = queue
        self.lease = timedelta(seconds=lease_seconds)

    def dispatch(self, task_id: str) -> bool:
        db = self.db
        job = db.ref("tasks", task_id)
        intent = db.ref(self.intents, task_id)

        def select(tx: Transaction) -> int | None:
            row = db.read(job, tx)
            state = db.read(intent, tx)
            now = db.clock()
            if (
                not row
                or row.get("source") != self.source
                or row["state"] == "done"
                or not state
                or state["next_check_at"] is None
                or state["next_check_at"] > now
                or row["next_check_at"] > now
            ):
                return None
            generation = int(state["generation"]) + int(state["dispatched"])
            tx.update(
                intent,
                {
                    "generation": generation,
                    "dispatched": False,
                    "next_check_at": now + timedelta(seconds=5),
                },
            )
            return generation

        generation = db.atomic(select)
        if generation is None:
            return False
        try:
            self.queue.enqueue(task_id, generation, db.clock())
        except QueueUnavailable:
            return False

        def dispatched(tx: Transaction) -> None:
            current = db.read(intent, tx)
            if (
                current
                and current["generation"] == generation
                and current["next_check_at"] is not None
            ):
                tx.update(
                    intent,
                    {
                        "dispatched": True,
                        "next_check_at": max(current["next_check_at"], db.clock() + CHECK_DISPATCH),
                    },
                )

        db.atomic(dispatched)
        return True

    def recover(self, limit: int = 50) -> int:
        query = self.db.client.collection(self.intents).where(
            filter=FieldFilter("next_check_at", "<=", self.db.clock())
        )
        return sum(
            self.dispatch(doc.id) for doc in query.order_by("next_check_at").limit(limit).stream()
        )

    def claim(self, task_id: str) -> TaskLease | Literal["busy", "done"]:
        db = self.db
        job = db.ref("tasks", task_id)
        intent = db.ref(self.intents, task_id)

        def update(tx: Transaction) -> TaskLease | Literal["busy", "done"]:
            row = db.read(job, tx)
            state = db.read(intent, tx)
            now = db.clock()
            if not row or row.get("source") != self.source or row["state"] == "done" or not state:
                return "done"
            if row["next_check_at"] > now:
                return "busy"
            fence = int(row["fence"]) + 1
            tx.update(job, {"state": "running", "fence": fence, "next_check_at": now + self.lease})
            tx.update(intent, {"next_check_at": now + self.lease})
            return TaskLease(task_id, fence, row)

        return db.atomic(update)

    def finish(self, lease: TaskLease, *, retry_after: float = 0) -> None:
        db = self.db
        job = db.ref("tasks", lease.task_id)
        intent = db.ref(self.intents, lease.task_id)

        def save(tx: Transaction) -> None:
            row = db.read(job, tx)
            if not row or row["state"] != "running" or row["fence"] != lease.fence:
                raise LeaseLost()
            next_check = db.clock() + timedelta(seconds=retry_after) if retry_after else None
            update: Record = {
                "state": "pending" if retry_after else "done",
                "next_check_at": next_check,
            }
            if next_check is None and self.terminal_retention:
                update["expires_at"] = db.clock() + self.terminal_retention
                tx.delete(intent)
            else:
                tx.update(intent, {"next_check_at": next_check})
            tx.update(job, update)

        db.atomic(save)


class TelegramTasks(TaskDispatch):
    source = "telegram"
    intents = "dispatch_intents"

    def register(
        self,
        bot_id: str,
        update_id: int,
        recipient: Recipient,
        operation: Operation,
    ) -> str:
        db = self.db
        key = stable_key(bot_id, str(update_id))
        update = db.ref("telegram_updates", key)
        job = db.ref("tasks", key)
        intent = db.ref(self.intents, key)

        def save(tx: Transaction) -> None:
            if db.read(update, tx) is not None:
                return
            now = db.clock()
            tx.create(update, {"task_id": key, "received_at": now})
            tx.create(
                job,
                {
                    "source": self.source,
                    "owner_id": recipient.owner_id,
                    "user_id": recipient.user_id,
                    "chat_id": recipient.chat_id,
                    "operation": operation.model_dump(mode="json"),
                    "created_at": now,
                    "state": "pending",
                    "next_check_at": now,
                    "fence": 0,
                },
            )
            tx.create(
                intent,
                {
                    "generation": 0,
                    "next_check_at": now,
                    "dispatched": False,
                },
            )

        db.atomic(save)
        return key


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    task_id: str = Field(pattern=r"^[a-f0-9]{64}$")
