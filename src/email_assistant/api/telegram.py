import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import partial
from hmac import compare_digest
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from starlette.concurrency import run_in_threadpool
from telegram import Update

from email_assistant.config import Settings
from email_assistant.pipeline.digest import DigestStore
from email_assistant.runtime import DigestRuntime
from email_assistant.storage.firestore import FirestoreRepository
from email_assistant.tasks.queue import TaskQueue
from email_assistant.tasks.telegram import TaskRequest, TelegramTasks
from email_assistant.telegram.commands import InteractionWorker, command
from email_assistant.telegram.delivery import Recipient

UPDATE_LIMIT = 64 * 1024


class InputError(ValueError):
    pass


async def read_object(request: Request, limit: int) -> dict[str, Any]:
    data = bytearray()
    try:
        async for chunk in request.stream():
            if len(data) + len(chunk) > limit:
                raise InputError
            data.extend(chunk)
        value = json.loads(data)
    except (ValueError, RecursionError):
        raise InputError from None
    if not isinstance(value, dict):
        raise InputError
    return value


def error(status: int, message: str) -> Response:
    return JSONResponse({"error": message}, status_code=status)


def install_webhook(
    app: FastAPI,
    settings: Settings,
    database: Callable[[], AbstractContextManager[FirestoreRepository]],
    queue: Callable[[], TaskQueue],
    secret: Callable[[], bytes],
) -> None:
    @app.post("/telegram/webhook")
    async def webhook(request: Request) -> Response:
        supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "").encode()
        try:
            expected = await run_in_threadpool(secret)
        except Exception:
            return error(503, "验证暂时不可用。")
        if not supplied or not expected or not compare_digest(supplied, expected):
            return error(403, "Webhook 验证失败。")
        try:
            data = await read_object(request, UPDATE_LIMIT)
            if type(data.get("update_id")) is not int or data["update_id"] < 0:
                raise InputError
            update = Update.de_json(data, None)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
            return error(400, "无效或过大的 Telegram 更新。")
        if update.message is None and update.callback_query is None:
            return Response(status_code=200)
        user, chat = update.effective_user, update.effective_chat
        if (
            user is None
            or chat is None
            or user.is_bot
            or chat.type != "private"
            or type(user.id) is not int
            or type(chat.id) is not int
            or not (recipient := Recipient(user.id, chat.id)).allowed(settings)
        ):
            return error(403, "用户或私聊未获授权。")

        def accept() -> bool:
            with database() as db:
                operation = command(update, recipient, DigestStore(db))
                if operation is None:
                    return True
                bot_id = settings.telegram_bot_id
                if bot_id is None and settings.environment != "test":
                    return False
                tasks = TelegramTasks(db, queue(), lease_seconds=settings.hard_timeout_seconds)
                task_id = tasks.register(
                    str(bot_id or "fixture"), update.update_id, recipient, operation
                )
                tasks.dispatch(task_id)
                return operation.action != "error"

        try:
            accepted = await run_in_threadpool(accept)
        except Exception:
            return error(503, "更新暂时无法保存，请重试。")
        if settings.telegram_bot_id is None and settings.environment != "test":
            return error(503, "Telegram Bot 尚未配置。")
        if update.callback_query:
            response = {
                "method": "answerCallbackQuery",
                "callback_query_id": update.callback_query.id,
            }
            if not accepted:
                response["text"] = "操作无效或已过期。"
            return JSONResponse(response)
        return Response(status_code=200)


def verified_invoker(settings: Settings, authorization: str) -> bool:
    method, _, token = authorization.partition(" ")
    if method.lower() != "bearer" or not token or not settings.task_worker_url:
        return False
    try:
        claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            token,
            partial(GoogleRequest(), timeout=3),
            audience=str(settings.task_worker_url).rstrip("/"),
        )
    except Exception:
        return False
    return (
        settings.task_invoker_email is not None
        and claims.get("email") == settings.task_invoker_email
        and claims.get("email_verified") is True
    )


def install_worker(
    app: FastAPI,
    settings: Settings,
    database: Callable[[], AbstractContextManager[FirestoreRepository]],
    queue: Callable[[], TaskQueue],
    runtime: Callable[[FirestoreRepository], AbstractContextManager[DigestRuntime]],
    verify: Callable[[str], bool],
) -> None:
    @app.post("/internal/telegram/tasks")
    async def execute(request: Request) -> Response:
        if not await run_in_threadpool(verify, request.headers.get("Authorization", "")):
            return error(403, "任务调用方未获授权。")
        try:
            task = TaskRequest.model_validate(await read_object(request, 1024))
        except ValueError:
            return error(400, "无效的任务 ID。")

        try:
            with database() as db:
                tasks = TelegramTasks(db, queue(), lease_seconds=settings.hard_timeout_seconds)
                lease = await run_in_threadpool(tasks.claim, task.task_id)
                if lease == "done":
                    return Response(status_code=204)
                if lease == "busy":
                    return error(503, "任务正在处理或等待重试。")
                try:
                    with runtime(db) as services:
                        handler = InteractionWorker(settings, services.digest, services.model)
                        delay = await handler.run(lease)
                except Exception:
                    delay = 5
                await run_in_threadpool(tasks.finish, lease, retry_after=delay)
        except Exception:
            return error(503, "任务状态暂时不可用。")
        return error(503, "任务等待重试。") if delay else Response(status_code=204)

    @app.post("/internal/telegram/recover")
    async def recover(request: Request) -> Response:
        if not await run_in_threadpool(verify, request.headers.get("Authorization", "")):
            return error(403, "任务调用方未获授权。")

        def scan() -> int:
            with database() as db:
                return TelegramTasks(
                    db, queue(), lease_seconds=settings.hard_timeout_seconds
                ).recover()

        try:
            return JSONResponse({"dispatched": await run_in_threadpool(scan)})
        except Exception:
            return error(503, "任务恢复暂时不可用。")
