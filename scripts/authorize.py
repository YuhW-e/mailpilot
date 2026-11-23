"""Manage the local email authorization for one MailPilot account."""

import argparse
import logging
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from google.cloud import firestore
from pydantic import SecretStr

from email_assistant.email.gmail import GmailProvider
from email_assistant.email.outlook import OutlookProvider
from email_assistant.email.provider import Credentials, ErrorCode, Identity, ProviderError
from email_assistant.security.account_secrets import SecretManagerAccountSecrets
from email_assistant.security.credentials import CredentialCoordinator
from email_assistant.security.gmail_oauth import GmailAuthorizationError, GmailOAuth
from email_assistant.security.outlook_oauth import CALLBACK_PATH, OutlookOAuth
from email_assistant.storage.firestore import AccountKey, FirestoreRepository


def browser_grant(
    provider: OutlookProvider | GmailProvider, port: int, expected: Identity | None
) -> Credentials:
    result: Credentials | None = None

    class Callback(BaseHTTPRequestHandler):
        def setup(self) -> None:
            self.request.settimeout(5)
            super().setup()

        def log_message(self, format: str, *args: Any) -> None:
            # BaseHTTPRequestHandler normally logs the authorization code in the request path.
            pass

        def do_GET(self) -> None:
            if isinstance(provider, GmailProvider) and len(self.path) <= 8192:
                self.accept(self.path)
            else:
                self.accept("")

        def do_POST(self) -> None:
            try:
                fields = self.headers.get_all("Content-Length") or []
                length = int(fields[0]) if len(fields) == 1 and fields[0].isascii() else 0
                if (
                    isinstance(provider, OutlookProvider)
                    and self.path == CALLBACK_PATH
                    and self.headers.get_content_type() == "application/x-www-form-urlencoded"
                    and not self.headers.get_all("Transfer-Encoding")
                    and 0 < length <= 8192
                ):
                    payload = self.rfile.read(length)
                    if len(payload) == length:
                        self.accept(f"{CALLBACK_PATH}?{payload.decode('ascii')}")
                        return
            except (OSError, ValueError):
                pass
            self.accept("")

        def accept(self, target: str) -> None:
            nonlocal result
            accepted = False
            try:
                if (
                    self.headers.get_all("Host") == [host]
                    and urlsplit(target).path == CALLBACK_PATH
                    and target.startswith(f"{CALLBACK_PATH}?")
                ):
                    result = provider.complete_authorization(f"http://{host}{target}", expected)
                    accepted = True
            except GmailAuthorizationError as exc:
                print(f"Gmail callback rejected during {exc.stage}.", file=sys.stderr, flush=True)
            except (OSError, ValueError, ProviderError):
                pass
            body = (
                b"Identity verified. Check the terminal to confirm registration."
                if accepted
                else b"Authorization callback rejected."
            )
            self.send_response(200 if accepted else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", port), Callback) as server:
        server.timeout = 0.5
        host = f"127.0.0.1:{server.server_port}"
        flow = provider.begin_authorization(f"http://{host}{CALLBACK_PATH}")
        deadline = monotonic() + 300
        if not webbrowser.open(flow.url, new=1):
            raise ProviderError(ErrorCode.AUTHORIZATION)
        while result is None and monotonic() < deadline:
            server.handle_request()
    if result is None:
        raise ProviderError(ErrorCode.AUTHORIZATION)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("authorize", "rotate", "revoke"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--provider", choices=("outlook", "gmail"), default="outlook")
    parser.add_argument("--tenant")
    parser.add_argument("--client-id")
    parser.add_argument("--gmail-client-file", type=Path, help="Local desktop OAuth client JSON")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--account", help="Account ID for reauthorization, rotation or revocation")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.action != "authorize" and not args.account:
        parser.error("--account is required for rotate and revoke")
    if args.provider == "outlook" and not (args.tenant and args.client_id):
        parser.error("--tenant and --client-id are required for Outlook")
    if args.provider == "gmail" and args.gmail_client_file is None:
        parser.error("--gmail-client-file is required for Gmail")
    port = args.port if args.port is not None else (0 if args.provider == "gmail" else 8765)
    if not 0 <= port <= 65535:
        parser.error("Invalid loopback port")
    if os.environ.get("FIRESTORE_EMULATOR_HOST"):
        parser.error("Run the authorization CLI with the production Firestore configuration.")
    logging.disable(logging.CRITICAL)
    try:
        db = FirestoreRepository(firestore.Client(project=args.project))
        secrets = SecretManagerAccountSecrets(args.project, provider=args.provider)
        provider: OutlookProvider | GmailProvider
        if args.provider == "gmail":
            configuration = SecretStr(args.gmail_client_file.read_text(encoding="utf-8"))
            provider = GmailProvider(GmailOAuth(configuration))
        else:
            provider = OutlookProvider(OutlookOAuth(args.tenant, args.client_id))
        try:
            coordinator = CredentialCoordinator(db, secrets, provider)
            account = AccountKey(args.owner, args.account) if args.account else None
            expected = coordinator.identity(db.account(account)) if account else None
            if expected is not None and expected.provider != args.provider:
                raise ProviderError(ErrorCode.AUTHORIZATION)
            if args.action == "authorize":
                grant = browser_grant(provider, port, expected)
                account = db.register(args.owner, grant.identity)
                secrets.ensure(account.account_id)
                coordinator.authorize(account, grant)
                print(f"{args.provider} account authorized: {account.account_id}")
            elif account and args.action == "rotate":
                coordinator.rotate(account)
                print(f"{args.provider} credential rotated.")
            elif account:
                coordinator.revoke(account)
                print(f"MailPilot access to {args.provider} revoked.")
        finally:
            provider.close()
    except ProviderError as exc:
        print(f"{args.provider} authorization command failed ({exc.code}).", file=sys.stderr)
        return 1
    except Exception:
        print(f"{args.provider} authorization command failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
