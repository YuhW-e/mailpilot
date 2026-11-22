import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Never
from uuid import UUID

from google.cloud import firestore

from email_assistant.ai.provider import LiteLLMProvider, LLMProvider
from email_assistant.config import ConfigurationError, Settings
from email_assistant.email.outlook import OutlookProvider
from email_assistant.pipeline.classification import ClassificationCoordinator
from email_assistant.pipeline.digest import DigestCoordinator, DigestStore
from email_assistant.pipeline.sync import SyncCoordinator
from email_assistant.security.account_secrets import AccountSecrets, SecretManagerAccountSecrets
from email_assistant.security.credentials import CredentialCoordinator
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
) -> Iterator[DigestRuntime]:
    if settings.environment == "production" and (account_secrets is not None or oauth is not None):
        raise ConfigurationError("模拟 Outlook 凭据仅在测试环境可用。")
    if oauth is None:
        tenant, client = settings.outlook_tenant_id, settings.outlook_client_id
        if settings.environment == "production" and (tenant is None or client is None):
            raise ConfigurationError("请配置 Outlook 租户和应用 ID。")
        oauth = OutlookOAuth(str(tenant or UUID(int=0)), str(client or UUID(int=0)))
    if account_secrets is None:
        if settings.environment == "test":
            raise ConfigurationError("请注入测试用 Outlook 凭据存储。")
        account_secrets = SecretManagerAccountSecrets(settings.project_id)
    secrets = secret_reader(settings)
    outlook = OutlookProvider(oauth, settings=settings)
    try:
        repository = SyncRepository(db)
        sync = SyncCoordinator(
            repository, outlook, CredentialCoordinator(db, account_secrets, outlook)
        )
        model = LiteLLMProvider(settings, secrets)
        classification = ClassificationCoordinator(
            settings,
            ClassificationRepository(repository),
            sync.get_message,
            model,
        )
        delivery = Deliveries(db, TelegramNotifier(settings, secrets))
        store = DigestStore(db)
        yield DigestRuntime(
            DigestCoordinator(settings, store, sync, classification, delivery, Buttons(store)),
            model,
        )
    finally:
        outlook.close()
