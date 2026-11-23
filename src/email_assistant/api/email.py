import base64
import binascii
import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from hmac import compare_digest
from typing import cast

from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
from starlette.concurrency import run_in_threadpool

from email_assistant.api.telegram import InputError, error, read_object
from email_assistant.config import Settings
from email_assistant.email.provider import stable_key
from email_assistant.pipeline.urgent import UrgentWorker
from email_assistant.runtime import DigestRuntime
from email_assistant.security.secrets import SecretReader
from email_assistant.storage.firestore import AccountKey, FirestoreRepository, Record
from email_assistant.storage.subscriptions import Subscriptions
from email_assistant.tasks.email import EmailTasks, Lifecycle
from email_assistant.tasks.queue import TaskQueue
from email_assistant.tasks.telegram import TaskRequest

_UUID = r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"
_LIFECYCLE = {"subscriptionRemoved", "reauthorizationRequired", "missed"}


def _graph(
    subscriptions: Subscriptions, item: Record
) -> tuple[AccountKey, str, Lifecycle | None, str] | None:
    subscription, tenant = item.get("subscriptionId"), item.get("tenantId")
    client_state, resource = item.get("clientState"), item.get("resource")
    lifecycle = item.get("lifecycleEvent")
    if (
        not isinstance(subscription, str)
        or not re.fullmatch(_UUID, subscription)
        or not isinstance(tenant, str)
        or not re.fullmatch(_UUID, tenant)
        or not isinstance(client_state, str)
        or not 1 <= len(client_state) <= 128
        or (
            lifecycle is not None
            and (not isinstance(lifecycle, str) or lifecycle not in _LIFECYCLE)
        )
    ):
        return None
    match = subscriptions.lookup("subscription_id", subscription.casefold(), "outlook")
    if match is None:
        return None
    account, row = match
    if tenant.casefold() != row.get("tenant") or not compare_digest(
        stable_key(client_state), row.get("state_hash", "")
    ):
        return None
    if lifecycle is None:
        if (
            item.get("changeType") != "created"
            or not isinstance(resource, str)
            or len(resource) > 1500
            or not re.fullmatch(
                re.escape(row["resource"]) + r"/[A-Za-z0-9_+=.%~-]+",
                resource.lstrip("/"),
                flags=re.IGNORECASE | re.ASCII,
            )
        ):
            return None
        message = resource.lstrip("/").rsplit("/", 1)[1]
        event = stable_key(subscription.casefold(), message)
    else:
        # Lifecycle events lack a message resource and may repeat for one subscription.
        period = int(subscriptions.db.clock().timestamp()) // 900
        event = stable_key(subscription.casefold(), lifecycle, str(period))
    return account, event, cast(Lifecycle | None, lifecycle), subscription.casefold()


