from fastapi import FastAPI

from email_assistant.api.service import create_service, serve
from email_assistant.config import Role, load_settings


def create_app() -> FastAPI:
    return create_service(load_settings(Role.INGRESS), Role.INGRESS)


def main() -> None:
    serve(Role.INGRESS)
