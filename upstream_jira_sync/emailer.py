"""Gmail API notifier for low-confidence pings. Auths via the GCP service account."""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage
from typing import Any

from upstream_jira_sync.http import BaseHTTPClient

log = logging.getLogger(__name__)

_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


class GmailNotifier(BaseHTTPClient):
    """Sends mail through the Gmail API as the service account."""

    def __init__(self, from_addr: str = "", base_url: str = "") -> None:
        super().__init__()
        self._from = from_addr
        self._send_url = (
            f"{base_url}/gmail/v1/users/me/messages/send"
            if base_url
            else _GMAIL_SEND_URL
        )
        self._credentials: Any = None
        if not base_url:
            self._refresh_token()

    def _refresh_token(self) -> None:
        import google.auth
        from google.auth.transport.requests import Request as AuthRequest

        self._credentials, _ = google.auth.default(scopes=[_GMAIL_SCOPE])
        self._credentials.refresh(AuthRequest())
        self._session.headers["Authorization"] = f"Bearer {self._credentials.token}"

    def _ensure_valid_token(self) -> None:
        if self._credentials and not self._credentials.valid:
            self._refresh_token()

    def send(self, to: str, subject: str, body: str) -> None:
        self._ensure_valid_token()
        msg = EmailMessage()
        if self._from:
            msg["From"] = self._from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        self._request("POST", self._send_url, json={"raw": raw})
        log.info("  Sent email: %s", subject)
