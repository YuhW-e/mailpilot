import re
from secrets import token_urlsafe
from typing import Literal

from google.cloud.firestore_v1 import Transaction
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from email_assistant.email.provider import stable_key
from email_assistant.pipeline.digest import DigestEntry, DigestStore, View, page_count
from email_assistant.storage.firestore import Record
from email_assistant.tasks.telegram import Operation
from email_assistant.telegram.delivery import Deliveries, Recipient


class Buttons:
    def __init__(self, store: DigestStore) -> None:
        self.store = store

    def keyboard(
        self,
        recipient: Recipient,
        batch: Record,
        view: View,
        page: int,
        entry: DigestEntry | None,
        business_key: str,
        segment: int,
        footer_entries: list[DigestEntry] | None,
    ) -> InlineKeyboardMarkup:
        db = self.store.db
        delivery_id = Deliveries.key(recipient, business_key, segment)
        commands: list[list[tuple[str, Operation]]] = []
        if entry is not None:
            actions: tuple[tuple[str, Literal["detail", "translate"]], ...] = (
                ("详情", "detail"),
                ("翻译", "translate"),
            )
            commands.append(
                [
                    (
                        f"{label} {entry.number}",
                        Operation(
                            action=action,
                            batch_id=batch["batch_id"],
                            view=view,
                            numbers=(entry.number,),
                        ),
                    )
                    for label, action in actions
                ]
            )
        if footer_entries is not None:
            navigation = []
            for label, destination in (("上一页", page - 1), ("下一页", page + 1)):
                if 0 <= destination < page_count(batch["counts"][view]):
                    navigation.append(
                        (
                            label,
                            Operation(
                                action="page",
                                batch_id=batch["batch_id"],
                                view=view,
                                page=destination,
                            ),
                        )
                    )
            if navigation:
                commands.append(navigation)
            if len(footer_entries) > 1:
                commands.append(
                    [
                        (
                            "本页全部详情",
                            Operation(
                                action="detail",
                                batch_id=batch["batch_id"],
                                view=view,
                                numbers=tuple(entry.number for entry in footer_entries),
                            ),
                        ),
                        (
                            "本页全部翻译",
                            Operation(
                                action="translate",
                                batch_id=batch["batch_id"],
                                view=view,
                                numbers=tuple(entry.number for entry in footer_entries),
                            ),
                        ),
                    ]
                )
            other: View = "filtered" if view == "digest" else "digest"
            commands.append(
                [
                    (
                        "过滤清单" if other == "filtered" else "邮件摘要",
                        Operation(action="page", batch_id=batch["batch_id"], view=other),
                    )
                ]
            )

        def create(tx: Transaction) -> list[list[str]]:
            rows = []
            for row in commands:
                tokens = []
                for _, operation in row:
                    token = token_urlsafe(16)
                    tokens.append(token)
                    tx.create(
                        db.ref("telegram_buttons", stable_key(token)),
                        {
                            "owner_id": recipient.owner_id,
                            "chat_id": str(recipient.chat_id),
                            "source_view": view,
                            "delivery_id": delivery_id,
                            "operation": operation.model_dump(mode="json"),
                            "expires_at": batch["expires_at"],
                        },
                    )
                rows.append(tokens)
            return rows

        tokens = db.atomic(create)
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(label, callback_data=token)
                    for (label, _), token in zip(row, row_tokens, strict=True)
                ]
                for row, row_tokens in zip(commands, tokens, strict=True)
            ]
        )

    def resolve(self, recipient: Recipient, source_message: int, token: str) -> Operation | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{22}", token):
            return None
        db = self.store.db
        row = db.read(db.ref("telegram_buttons", stable_key(token)))
        if (
            not row
            or row["owner_id"] != recipient.owner_id
            or row["chat_id"] != str(recipient.chat_id)
            or row["expires_at"] <= db.clock()
        ):
            return None
        operation = Operation.model_validate(row["operation"])
        if operation.batch_id is None or self.store.batch(recipient, operation.batch_id) is None:
            return None
        delivery = db.read(db.ref("deliveries", row["delivery_id"]))
        source = self.store.message(recipient, source_message)
        if (
            not delivery
            or delivery["state"] != "confirmed"
            or delivery["telegram_message_id"] != source_message
            or not source
            or source["batch_id"] != operation.batch_id
            or source.get("view") != row["source_view"]
        ):
            return None
        return operation
