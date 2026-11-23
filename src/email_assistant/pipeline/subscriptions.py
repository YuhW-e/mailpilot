import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from hmac import digest

from pydantic import SecretStr

from email_assistant.config import Settings
from email_assistant.email.gmail import GmailProvider
from email_assistant.email.outlook import OutlookProvider
from email_assistant.email.provider import ErrorCode, ProviderError, stable_key
from email_assistant.pipeline.sync import SyncCoordinator
from email_assistant.security.secrets import SecretReader, SecretReadError
from email_assistant.storage.firestore import Lease, Record
from email_assistant.storage.subscriptions import Subscriptions

logger = logging.getLogger("uvicorn.error.mailpilot.email")


class SubscriptionMaintenance:
    def __init__(self, settings: Settings, sync: SyncCoordinator, secrets: SecretReader) -> None:
        self.settings = settings
        self.sync = sync
        self.secrets = secrets
        self.store = Subscriptions(sync.store.db)

    def run(self, lease: Lease, event: Record) -> None:
        account = lease.account
        settings = self.settings
        provider = self.sync.provider
        db = self.store.db
        now = db.clock()
        current = self.store.read(account)
        expires = current.get("expires_at")
        binding = current.get("subscription_id")
        lifecycle = event["lifecycle"] if event.get("subscription") == binding else None
        forced = lifecycle in {"subscriptionRemoved", "reauthorizationRequired"}
        if event["channel"] != "poll" and not forced:
            return
        if current.get("retry_at") and current["retry_at"] > now:
            return

        def authenticated[T](operation: Callable[[SecretStr], T]) -> T:
            try:
                return operation(self.sync.credentials.access(account))
            except ProviderError as exc:
                if exc.code != ErrorCode.REAUTHORIZE:
                    raise
            return operation(self.sync.credentials.access(account, force=True))

        fields: Record
        try:
            if isinstance(provider, GmailProvider) and (topic := settings.gmail_watch_topic):
                if (
                    current.get("status") == "active"
                    and current.get("target") == topic
                    and expires
                    and expires > now + timedelta(hours=1)
                    and current["renewed_at"] + timedelta(days=1) > now
                ):
                    return
                address = authenticated(provider.mailbox_address)
                gmail_expiry = authenticated(lambda token: provider.watch(token, topic))
                if gmail_expiry <= now:
                    raise ProviderError(ErrorCode.INVALID_RESPONSE)
                fields = {
                    "mailbox_hash": stable_key(address.casefold()),
                    "expires_at": gmail_expiry,
                    "target": topic,
                }
            elif isinstance(provider, OutlookProvider) and settings.graph_notification_url:
                if (
                    not forced
                    and current.get("status") == "active"
                    and expires
                    and expires > now + timedelta(days=2)
                    and current.get("target") == str(settings.graph_notification_url)
                    and current.get("key_version") == settings.graph_client_state_version
                ):
                    return
                identity = db.account(account)["identity"]
                callback = str(settings.graph_notification_url)
                version = settings.graph_client_state_version
                if version is None:
                    raise ProviderError(ErrorCode.AUTHORIZATION)
                secret = self.secrets.read(version).get_secret_value().encode()
                if len(secret) < 32:
                    raise ProviderError(ErrorCode.AUTHORIZATION)
                client_state = digest(secret, account.account_id.encode(), "sha256").hex()
                state_hash = stable_key(client_state)
                expiry = now + timedelta(days=6)
                graph_result: tuple[str, datetime] | None = None
                if (
                    isinstance(binding, str)
                    and expires
                    and expires > now
                    and lifecycle != "subscriptionRemoved"
                ):
                    saved_id = binding
                    if current.get("target") != callback or current.get("state_hash") != state_hash:
                        authenticated(lambda token: provider.remove_subscription(token, saved_id))
                    else:
                        try:
                            graph_result = authenticated(
                                lambda token: provider.renew_subscription(token, saved_id, expiry)
                            )
                            if graph_result[0] != binding:
                                raise ProviderError(ErrorCode.INVALID_RESPONSE)
                        except ProviderError as exc:
                            if exc.code != ErrorCode.NOT_FOUND:
                                raise

                if graph_result is None:
                    graph_result = authenticated(
                        lambda token: provider.subscribe(
                            token, identity["subject"], callback, client_state, expiry
                        )
                    )
                if graph_result[1] <= now:
                    raise ProviderError(ErrorCode.INVALID_RESPONSE)
                fields = {
                    "subscription_id": graph_result[0],
                    "expires_at": graph_result[1],
                    "tenant": identity["issuer_tenant"].casefold(),
                    "resource": f"users/{identity['subject'].casefold()}/messages",
                    "state_hash": state_hash,
                    "key_version": version,
                    "target": callback,
                }
            else:
                return
        except (ProviderError, SecretReadError) as exc:
            code = exc.code.value if isinstance(exc, ProviderError) else "secret_unavailable"
            wait = max(60, (exc.retry_after or 0) if isinstance(exc, ProviderError) else 0)
            self.store.save(
                lease,
                settings.email_provider,
                {
                    "status": "poll_only",
                    "retry_at": now + timedelta(seconds=wait),
                    "last_error": code,
                    **({"expires_at": now} if lifecycle == "subscriptionRemoved" else {}),
                },
            )
            logger.warning(
                "email_subscription_failure provider=%s code=%s", settings.email_provider, code
            )
            return

        self.store.save(
            lease,
            settings.email_provider,
            {
                **fields,
                "status": "active",
                "last_error": None,
                "retry_at": None,
                "renewed_at": now,
            },
        )
        logger.info(
            "email_subscription_active provider=%s remaining_seconds=%d",
            settings.email_provider,
            int((fields["expires_at"] - now).total_seconds()),
        )
