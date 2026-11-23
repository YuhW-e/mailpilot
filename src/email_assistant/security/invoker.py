from functools import partial

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token


def verified_invoker(authorization: str, *, audience: str, email: str | None) -> bool:
    method, _, token = authorization.partition(" ")
    if method.lower() != "bearer" or not token or not audience or not email:
        return False
    try:
        claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            token,
            partial(GoogleRequest(), timeout=3),
            audience=audience,
        )
    except Exception:
        return False
    return claims.get("email") == email and claims.get("email_verified") is True
