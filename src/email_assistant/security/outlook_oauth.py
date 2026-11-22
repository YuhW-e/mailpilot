import hashlib
import json
import secrets
from collections.abc import Callable
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
import jwt
import msal  # type: ignore[import-untyped]
from pydantic import SecretStr

from email_assistant.email.provider import (
    Authorization,
    Credentials,
    ErrorCode,
    Identity,
    ProviderError,
)

SCOPES = ("https://graph.microsoft.com/Mail.Read",)
CALLBACK_PATH = "/oauth/callback"


class OutlookOAuth:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        *,
        http_client: Any = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.tenant = str(UUID(tenant_id))
        self.client_id = str(UUID(client_id))
        self.authority = f"https://login.microsoftonline.com/{self.tenant}"
        self._http = http_client
        self._clock = clock
        self._lock = Lock()
        self._pending: tuple[dict[str, Any], float, Any, Any] | None = None

    def _application(self, cache: Any) -> Any:
        return msal.PublicClientApplication(
            self.client_id,
            authority=self.authority,
            token_cache=cache,
            http_client=self._http,
            timeout=10,
            enable_pii_log=False,
            instance_discovery=False,
        )

    def _verified_claims(self, token: str) -> dict[str, Any]:
        issuer = f"{self.authority}/v2.0"
        url = f"{self.authority}/discovery/v2.0/keys"
        response = (
            self._http.get(url, timeout=5)
            if self._http is not None
            else httpx.get(url, timeout=5, trust_env=False)
        )
        response.raise_for_status()
        kid = jwt.get_unverified_header(token)["kid"]
        keys = [
            key
            for key in response.json()["keys"]
            if key.get("kid") == kid
            and key.get("kty") == "RSA"
            and key.get("use", "sig") == "sig"
            and key.get("issuer", issuer).replace("{tenantid}", self.tenant) == issuer
        ]
        if len(keys) != 1:
            raise ValueError
        return jwt.decode(
            token,
            key=jwt.PyJWK.from_dict(keys[0], algorithm="RS256"),
            algorithms=["RS256"],
            audience=self.client_id,
            issuer=issuer,
            options={"require": ["aud", "exp", "iat", "iss", "nonce", "sub", "tid", "oid"]},
        )

    def begin_authorization(self, redirect_uri: str) -> Authorization:
        try:
            parts = urlsplit(redirect_uri)
            if (
                parts.scheme != "http"
                or parts.netloc != f"127.0.0.1:{parts.port}"
                or not parts.port
                or parts.path != CALLBACK_PATH
                or parts.query
                or parts.fragment
            ):
                raise ValueError
            cache = msal.SerializableTokenCache()
            app = self._application(cache)
            state = secrets.token_urlsafe(32)
            flow = app.initiate_auth_code_flow(
                list(SCOPES),
                redirect_uri=redirect_uri,
                state=state,
                prompt="select_account",
                response_mode="form_post",
            )
            url = flow["auth_uri"]
        except Exception:
            raise ProviderError(ErrorCode.AUTHORIZATION) from None
        with self._lock:
            self._pending = (flow, self._clock() + 300, app, cache)
        return Authorization(url, state)

    def complete_authorization(
        self, callback_uri: str, expected: Identity | None = None
    ) -> Credentials:
        try:
            parts = urlsplit(callback_uri)
            query = parse_qs(parts.query, strict_parsing=True, max_num_fields=12)
            with self._lock:
                if self._pending is None:
                    raise ValueError
                flow, expiration, app, cache = self._pending
                if (
                    self._clock() >= expiration
                    or parts.fragment
                    or parts._replace(query="", fragment="").geturl() != flow["redirect_uri"]
                    or any(len(values) != 1 for values in query.values())
                    or len(query.get("state", [])) != 1
                    or not query["state"][0].isascii()
                    or not secrets.compare_digest(query["state"][0], flow["state"])
                ):
                    raise ValueError
                self._pending = None
            if "error" in query or not query.get("code"):
                raise ValueError
            result = app.acquire_token_by_auth_code_flow(
                flow, {"code": query["code"][0], "state": query["state"][0]}
            )
            claims = self._verified_claims(result["id_token"])
            if (
                str(UUID(claims["tid"])) != self.tenant
                or claims["nonce"] != hashlib.sha256(flow["nonce"].encode("ascii")).hexdigest()
            ):
                raise ValueError
            identity = Identity("outlook", self.tenant, str(UUID(claims["oid"])))
            if expected is not None and identity != expected:
                raise ValueError
            serialized = cache.serialize()
            if len(json.loads(serialized).get("RefreshToken", {})) != 1:
                raise ValueError
            token = result["access_token"]
            if not isinstance(token, str) or not token:
                raise ValueError
            return Credentials(identity, SecretStr(token), SecretStr(serialized))
        except Exception:
            raise ProviderError(ErrorCode.AUTHORIZATION) from None

    def refresh(self, identity: Identity, cache: SecretStr, *, force: bool = False) -> Credentials:
        if identity.provider != "outlook" or identity.issuer_tenant != self.tenant:
            raise ProviderError(ErrorCode.REAUTHORIZE)
        try:
            token_cache = msal.SerializableTokenCache()
            token_cache.deserialize(cache.get_secret_value())
            app = self._application(token_cache)
            accounts = [
                account
                for account in app.get_accounts()
                if account.get("local_account_id") == identity.subject
                and account.get("realm") == self.tenant
            ]
            if len(accounts) != 1 or len(app.get_accounts()) != 1:
                raise ProviderError(ErrorCode.REAUTHORIZE)
            result = app.acquire_token_silent_with_error(
                list(SCOPES), account=accounts[0], force_refresh=force
            )
            if not result or result.get("error") in {"invalid_grant", "interaction_required"}:
                raise ProviderError(ErrorCode.REAUTHORIZE)
            if not result.get("access_token"):
                raise ProviderError(ErrorCode.TEMPORARY)
            return Credentials(
                identity,
                SecretStr(result["access_token"]),
                SecretStr(token_cache.serialize()) if token_cache.has_state_changed else cache,
            )
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(ErrorCode.TEMPORARY) from None
