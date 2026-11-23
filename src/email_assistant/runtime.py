import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Never
from uuid import UUID

import httpx
from google.cloud import firestore

from email_assistant.ai.provider import LiteLLMProvider, LLMProvider
from email_assistant.config import ConfigurationError, Settings
from email_assistant.email.gmail import GMAIL, GmailProvider
from email_assistant.email.outlook import OutlookProvider
from email_assistant.pipeline.classification import ClassificationCoordinator
from email_assistant.pipeline.digest import DigestCoordinator, DigestStore
from email_assistant.pipeline.sync import SyncCoordinator
from email_assistant.security.account_secrets import AccountSecrets, SecretManagerAccountSecrets
from email_assistant.security.credentials import CredentialCoordinator
from email_assistant.security.gmail_oauth import GmailOAuth
from email_assistant.security.outlook_oauth import OutlookOAuth
from email_assistant.security.secrets import SecretManagerReader, SecretReader
from email_assistant.storage.classification import ClassificationRepository
from email_assistant.storage.firestore import FirestoreRepository
from email_assistant.storage.sync import SyncRepository
from email_assistant.telegram.buttons import Buttons
from email_assistant.telegram.delivery import Deliveries, TelegramNotifier


class UnconfiguredSecrets:
    def read(self, version: str) -> Never:
        raise ConfigurationError("测试环境需要注入模拟密钥。")


def secret_reader(settings: Settings) -> SecretReader:
    return UnconfiguredSecrets() if settings.environment == "test" else SecretManagerReader()


@contextmanager
def database(settings: Settings) -> Iterator[FirestoreRepository]:
    if os.environ.get("FIRESTORE_EMULATOR_HOST") != settings.firestore_emulator_host:
        raise ConfigurationError("Firestore 连接配置不一致。")
    client = firestore.Client(project=settings.project_id)
    try:
        yield FirestoreRepository(client)
    finally:
        client.close()  # type: ignore[no-untyped-call]


@dataclass(frozen=True)
class DigestRuntime:
    digest: DigestCoordinator
    model: LLMProvider


@contextmanager
def digest_runtime(
    settings: Settings,
    db: FirestoreRepository,
    *,
    account_secrets: AccountSecrets | None = None,
    oauth: OutlookOAuth | None = None,
    gmail_oauth: GmailOAuth | None = None,
    gmail_transport: httpx.BaseTransport | None = None,
) -> Iterator[DigestRuntime]:
    if settings.environment == "production" and any(
        item is not None for item in (account_secrets, oauth, gmail_oauth, gmail_transport)
    ):
        raise ConfigurationError("模拟邮箱配置仅在测试环境可用。")
    secrets = secret_reader(settings)
    if account_secrets is None:
        if settings.environment == "test":
            raise ConfigurationError("请注入测试用邮箱凭据存储。")
        account_secrets = SecretManagerAccountSecrets(
            settings.project_id, provider=settings.email_provider
        )

    provider: GmailProvider | OutlookProvider
    if settings.email_provider == "gmail":
        if settings.environment == "test" and (
            gmail_oauth is None or not (gmail_transport or settings.gmail_endpoint)
        ):
            raise ConfigurationError("请注入 Gmail 测试 OAuth 和模拟端点。")
        if gmail_oauth is None:
            version = settings.gmail_oauth_client_version
            if version is None:
                raise ConfigurationError("请配置 Gmail OAuth 客户端密钥版本。")
            gmail_oauth = GmailOAuth(secrets.read(version))
        provider = GmailProvider(
            gmail_oauth, endpoint=str(settings.gmail_endpoint or GMAIL), transport=gmail_transport
        )
    else:
        if oauth is None:
            tenant, client = settings.outlook_tenant_id, settings.outlook_client_id
            if settings.environment == "production" and (tenant is None or client is None):
                raise ConfigurationError("请配置 Outlook 租户和应用 ID。")
            oauth = OutlookOAuth(str(tenant or UUID(int=0)), str(client or UUID(int=0)))
        provider = OutlookProvider(oauth, settings=settings)
    try:
        repository = SyncRepository(db)
        sync = SyncCoordinator(
            repository, provider, CredentialCoordinator(db, account_secrets, provider)
        )
        model = LiteLLMProvider(settings, secrets)
        classification = ClassificationCoordinator(
            settings,
            ClassificationRepository(repository),
            sync.get_message,
            model,
        )
        delivery = Deliveries(db, TelegramNotifier(settings, secrets))
        store = DigestStore(db, provider=settings.email_provider)
        yield DigestRuntime(
            DigestCoordinator(settings, store, sync, classification, delivery, Buttons(store)),
            model,
        )
    finally:
        provider.close()
