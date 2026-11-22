import argparse
import asyncio
from datetime import datetime
from time import monotonic

from email_assistant.config import ConfigurationError, Role, load_settings
from email_assistant.pipeline.digest import DigestCoordinator, DigestRun, Window
from email_assistant.runtime import database, digest_runtime
from email_assistant.storage.firestore import AccountAccessError
from email_assistant.telegram.delivery import Recipient


async def run_job(
    coordinator: DigestCoordinator,
    recipient: Recipient,
    window: Window,
    *,
    manual_id: str | None = None,
) -> DigestRun:
    started = monotonic()
    result = await coordinator.run(recipient, window, manual_id=manual_id)
    print(
        f"batch={result.batch_id[:10]} state={result.state} "
        f"pages={result.pages} emails={result.emails} seconds={monotonic() - started:.2f}"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--chat-id", type=int)
    parser.add_argument("--at", type=datetime.fromisoformat)
    parser.add_argument("--slot", choices=["06:00", "21:00"])
    args = parser.parse_args()
    try:
        settings = load_settings(Role.JOB)
        if all(value is None for value in vars(args).values()):
            print("MailPilot Job smoke check passed.")
            return
        if not all(value is not None for value in vars(args).values()):
            parser.error("摘要需要 --user-id、--chat-id、--at 和 --slot。")
        window = Window.scheduled(args.at, args.slot)
        recipient = Recipient(args.user_id, args.chat_id)
        if not recipient.allowed(settings):
            raise AccountAccessError()
        with database(settings) as db, digest_runtime(settings, db) as runtime:
            result = asyncio.run(run_job(runtime.digest, recipient, window))
        if result.state != "confirmed":
            raise SystemExit(1)
    except (ConfigurationError, AccountAccessError, ValueError):
        raise SystemExit("摘要配置无效或目标私聊未获授权。") from None
    except Exception:
        raise SystemExit("摘要执行中断，请检查运行状态并重试同一批次。") from None
