from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from email_assistant.api.telegram import error
from email_assistant.config import Settings
from email_assistant.pipeline.digest import Window
from email_assistant.pipeline.digest_job import DigestJobs
from email_assistant.storage.firestore import FirestoreRepository
from email_assistant.tasks.email import EmailTasks
from email_assistant.tasks.queue import TaskQueue
from email_assistant.tasks.telegram import TelegramTasks
from email_assistant.telegram.delivery import Recipient


def install_scheduler(
    app: FastAPI,
    settings: Settings,
    database: Callable[[], AbstractContextManager[FirestoreRepository]],
    queue: Callable[[], TaskQueue],
    verify: Callable[[str], bool],
    submit: Callable[[str], None],
    *,
    email_queue: Callable[[], TaskQueue] | None = None,
) -> None:
    recipients = sorted(set(settings.allowed_user_ids).intersection(settings.allowed_chat_ids))

    @app.post("/internal/schedule/{slot}")
    def schedule(slot: str, request: Request) -> Response:
        if not verify(request.headers.get("Authorization", "")):
            return error(403, "计划调用方未获授权。")
        try:
            route_slots = {"morning": "06:00", "evening": "21:00"}
            at = datetime.fromisoformat(request.headers["X-CloudScheduler-ScheduleTime"])
            if at.second or at.microsecond:
                raise ValueError
            window = Window.scheduled(at, route_slots[slot])
        except (ValueError, KeyError):
            return error(400, "无效的计划时间或摘要时段。")

        try:
            with database() as db:
                jobs = DigestJobs(db)
                batch_ids = [jobs.register(Recipient(user, user), window) for user in recipients]
                for batch_id in batch_ids:
                    jobs.launch(batch_id, submit)
        except Exception:
            return error(503, "摘要计划暂时无法处理。")
        return Response(status_code=202)

    @app.post("/internal/telegram/recover")
    def recover(request: Request) -> Response:
        if not verify(request.headers.get("Authorization", "")):
            return error(403, "恢复调用方未获授权。")
        scheduled = None
        if email_queue is not None:
            try:
                scheduled = datetime.fromisoformat(request.headers["X-CloudScheduler-ScheduleTime"])
                if scheduled.utcoffset() is None:
                    raise ValueError
            except (ValueError, KeyError):
                return error(400, "无效的巡检计划时间。")
        try:
            with database() as db:
                launched = DigestJobs(db).recover(submit, limit=4)
                dispatched = TelegramTasks(
                    db, queue(), lease_seconds=settings.hard_timeout_seconds
                ).recover(limit=10)
                result = {"dispatched": dispatched, "launched": launched}
                if email_queue is not None and scheduled is not None:
                    email = EmailTasks(
                        db, email_queue(), lease_seconds=settings.hard_timeout_seconds
                    )
                    result["email_recovered"] = email.recover(limit=20)
                    result["email_polled"] = email.poll(settings, scheduled)
        except Exception:
            return error(503, "任务恢复暂时不可用。")
        return JSONResponse(result)
