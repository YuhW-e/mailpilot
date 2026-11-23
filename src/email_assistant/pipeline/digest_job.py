import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import google.auth
from google.auth.transport.requests import AuthorizedSession
from google.cloud.firestore import DELETE_FIELD
from google.cloud.firestore_v1 import FieldFilter, Transaction

from email_assistant.pipeline.digest import DigestRun, Window
from email_assistant.storage.firestore import FirestoreRepository
from email_assistant.telegram.delivery import Recipient

LAUNCH_GRACE = timedelta(minutes=2)
logger = logging.getLogger(__name__)
TERMINAL = {"confirmed", "failed", "unknown"}


@dataclass(frozen=True)
class DigestExecution:
    batch_id: str
    token: str
    recipient: Recipient
    window: Window


class DigestJobs:
    """Reserve launches and executions separately across the Cloud Run API boundary."""

    def __init__(self, db: FirestoreRepository) -> None:
        self.db = db

    def register(self, recipient: Recipient, window: Window) -> str:
        batch_id = window.key(recipient, None)
        ref = self.db.ref("digest_job_intents", batch_id)

        def create(tx: Transaction) -> None:
            if self.db.read(ref, tx) is None:
                tx.create(
                    ref,
                    {
                        "user_id": recipient.user_id,
                        "chat_id": recipient.chat_id,
                        "slot": window.slot,
                        "scheduled_at": window.end,
                        "next_check_at": self.db.clock(),
                    },
                )

        self.db.atomic(create)
        return batch_id

    def launch(self, batch_id: str, submit: Callable[[str], None]) -> bool:
        ref = self.db.ref("digest_job_intents", batch_id)

        def reserve(tx: Transaction) -> bool:
            now = self.db.clock()
            row = self.db.read(ref, tx)
            if not row or not row.get("next_check_at") or row["next_check_at"] > now:
                return False
            tx.update(ref, {"next_check_at": now + LAUNCH_GRACE, "execution": None})
            return True

        if not self.db.atomic(reserve):
            return False
        # A timeout can mean the execution already exists. Leave the reservation in
        # place and let recovery retry if no execution checks in.
        try:
            submit(batch_id)
        except RuntimeError:
            logger.warning("digest_job_launch_unconfirmed")
            return False
        return True

    def recover(self, submit: Callable[[str], None], limit: int = 10) -> int:
        due = self.db.client.collection("digest_job_intents").where(
            filter=FieldFilter("next_check_at", "<=", self.db.clock())
        )
        return sum(self.launch(doc.id, submit) for doc in due.limit(limit).get())

    def claim(self, batch_id: str, timeout_seconds: int) -> DigestExecution | None:
        ref = self.db.ref("digest_job_intents", batch_id)
        token = uuid4().hex

        def start(tx: Transaction) -> DigestExecution | None:
            row = self.db.read(ref, tx)
            now = self.db.clock()
            if (
                not row
                or row.get("state") in TERMINAL
                or (row.get("execution") and row["next_check_at"] > now)
            ):
                return None
            recipient = Recipient(row["user_id"], row["chat_id"])
            window = Window.scheduled(row["scheduled_at"], row["slot"])
            if batch_id != window.key(recipient, None):
                raise ValueError("Invalid digest intent.")
            tx.update(
                ref,
                {
                    "execution": token,
                    "next_check_at": now + timedelta(seconds=timeout_seconds),
                },
            )
            return DigestExecution(batch_id, token, recipient, window)

        return self.db.atomic(start)

    def finish(self, execution: DigestExecution, outcome: DigestRun) -> None:
        ref = self.db.ref("digest_job_intents", execution.batch_id)

        def settle(tx: Transaction) -> None:
            row = self.db.read(ref, tx)
            if not row or row.get("execution") != execution.token:
                return
            retry_at = (
                DELETE_FIELD
                if outcome.state in TERMINAL
                else self.db.clock() + timedelta(seconds=max(5, outcome.retry_after))
            )
            tx.update(ref, {"state": outcome.state, "execution": None, "next_check_at": retry_at})

        self.db.atomic(settle)


def submit_job(job: str, batch_id: str) -> None:
    try:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        with AuthorizedSession(credentials) as session:  # type: ignore[no-untyped-call]
            response = session.post(
                f"https://run.googleapis.com/v2/{job}:run",
                json={"overrides": {"containerOverrides": [{"args": ["--batch-id", batch_id]}]}},
                timeout=5,
            )
            response.raise_for_status()
    except Exception:
        raise RuntimeError("摘要 Job 启动尚未确认。") from None
