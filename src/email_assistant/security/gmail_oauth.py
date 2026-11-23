import hashlib
import json
import secrets
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic, time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from pydantic import SecretStr

from email_assistant.email.provider import (
    Authorization,
    Credentials,
    ErrorCode,
    Identity,
    ProviderError,
    retry_after,
)

ISSUER = "https://accounts.google.com"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES = frozenset({"openid", READ_SCOPE})
CALLBACK_PATH = "/oauth/callback"


class GmailAuthorizationError(ProviderError):
    def __init__(self, stage: str) -> None:
        super().__init__(ErrorCode.AUTHORIZATION)
        self.stage = stage


@dataclass(frozen=True, repr=False)
class _Pending:
    redirect: str
    state: str
    nonce: str
    verifier: str = field(repr=False)
    deadline: float


class GmailOAuth:
    def __init__(
        self, configuration: SecretStr, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        try:
            installed = json.loads(configuration.get_secret_value())["installed"]
            self.client_id = installed["client_id"]
            self._client_secret = installed["client_secret"]
            if (
                not isinstance(self.client_id, str)
                or not self.client_id.endswith(".apps.googleusercontent.com")
                or not isinstance(self._client_secret, str)
                or not self._client_secret
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ProviderError(ErrorCode.AUTHORIZATION) from None
        self._http = httpx.Client(
            transport=transport, timeout=8, follow_redirects=False, trust_env=False
        )
        self._lock = Lock()
        self._pending: _Pending | None = None

    def close(self) -> None:
        self._http.close()

    def begin_authorization(self, redirect_uri: str) -> Authorization:
        try:
            url = urlsplit(redirect_uri)
            if (
                url.scheme != "http"
                or not url.port
                or url.netloc != f"127.0.0.1:{url.port}"
                or url.path != CALLBACK_PATH
                or url.query
                or url.fragment
            ):
                raise ValueError
        except ValueError:
            raise ProviderError(ErrorCode.AUTHORIZATION) from None
        pending = _Pending(
            redirect_uri,
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(48),
            monotonic() + 300,
        )
        with self._lock:
            self._pending = pending
        challenge = urlsafe_b64encode(hashlib.sha256(pending.verifier.encode()).digest())
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(sorted(SCOPES)),
            "access_type": "offline",
            "prompt": "consent select_account",
            "state": pending.state,
            "nonce": pending.nonce,
            "code_challenge": challenge.rstrip(b"=").decode(),
            "code_challenge_method": "S256",
        }
        return Authorization(f"{ISSUER}/o/oauth2/v2/auth?{urlencode(params)}", pending.state)

    def complete_authorization(
        self, callback_uri: str, expected: Identity | None = None
    ) -> Credentials:
        stage = "callback validation"
        try:
            url = urlsplit(callback_uri)
            query = parse_qs(url.query, strict_parsing=True, max_num_fields=12)
            with self._lock:
                pending = self._pending
                if (
                    pending is None
                    or monotonic() >= pending.deadline
                    or url.fragment
                    or url._replace(query="").geturl() != pending.redirect
                    or any(len(values) != 1 for values in query.values())
                    or not query["state"][0].isascii()
                    or not secrets.compare_digest(query["state"][0], pending.state)
                ):
                    raise ValueError
                self._pending = None
            stage = "Google consent" if "error" in query else "code exchange"
            if "error" in query:
                raise ValueError
            data = self._token(
                grant_type="authorization_code",
                code=query["code"][0],
                redirect_uri=pending.redirect,
                code_verifier=pending.verifier,
            )
            stage = "Google identity verification"
            identity = self._identity(data["id_token"], pending.nonce)
            if expected is not None and identity != expected:
                raise ValueError
            stage = "granted scopes and credentials"
            return self._credentials(identity, data)
        except (KeyError, TypeError, ValueError, httpx.HTTPError, jwt.PyJWTError, ProviderError):
            raise GmailAuthorizationError(stage) from None

    def refresh(self, identity: Identity, cache: SecretStr, *, force: bool = False) -> Credentials:
        try:
            if identity.provider != "gmail" or identity.issuer_tenant != ISSUER:
                raise ValueError
            data = json.loads(cache.get_secret_value())
            refresh = data["refresh_token"]
            if (
                not isinstance(refresh, str)
                or not refresh
                or not isinstance(data["access_token"], str)
                or not data["access_token"]
                or type(data["expires_at"]) is not int
            ):
                raise ValueError
            if not force and data["expires_at"] > time() + 60:
                return Credentials(identity, SecretStr(data["access_token"]), cache)
            result = self._token(grant_type="refresh_token", refresh_token=refresh)
            return self._credentials(identity, result, refresh=refresh)
        except (KeyError, TypeError, ValueError):
            raise ProviderError(ErrorCode.REAUTHORIZE) from None

    def _identity(self, token: str, nonce: str) -> Identity:
        kid = jwt.get_unverified_header(token)["kid"]
        response = self._http.get(JWKS_URL)
        response.raise_for_status()
        keys = [
            key
            for key in response.json()["keys"]
            if key["kid"] == kid and key["kty"] == "RSA" and key.get("use", "sig") == "sig"
        ]
        if len(keys) != 1:
            raise ValueError
        claims = jwt.decode(
            token,
            jwt.PyJWK.from_dict(keys[0], algorithm="RS256"),
            algorithms=["RS256"],
            issuer=(ISSUER, "accounts.google.com"),
            audience=self.client_id,
            options={"require": ["sub", "aud", "exp", "iat", "iss", "nonce"]},
        )
        if (
            not claims["sub"]
            or claims["nonce"] != nonce
            or claims.get("azp", self.client_id) != self.client_id
        ):
            raise ValueError
        return Identity("gmail", ISSUER, claims["sub"])

    def _token(self, **fields: str) -> dict[str, Any]:
        try:
            response = self._http.post(
                TOKEN_URL,
                data=fields | {"client_id": self.client_id, "client_secret": self._client_secret},
            )
            if response.status_code == 429 or response.status_code >= 500:
                code = ErrorCode.THROTTLED if response.status_code == 429 else ErrorCode.TEMPORARY
                raise ProviderError(
                    code, retry_after=retry_after(response.headers.get("Retry-After"))
                )
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            if response.is_error:
                code = (
                    ErrorCode.REAUTHORIZE
                    if result.get("error") == "invalid_grant"
                    else ErrorCode.AUTHORIZATION
                )
                raise ProviderError(code)
            if not response.is_success:
                raise ValueError
            return result
        except httpx.TransportError:
            raise ProviderError(ErrorCode.TEMPORARY) from None
        except ValueError:
            raise ProviderError(ErrorCode.INVALID_RESPONSE) from None

    @staticmethod
    def _credentials(
        identity: Identity, data: dict[str, Any], *, refresh: str | None = None
    ) -> Credentials:
        scope = data.get("scope")
        if not isinstance(scope, str) and (refresh is None or "scope" in data):
            raise ValueError
        scopes = set(scope.split()) if scope is not None else SCOPES
        if (
            READ_SCOPE not in scopes
            or not scopes <= SCOPES
            or str(data.get("token_type", "")).lower() != "bearer"
        ):
            raise ValueError
        access = data["access_token"]
        refresh = data.get("refresh_token", refresh)
        expires = data["expires_in"]
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not refresh
            or type(expires) is not int
            or not 0 < expires <= 86400
        ):
            raise ValueError
        cache = json.dumps(
            {
                "access_token": access,
                "refresh_token": refresh,
                "expires_at": int(time()) + expires,
            }
        )
        return Credentials(identity, SecretStr(access), SecretStr(cache))
