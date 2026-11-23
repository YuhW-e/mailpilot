import argparse
import asyncio
import re
from datetime import datetime
from time import monotonic

from email_assistant.ai.provider import LiteLLMProvider, LLMProvider
from email_assistant.config import ConfigurationError, Role, load_settings
from email_assistant.pipeline.digest import DigestCoordinator, DigestRun, Window
from email_assistant.pipeline.digest_job import DigestJobs
from email_assistant.runtime import database, digest_runtime
from email_assistant.storage.firestore import AccountAccessError
from email_assistant.telegram.delivery import Recipient


async def run_job(
    coordinator: DigestCoordinator,
    recipient: Recipient,
    window: Window,
    *,
    manual_id: str | None = None,
    model: LLMProvider | None = None,
) -> DigestRun:
    started = monotonic()
    initial_sync_retries = coordinator.sync.retries
    result = await coordinator.run(recipient, window, manual_id=manual_id)
    sync_retries = coordinator.sync.retries - initial_sync_retries
    model_requests = model.requests if isinstance(model, LiteLLMProvider) else 0
    model_retries = model.retries if isinstance(model, LiteLLMProvider) else 0
    print(
        f"batch={result.batch_id[:10]} state={result.state} "
        f"accounts={result.accounts} pages={result.pages} emails={result.emails} "
        f"pending_accounts={result.pending_accounts} "
        f"sync_retries={sync_retries} model_requests={model_requests} "
        f"model_retries={model_retries} seconds={monotonic() - started:.2f}"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id")
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
        if args.batch_id is not None:
            if not re.fullmatch(r"[a-f0-9]{64}", args.batch_id) or any(
                value is not None for key, value in vars(args).items() if key != "batch_id"
            ):
                parser.error("无效的摘要批次 ID。")
            with database(settings) as db:
                jobs = DigestJobs(db)
                execution = jobs.claim(args.batch_id, settings.hard_timeout_seconds)
                if execution is None:
                    return
                if not execution.recipient.allowed(settings):
                    jobs.finish(execution, DigestRun(execution.batch_id, "failed"))
                    raise AccountAccessError()
                with digest_runtime(settings, db) as runtime:
                    result = asyncio.run(
                        run_job(
                            runtime.digest,
                            execution.recipient,
                            execution.window,
                            model=runtime.model,
                        )
                    )
                jobs.finish(execution, result)
            return
        if not all(value is not None for key, value in vars(args).items() if key != "batch_id"):
            parser.error("摘要需要 --user-id、--chat-id、--at 和 --slot。")
        window = Window.scheduled(args.at, args.slot)
        recipient = Recipient(args.user_id, args.chat_id)
        if not recipient.allowed(settings):
            raise AccountAccessError()
        with database(settings) as db, digest_runtime(settings, db) as runtime:
            result = asyncio.run(
                run_job(
                    runtime.digest,
                    recipient,
                    window,
                    model=runtime.model,
                )
            )
        if result.state != "confirmed":
            raise SystemExit(1)
    except (ConfigurationError, AccountAccessError, ValueError):
        raise SystemExit("摘要配置无效或目标私聊未获授权。") from None
    except Exception:
        raise SystemExit("摘要执行中断，请检查运行状态并重试同一批次。") from None
