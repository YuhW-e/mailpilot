import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Protocol

from google.cloud.firestore_v1 import Transaction
from pydantic import SecretStr
from telegram import Bot, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest, Forbidden, InvalidToken, RetryAfter
from telegram.request import HTTPXRequest

from email_assistant.config import Settings
from email_assistant.email.provider import stable_key
from email_assistant.security.secrets import SecretReader
from email_assistant.storage.firestore import FirestoreRepository


@dataclass(frozen=True)
class Recipient:
    user_id: int
    chat_id: int

    @property
    def owner_id(self) -> str:
        return str(self.user_id)

    def allowed(self, settings: Settings) -> bool:
        return (
            self.user_id in settings.allowed_user_ids
            and self.chat_id in settings.allowed_chat_ids
            and self.user_id == self.chat_id
        )


type SendState = Literal["confirmed", "retry", "failed", "unknown", "busy"]


@dataclass(frozen=True)
class SendResult:
    state: SendState
    message_id: int | None = None
    retry_after: float = 0


class Notifier(Protocol):
    async def send(
        self, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None
    ) -> SendResult: ...


class TelegramNotifier:
    def __init__(self, settings: Settings, secrets: SecretReader) -> None:
        self.settings = settings
        self.secrets = secrets

    def _token(self) -> SecretStr:
        if self.settings.environment == "test":
            return SecretStr("fixture")
        version = self.settings.telegram_bot_token_version
        if version is None:
            raise InvalidToken()
        return self.secrets.read(version)

    async def send(
        self, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None
    ) -> SendResult:
        try:
            token = await asyncio.to_thread(self._token)
        except InvalidToken:
            return SendResult("failed")
        except Exception:
            return SendResult("retry", retry_after=5)
        # HTTP/PTB request URLs include the token. SDK debug output can include message text.
        for logger in ("telegram", "httpx", "httpcore"):
            logging.getLogger(logger).setLevel(logging.CRITICAL)
        try:
            endpoint = self.settings.telegram_endpoint
            if self.settings.environment == "test" and endpoint is None:
                return SendResult("failed")
            base = str(endpoint).rstrip("/") + "/bot" if endpoint else None
            transport = HTTPXRequest(
                connect_timeout=3,
                read_timeout=5,
                write_timeout=5,
                httpx_kwargs={"trust_env": False},
            )
            bot = Bot(
                token.get_secret_value(),
                base_url=base or "https://api.telegram.org/bot",
                request=transport,
                get_updates_request=transport,
            )
        except InvalidToken:
            return SendResult("failed")
        request = bot.request
        try:
            message = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except RetryAfter as exc:
            delay = exc.retry_after
            return SendResult(
                "retry",
                retry_after=max(
                    1, delay.total_seconds() if isinstance(delay, timedelta) else delay
                ),
            )
        except (BadRequest, Forbidden, InvalidToken):
            return SendResult("failed")
        except Exception:
            # A successful write can be followed by a lost or malformed response.
            return SendResult("unknown")
        finally:
            await request.shutdown()
        if message.chat.id != chat_id or message.message_id <= 0:
            return SendResult("unknown")
        return SendResult("confirmed", message.message_id)


class Deliveries:
    def __init__(self, db: FirestoreRepository, notifier: Notifier) -> None:
        self.db = db
        self.notifier = notifier

    @staticmethod
    def key(recipient: Recipient, business_key: str, segment: int) -> str:
        return stable_key(recipient.owner_id, str(recipient.chat_id), business_key, str(segment))

    def outcome(
        self,
        recipient: Recipient,
        business_key: str,
        segment: int = 0,
    ) -> SendResult | None:
        row = self.db.read(self.db.ref("deliveries", self.key(recipient, business_key, segment)))
        if row and row["state"] in {"confirmed", "failed", "unknown"}:
            return SendResult(row["state"], row.get("telegram_message_id"))
        return None

    async def send(
        self,
        recipient: Recipient,
        business_key: str,
        segment: int,
        text: str,
        *,
        batch_id: str | None = None,
        keyboard: InlineKeyboardMarkup | None = None,
        view: str = "digest",
    ) -> SendResult:
        ref = self.db.ref("deliveries", self.key(recipient, business_key, segment))

        def claim(tx: Transaction) -> SendResult | None:
            old = self.db.read(ref, tx)
            now = self.db.clock()
            if old:
                state = old["state"]
                if state in {"confirmed", "failed", "unknown"}:
                    return SendResult(state, old.get("telegram_message_id"))
                if state == "sending":
                    if old["next_check_at"] > now:
                        return SendResult("busy", retry_after=2)
                    tx.update(ref, {"state": "unknown", "checked_at": now})
                    return SendResult("unknown")
                if old["next_check_at"] > now:
                    delay = (old["next_check_at"] - now).total_seconds()
                    return SendResult("retry", retry_after=delay)
            tx.set(
                ref,
                {
                    "owner_id": recipient.owner_id,
                    "chat_id": str(recipient.chat_id),
                    "batch_id": batch_id,
                    "business_key": business_key,
                    "segment": segment,
                    "state": "sending",
                    "next_check_at": now + timedelta(seconds=30),
                },
            )
            return None

        previous = self.db.atomic(claim)
        if previous:
            return previous
        result = await self.notifier.send(recipient.chat_id, text, keyboard)

        def finish(tx: Transaction) -> SendResult:
            old = self.db.read(ref, tx)
            if old is None or old["state"] != "sending":
                return SendResult("unknown")
            now = self.db.clock()
            tx.update(
                ref,
                {
                    "state": result.state,
                    "telegram_message_id": result.message_id,
                    "checked_at": now,
                    "next_check_at": now + timedelta(seconds=result.retry_after),
                },
            )
            if result.state == "confirmed" and batch_id is not None:
                message_key = stable_key(str(recipient.chat_id), str(result.message_id))
                tx.set(
                    self.db.ref("telegram_messages", message_key),
                    {
                        "owner_id": recipient.owner_id,
                        "chat_id": str(recipient.chat_id),
                        "batch_id": batch_id,
                        "view": view,
                    },
                )
            return result

        return self.db.atomic(finish)
