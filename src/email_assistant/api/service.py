import uvicorn
from fastapi import FastAPI

from email_assistant.config import ConfigurationError, Role, Settings, load_settings


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

    return app


def serve(role: Role) -> None:
    try:
        settings = load_settings(role)
        app = create_service(settings, role)
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    uvicorn.run(app, host="0.0.0.0", port=settings.port, access_log=False)
