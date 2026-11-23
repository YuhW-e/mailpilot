from functools import lru_cache, partial

import uvicorn
from fastapi import FastAPI

from email_assistant.api.email import install_email_webhooks, install_email_worker
from email_assistant.api.scheduler import install_scheduler
from email_assistant.api.telegram import install_webhook, install_worker
from email_assistant.config import ConfigurationError, Role, Settings, load_settings
from email_assistant.pipeline.digest_job import submit_job
from email_assistant.runtime import database, digest_runtime, secret_reader
from email_assistant.security.invoker import verified_invoker
from email_assistant.tasks.queue import CloudTaskQueue


def create_service(settings: Settings, role: Role) -> FastAPI:
    if settings.role != role:
        raise ConfigurationError("Configuration does not match the service entry point.")
    app = FastAPI(
        title=f"MailPilot {role}",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/healthz")
    def liveness() -> dict[str, str]:
        return {"status": "ok", "role": role.value}

    @lru_cache(maxsize=1)
    def queue() -> CloudTaskQueue:
        return CloudTaskQueue.configured(settings)

    @lru_cache(maxsize=1)
    def email_queue() -> CloudTaskQueue:
        return CloudTaskQueue.configured(settings, email=True)

    if role == Role.INGRESS:

        def secret() -> bytes:
            if settings.environment == "test":
                return b"fixture-webhook"
            version = settings.telegram_webhook_secret_version
            if version is None:
                raise ConfigurationError("Webhook secret is missing.")
            return secret_reader(settings).read(version).get_secret_value().encode()

        install_webhook(app, settings, partial(database, settings), queue, secret)
        if settings.email_queue_path:
            install_email_webhooks(
                app,
                settings,
                partial(database, settings),
                email_queue,
                partial(
                    verified_invoker,
                    audience=str(settings.gmail_push_audience or ""),
                    email=settings.gmail_push_email,
                ),
            )
    elif role == Role.WORKER:
        audience = str(settings.task_worker_url).rstrip("/") if settings.task_worker_url else ""
        install_worker(
            app,
            settings,
            partial(database, settings),
            queue,
            partial(digest_runtime, settings),
            partial(verified_invoker, audience=audience, email=settings.task_invoker_email),
        )

        if settings.email_queue_path:
            install_email_worker(
                app,
                settings,
                partial(database, settings),
                email_queue,
                partial(digest_runtime, settings),
                partial(verified_invoker, audience=audience, email=settings.task_invoker_email),
                partial(secret_reader, settings),
            )

        if settings.digest_job:
            install_scheduler(
                app,
                settings,
                partial(database, settings),
                queue,
                partial(
                    verified_invoker, audience=audience, email=settings.scheduler_invoker_email
                ),
                partial(submit_job, settings.digest_job),
                email_queue=email_queue if settings.email_queue_path else None,
            )

    return app


def serve(role: Role) -> None:
    try:
        settings = load_settings(role)
        app = create_service(settings, role)
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    uvicorn.run(app, host="0.0.0.0", port=settings.port, access_log=False)
