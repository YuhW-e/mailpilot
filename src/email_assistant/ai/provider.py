import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from time import monotonic
from typing import Protocol

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from email_assistant.config import Role, Settings
from email_assistant.security.secrets import SecretReader, SecretReadError

_BASE = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "anthropic": "https://api.anthropic.com/v1/messages",
}


class ModelErrorCode(StrEnum):
    THROTTLED = "throttled"
    TIMEOUT = "timeout"
    TEMPORARY = "temporarily_unavailable"
    CONFIGURATION = "configuration"
    INVALID_OUTPUT = "invalid_output"


class ModelError(RuntimeError):
    def __init__(self, code: ModelErrorCode, retry_after: float = 0) -> None:
        super().__init__(code.value)
        self.code = code
        self.retry_after = retry_after


class LLMProvider(Protocol):
    async def complete[T: BaseModel](
        self, instructions: str, text: str, schema: type[T], *, deadline: float
    ) -> T: ...


def _retry_after(value: str) -> float:
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, delay)


class LiteLLMProvider:
    def __init__(
        self,
        settings: Settings,
        secrets: SecretReader,
        *,
        test_key: SecretStr | None = None,
        test_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if settings.role == Role.INGRESS or (
            settings.environment != "test" and (test_key is not None or test_transport is not None)
        ):
            raise ModelError(ModelErrorCode.CONFIGURATION)
        self._settings = settings
        self._secrets = secrets
        self._test_key = test_key if test_key is not None else SecretStr("fixture")
        self._test_transport = test_transport

    async def complete[T: BaseModel](
        self, instructions: str, text: str, schema: type[T], *, deadline: float
    ) -> T:
        settings = self._settings
        deadline = min(deadline, monotonic() + settings.model_budget_seconds)
        system = f"{instructions}\nJSON schema:\n{json.dumps(schema.model_json_schema())}"
        if len(text.encode()) > settings.model_input_bytes or len(system.encode()) > 12000:
            raise ModelError(ModelErrorCode.CONFIGURATION)

        error = ModelError(ModelErrorCode.TIMEOUT)
        for attempt in range(settings.model_max_attempts):
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            try:
                async with asyncio.timeout(min(settings.model_timeout_seconds, remaining)):
                    key = await self._key()
                    content = await self._request(
                        system, text, key, min(remaining, settings.model_timeout_seconds)
                    )
                if len(content) > 6000 or key.get_secret_value() in content:
                    raise ModelError(ModelErrorCode.INVALID_OUTPUT)
                return schema.model_validate_json(content, strict=True)
            except TimeoutError:
                error = ModelError(ModelErrorCode.TIMEOUT)
            except ValidationError:
                error = ModelError(ModelErrorCode.INVALID_OUTPUT)
            except ModelError as exc:
                error = exc
                if error.code == ModelErrorCode.CONFIGURATION:
                    break
            delay = max(error.retry_after, 0.25 * 2**attempt)
            if attempt + 1 >= settings.model_max_attempts or monotonic() + delay >= deadline:
                break
            await asyncio.sleep(delay)
        raise error from None

    async def _key(self) -> SecretStr:
        settings = self._settings
        if settings.environment == "test":
            if not settings.model_endpoint and self._test_transport is None:
                raise ModelError(ModelErrorCode.CONFIGURATION)
            return self._test_key
        if not settings.model_api_key_version:
            raise ModelError(ModelErrorCode.CONFIGURATION)
        try:
            return await asyncio.to_thread(self._secrets.read, settings.model_api_key_version)
        except SecretReadError:
            raise ModelError(ModelErrorCode.CONFIGURATION) from None

    async def _request(self, system: str, text: str, key: SecretStr, timeout: float) -> str:
        settings = self._settings
        provider = settings.model_provider if not settings.model_endpoint else "openai"
        model = settings.model_name or ("fixture" if settings.model_endpoint else None)
        api_key = key.get_secret_value()
        if not model or not api_key:
            raise ModelError(ModelErrorCode.CONFIGURATION)
        base = str(settings.model_endpoint or _BASE[provider]).rstrip("/")
        origin = httpx.URL(base)
        path = {
            "openai": f"{origin.path.rstrip('/')}/chat/completions",
            "gemini": f"{origin.path.rstrip('/')}/models/{model}:generateContent",
            "anthropic": origin.path,
        }[provider]
        sent = False
        status = 0
        retry_after = 0.0

        async def check_request(request: httpx.Request) -> None:
            nonlocal sent
            url = request.url
            if (
                sent
                or request.method != "POST"
                or url.path != path
                or url.query
                or (url.scheme, url.host, url.port) != (origin.scheme, origin.host, origin.port)
            ):
                raise ModelError(ModelErrorCode.CONFIGURATION)
            sent = True

        async def record_status(response: httpx.Response) -> None:
            nonlocal status, retry_after
            status = response.status_code
            retry_after = _retry_after(response.headers.get("Retry-After", ""))

        # Load the bundled SDK catalogs without background downloads, and suppress SDK traces.
        os.environ["LITELLM_OFFLINE_MODE"] = "true"
        for namespace in ("LiteLLM", "LiteLLM Router", "openai._base_client"):
            logging.getLogger(namespace).disabled = True

        import litellm
        from litellm.exceptions import APIConnectionError, RateLimitError, Timeout
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
        from openai import AsyncOpenAI

        try:
            handler = AsyncHTTPHandler(
                timeout=timeout,
                follow_redirects=False,
                transport=self._test_transport
                or httpx.AsyncHTTPTransport(retries=0, trust_env=False),
                event_hooks={"request": [check_request], "response": [record_status]},
            )
            async with handler.client as http:
                client: AsyncOpenAI | AsyncHTTPHandler = handler
                if provider == "openai":
                    client = AsyncOpenAI(
                        base_url=base, api_key=api_key, http_client=http, max_retries=0
                    )
                response = await litellm.acompletion(
                    model=f"{provider}/{model}",
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": text},
                    ],
                    api_base=base,
                    api_key=api_key,
                    client=client,
                    max_tokens=settings.model_output_tokens,
                    timeout=timeout,
                    num_retries=0,
                    max_retries=0,
                    caching=False,
                    stream=False,
                    turn_off_message_logging=True,
                    **{"no-log": True},
                )
        except Exception as exc:
            if isinstance(exc, (TimeoutError, httpx.TimeoutException, Timeout)):
                code = ModelErrorCode.TIMEOUT
            elif status == 429 or isinstance(exc, RateLimitError):
                code = ModelErrorCode.THROTTLED
            elif status >= 500 or isinstance(exc, (httpx.TransportError, APIConnectionError)):
                code = ModelErrorCode.TEMPORARY
            elif 200 <= status < 300:
                code = ModelErrorCode.INVALID_OUTPUT
            else:
                code = ModelErrorCode.CONFIGURATION
            raise ModelError(code, retry_after) from None

        if not isinstance(response, litellm.ModelResponse) or len(response.choices) != 1:
            raise ModelError(ModelErrorCode.INVALID_OUTPUT)
        choice = response.choices[0]
        message = getattr(choice, "message", None)
        if (
            choice.finish_reason != "stop"
            or message is None
            or message.tool_calls
            or message.function_call
            or not isinstance(message.content, str)
        ):
            raise ModelError(ModelErrorCode.INVALID_OUTPUT)
        return message.content
