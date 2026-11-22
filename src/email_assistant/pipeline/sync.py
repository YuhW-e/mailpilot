import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from time import monotonic, sleep

from pydantic import SecretStr

from email_assistant.email.provider import EmailProvider, ErrorCode, MessageBody, ProviderError
from email_assistant.security.credentials import CredentialCoordinator
from email_assistant.storage.firestore import AccountKey
from email_assistant.storage.sync import SyncRepository


class _YieldWork(Exception):
    pass


@dataclass(frozen=True)
class SyncResult:
    pending: bool
    pages: int


class SyncCoordinator:
    def __init__(
        self,
        store: SyncRepository,
        provider: EmailProvider,
        credentials: CredentialCoordinator,
        *,
        clock: Callable[[], float] = monotonic,
        wait: Callable[[float], None] = sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.store = store
        self.provider = provider
        self.credentials = credentials
        self.clock = clock
        self.wait = wait
        self.jitter = jitter

    def run(
        self,
        account: AccountKey,
        *,
        budget_seconds: int = 240,
        max_pages: int = 20,
        backfill_start: datetime | None = None,
        backfill_end: datetime | None = None,
    ) -> SyncResult:
        if budget_seconds <= 0 or max_pages <= 0:
            raise ValueError("Synchronization limits must be positive.")
        if any(d is not None and d.utcoffset() is None for d in (backfill_start, backfill_end)):
            raise ValueError("Backfill limits must have a time zone.")
        if backfill_start and backfill_end and backfill_start >= backfill_end:
            raise ValueError("Invalid backfill window.")

        deadline = self.clock() + budget_seconds
        lease = self.store.db.acquire(account, "sync", timedelta(seconds=budget_seconds + 30))
        if lease is None:
            return SyncResult(True, 0)

        def check_budget() -> None:
            if self.clock() >= deadline:
                raise _YieldWork

        def retry[T](operation: Callable[[], T]) -> T:
            for attempt in range(3):
                check_budget()
                try:
                    return operation()
                except ProviderError as exc:
                    if exc.code not in {ErrorCode.TEMPORARY, ErrorCode.THROTTLED}:
                        raise
                    delay = max(exc.retry_after or 0, 2**attempt * (0.5 + self.jitter()))
                    if self.clock() + delay >= deadline or attempt == 2:
                        self.store.postpone(lease, delay)
                        if attempt == 2:
                            raise
                        raise _YieldWork from None
                    self.wait(delay)
            raise AssertionError("Retry loop exhausted.")

        pages = 0
        try:
            # Recover authorization before beginning a round, including an initial grant whose
            # version write succeeded but whose Firestore commit did not.
            row = self.store.db.account(account)
            if row["wake_generation"] == row["completed_generation"]:
                return SyncResult(False, 0)
            if row.get("sync_retry_at") and row["sync_retry_at"] > self.store.db.clock():
                return SyncResult(True, 0)
            token = retry(lambda: self.credentials.access(account))
            round_state = self.store.begin(lease)
            if round_state is None:
                return SyncResult(False, 0)
            generation, discovered = round_state
            reauthenticated = False

            def graph[T](operation: Callable[[SecretStr], T]) -> T:
                nonlocal token, reauthenticated
                try:
                    return retry(lambda: operation(token))
                except ProviderError as exc:
                    if exc.code != ErrorCode.REAUTHORIZE or reauthenticated:
                        raise
                    reauthenticated = True
                    token = retry(lambda: self.credentials.access(account, force=True))
                    return retry(lambda: operation(token))

            if not discovered:
                discovery = graph(partial(self.provider.list_scopes, check_budget=check_budget))
                check_budget()
                self.store.discover(lease, generation, discovery, backfill_start, backfill_end)

            for _ in range(max_pages):
                check_budget()
                checkpoint = self.store.next_scope(lease, generation)
                if checkpoint is None:
                    return SyncResult(self.store.finish(lease, generation), pages)
                if checkpoint.reconciling:
                    self.store.reconcile(lease, generation, checkpoint)
                    continue
                started = self.store.db.clock()
                try:
                    page = graph(
                        partial(
                            self.provider.sync_page,
                            scope=checkpoint.scope,
                            continuation=checkpoint.continuation,
                        )
                    )
                except ProviderError as exc:
                    if exc.code == ErrorCode.CURSOR_EXPIRED and checkpoint.continuation:
                        self.store.reset(lease, generation, checkpoint)
                    elif exc.code in {ErrorCode.UNREADABLE, ErrorCode.NOT_FOUND}:
                        self.store.defer_scope(lease, generation, checkpoint)
                    else:
                        raise
                else:
                    if page.next_page and page.next_page == checkpoint.continuation:
                        raise ProviderError(ErrorCode.INVALID_RESPONSE)
                    self.store.apply_page(
                        lease,
                        generation,
                        checkpoint,
                        page,
                        check_budget=check_budget,
                        request_started_at=started,
                    )
                    pages += 1
            if self.store.next_scope(lease, generation) is None:
                return SyncResult(self.store.finish(lease, generation), pages)
            return SyncResult(True, pages)
        except _YieldWork:
            return SyncResult(True, pages)
        finally:
            self.store.db.release(lease)

    def get_message(self, account: AccountKey, message_id: str) -> MessageBody:
        self.store.indexed_message(account, message_id)
        token = self.credentials.access(account)
        try:
            return self.provider.get_message(token, message_id)
        except ProviderError as exc:
            if exc.code != ErrorCode.REAUTHORIZE:
                raise
        token = self.credentials.access(account, force=True)
        return self.provider.get_message(token, message_id)