def install_email_webhooks(
    app: FastAPI,
    settings: Settings,
    database: Callable[[], AbstractContextManager[FirestoreRepository]],
    queue: Callable[[], TaskQueue],
    verify_gmail: Callable[[str], bool],
) -> None:
    if settings.gmail_push_subscription:

        @app.post("/email/gmail")
        async def gmail(request: Request) -> Response:
            if not await run_in_threadpool(verify_gmail, request.headers.get("Authorization", "")):
                return error(403, "邮件通知签名无效。")
            try:
                envelope = await read_object(request, 16 * 1024)
                if envelope.get("subscription") != settings.gmail_push_subscription:
                    return error(403, "邮件通知订阅无效。")
                message = envelope["message"]
                data = message["data"]
                event = message["messageId"]
                if (
                    not isinstance(data, str)
                    or not re.fullmatch(r"[A-Za-z0-9_+/=-]{1,4096}", data)
                    or not isinstance(event, str)
                    or not re.fullmatch(r"[0-9]{1,40}", event)
                ):
                    raise InputError
                payload = json.loads(
                    base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
                )
                address, history = payload["emailAddress"], payload["historyId"]
                if (
                    not isinstance(address, str)
                    or not re.fullmatch(r"[^\s@]{1,200}@[^\s@]{1,200}", address)
                    or not isinstance(history, str)
                    or not re.fullmatch(r"[0-9]{1,40}", history)
                ):
                    raise InputError
            except (ValueError, KeyError, TypeError, binascii.Error, RecursionError):
                return error(400, "无效的 Gmail 通知。")

            def persist() -> bool:
                with database() as db:
                    match = Subscriptions(db).lookup(
                        "mailbox_hash", stable_key(address.casefold()), "gmail"
                    )
                    if match is None:
                        return False
                    EmailTasks(db, queue(), lease_seconds=settings.hard_timeout_seconds).wake(
                        match[0],
                        "gmail",
                        event,
                    )
                    return True

            try:
                accepted = await run_in_threadpool(persist)
            except Exception:
                return error(503, "邮件通知暂时无法保存。")
            return Response(status_code=204) if accepted else error(403, "邮箱映射无效。")

    if settings.email_provider == "outlook":

        @app.post("/email/graph")
        async def graph(request: Request) -> Response:
            challenge = request.query_params.get("validationToken")
            if challenge is not None:
                if not 1 <= len(challenge) <= 1024 or any(
                    ord(char) < 32 or ord(char) == 127 for char in challenge
                ):
                    return error(400, "无效的 Graph 验证请求。")
                return PlainTextResponse(challenge, headers={"X-Content-Type-Options": "nosniff"})
            try:
                items = (await read_object(request, 64 * 1024)).get("value")
                if (
                    not isinstance(items, list)
                    or not 1 <= len(items) <= 100
                    or any(not isinstance(item, dict) for item in items)
                ):
                    raise InputError
            except InputError:
                return error(400, "无效的 Graph 通知。")

            def persist_graph() -> int:
                with database() as db:
                    tasks = EmailTasks(db, queue(), lease_seconds=settings.hard_timeout_seconds)
                    subscriptions = Subscriptions(db)
                    count = 0
                    for item in items:
                        if match := _graph(subscriptions, item):
                            account, event, lifecycle, subscription = match
                            tasks.wake(
                                account,
                                "graph",
                                event,
                                lifecycle=lifecycle,
                                subscription=subscription,
                            )
                            count += 1
                    return count

            try:
                accepted = await run_in_threadpool(persist_graph)
            except Exception:
                return error(503, "邮件通知暂时无法保存。")
            return Response(status_code=202) if accepted else error(403, "Graph 通知验证失败。")


def install_email_worker(
    app: FastAPI,
    settings: Settings,
    database: Callable[[], AbstractContextManager[FirestoreRepository]],
    queue: Callable[[], TaskQueue],
    runtime: Callable[[FirestoreRepository], AbstractContextManager[DigestRuntime]],
    verify: Callable[[str], bool],
    secrets: Callable[[], SecretReader],
) -> None:
    @app.post("/internal/email/tasks")
    async def execute(request: Request) -> Response:
        if not await run_in_threadpool(verify, request.headers.get("Authorization", "")):
            return error(403, "任务调用方未获授权。")
        try:
            task = TaskRequest.model_validate(await read_object(request, 1024))
        except ValueError:
            return error(400, "无效的任务 ID。")

        try:
            with database() as db:
                tasks = EmailTasks(db, queue(), lease_seconds=settings.hard_timeout_seconds)
                lease = await run_in_threadpool(tasks.claim, task.task_id)
                if lease == "done":
                    return Response(status_code=204)
                if lease == "busy":
                    return error(503, "任务正在处理或等待重试。")
                try:
                    with runtime(db) as services:
                        delay = await UrgentWorker(settings, services.digest, secrets()).run(lease)
                except Exception:
                    delay = 5
                await run_in_threadpool(tasks.finish, lease, retry_after=delay)
        except Exception:
            return error(503, "邮件任务暂时不可用。")
        return error(503, "邮件任务等待重试。") if delay else Response(status_code=204)
