"""Manage the local Outlook authorization for one MailPilot account."""

import argparse
import logging
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import monotonic
from typing import Any

from google.cloud import firestore

from email_assistant.email.outlook import OutlookProvider
from email_assistant.email.provider import Credentials, ErrorCode, Identity, ProviderError
from email_assistant.security.account_secrets import SecretManagerAccountSecrets
from email_assistant.security.credentials import CredentialCoordinator
from email_assistant.security.outlook_oauth import CALLBACK_PATH, OutlookOAuth
from email_assistant.storage.firestore import AccountKey, FirestoreRepository


def browser_grant(provider: OutlookProvider, port: int, expected: Identity | None) -> Credentials:
    result: Credentials | None = None

    class Callback(BaseHTTPRequestHandler):
        def setup(self) -> None:
            self.request.settimeout(5)
            super().setup()

        def log_message(self, format: str, *args: Any) -> None:
            # BaseHTTPRequestHandler normally logs the authorization code in the request path.
            pass

        def do_POST(self) -> None:
            nonlocal result
            accepted = False
            try:
                fields = self.headers.get_all("Content-Length") or []
                length = int(fields[0]) if len(fields) == 1 and fields[0].isascii() else 0
                if (
                    self.headers.get_all("Host") == [host]
                    and self.path == CALLBACK_PATH
                    and self.headers.get_content_type() == "application/x-www-form-urlencoded"
                    and not self.headers.get_all("Transfer-Encoding")
                    and 0 < length <= 8192
                ):
                    payload = self.rfile.read(length)
                    if len(payload) != length:
                        raise ValueError
                    response = payload.decode("ascii")
                    result = provider.complete_authorization(
                        f"http://{host}{CALLBACK_PATH}?{response}", expected
                    )
                    accepted = True
            except (OSError, ValueError, ProviderError):
                pass
            body = (
                b"Outlook identity verified. Check the terminal to confirm registration."
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
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--account", help="Account ID for reauthorization, rotation or revocation")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.action != "authorize" and not args.account:
        parser.error("--account is required for rotate and revoke")
    if os.environ.get("FIRESTORE_EMULATOR_HOST"):
        parser.error("Run the authorization CLI with the production Firestore configuration.")
    logging.disable(logging.CRITICAL)
    try:
        oauth = OutlookOAuth(args.tenant, args.client_id)
        db = FirestoreRepository(firestore.Client(project=args.project))
        secrets = SecretManagerAccountSecrets(args.project)
        provider = OutlookProvider(oauth)
        try:
            coordinator = CredentialCoordinator(db, secrets, provider)
            account = AccountKey(args.owner, args.account) if args.account else None
            expected = coordinator.identity(db.account(account)) if account else None
            if args.action == "authorize":
                grant = browser_grant(provider, args.port, expected)
                account = db.register(args.owner, grant.identity)
                secrets.ensure(account.account_id)
                coordinator.authorize(account, grant)
                print(f"Outlook account authorized: {account.account_id}")
            elif account and args.action == "rotate":
                coordinator.rotate(account)
                print("Outlook credential rotated.")
            elif account:
                coordinator.revoke(account)
                print("MailPilot access to Outlook revoked.")
        finally:
            provider.close()
    except ProviderError as exc:
        print(f"Outlook authorization command failed ({exc.code}).", file=sys.stderr)
        return 1
    except Exception:
        print("Outlook authorization command failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
