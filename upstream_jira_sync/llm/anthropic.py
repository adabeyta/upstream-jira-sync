from __future__ import annotations

import os
from typing import Final

from upstream_jira_sync.config import LLMSettings
from upstream_jira_sync.http import BaseHTTPClient

_API_URL: Final[str] = "https://api.anthropic.com/v1/messages"


class AnthropicProvider(BaseHTTPClient):
    """Calls the model via the plain Anthropic Messages API."""

    def __init__(self, settings: LLMSettings) -> None:
        super().__init__()
        self._model = settings.model
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key and not settings.base_url:
            raise ValueError("Environment variable ANTHROPIC_API_KEY is not set")
        self._url = (
            f"{settings.base_url}/v1/messages" if settings.base_url else _API_URL
        )
        self._session.headers.update(
            {
                "content-type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
        )

    def complete(self, system: str, user_message: str, max_tokens: int = 256) -> str:
        resp = self._request(
            "POST",
            self._url,
            json={
                "model": self._model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user_message}],
            },
        )
        return resp.json()["content"][0]["text"].strip()
