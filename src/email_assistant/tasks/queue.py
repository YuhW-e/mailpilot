import json
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2
from google.protobuf.timestamp_pb2 import Timestamp  # type: ignore[import-untyped]

from email_assistant.config import ConfigurationError, Settings
from email_assistant.email.provider import stable_key


class QueueUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("任务队列暂时不可用。")


class TaskQueue(Protocol):
    def enqueue(self, task_id: str, generation: int, at: datetime) -> None: ...


@dataclass(frozen=True)
class CloudTaskQueue:
    client: tasks_v2.CloudTasksClient
    queue: str
    worker: str
    invoker: str

    @classmethod
    def configured(cls, settings: Settings) -> "CloudTaskQueue":
        path, url, invoker = (
            settings.task_queue_path,
            settings.task_worker_url,
            settings.task_invoker_email,
        )
        if path is None or url is None or invoker is None:
            raise ConfigurationError("请配置 Cloud Tasks 队列和 Worker 身份。")
        return cls(tasks_v2.CloudTasksClient(), path, str(url).rstrip("/"), invoker)

    def enqueue(self, task_id: str, generation: int, at: datetime) -> None:
        name = stable_key(task_id, str(generation))
        timestamp = Timestamp()
        timestamp.FromDatetime(at)
        try:
            self.client.create_task(
                request=tasks_v2.CreateTaskRequest(
                    parent=self.queue,
                    task=tasks_v2.Task(
                        name=f"{self.queue}/tasks/{name}",
                        schedule_time=timestamp,
                        http_request=tasks_v2.HttpRequest(
                            url=f"{self.worker}/internal/telegram/tasks",
                            http_method=tasks_v2.HttpMethod.POST,
                            headers={"Content-Type": "application/json"},
                            body=json.dumps({"task_id": task_id}).encode(),
                            oidc_token=tasks_v2.OidcToken(
                                service_account_email=self.invoker,
                                audience=self.worker,
                            ),
                        ),
                    ),
                ),
                retry=None,
                timeout=2,
            )
        except AlreadyExists:
            return
        except Exception:
            raise QueueUnavailable() from None
