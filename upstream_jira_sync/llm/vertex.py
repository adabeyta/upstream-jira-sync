from __future__ import annotations

from upstream_jira_sync.config import LLMSettings
from upstream_jira_sync.http import BaseHTTPClient

try:
    import google.auth
    from google.auth.transport.requests import Request as AuthRequest
except ImportError as exc:
    raise ImportError(
        "llm.provider=vertex needs google-auth; install the vertex extra: "
        'pip install "upstream-jira-sync[vertex]"'
    ) from exc


class VertexProvider(BaseHTTPClient):
    """Calls the model via Google Cloud Vertex AI using Application Default Credentials."""

    def __init__(self, settings: LLMSettings) -> None:
        super().__init__()
        self._model = settings.model
        project = settings.vertex_project
        region = settings.vertex_region
        self._session.headers.update({"content-type": "application/json"})
        if settings.base_url:
            self._url_prefix = (
                f"{settings.base_url}/v1/projects/{project}"
                f"/locations/{region}/publishers/anthropic/models/"
            )
            self._credentials = None
        else:
            self._url_prefix = (
                f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}"
                f"/locations/{region}/publishers/anthropic/models/"
            )
            self._refresh_token()

    def _refresh_token(self) -> None:
        self._credentials, _ = google.auth.default()
        self._credentials.refresh(AuthRequest())
        self._session.headers["Authorization"] = f"Bearer {self._credentials.token}"

    def _ensure_valid_token(self) -> None:
        if self._credentials and not self._credentials.valid:
            self._refresh_token()

    def complete(self, system: str, user_message: str, max_tokens: int = 256) -> str:
        self._ensure_valid_token()
        url = f"{self._url_prefix}{self._model}:rawPredict"
        resp = self._request(
            "POST",
            url,
            json={
                "anthropic_version": "vertex-2023-10-16",
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user_message}],
            },
        )
        return resp.json()["content"][0]["text"].strip()
