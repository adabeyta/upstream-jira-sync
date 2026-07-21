from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

import upstream_jira_sync
from upstream_jira_sync.config import AppConfig, LLMSettings
from upstream_jira_sync.digest import (
    DigestEvent,
    DigestReport,
    render_markdown,
    run_digest,
)
from upstream_jira_sync.emailer import GmailNotifier
from upstream_jira_sync.http import BaseHTTPClient
from upstream_jira_sync.models import TeamMember
from upstream_jira_sync.orchestrator import SyncOrchestrator
from upstream_jira_sync.review_activity import MemberReviewStats, render_review_section
from upstream_jira_sync.state import SyncState

ROSTER_EMAILS = ["octocat@example.com", "hubot@example.com"]


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _write_state(tmp_path, events: list[dict]) -> str:
    path = tmp_path / "sync_state.json"
    path.write_text(json.dumps({"version": 1, "digest": {"events": events}}))
    return str(path)


def _config(**overrides) -> AppConfig:
    base = dict(
        team=[
            TeamMember(github="octocat", jira_email=ROSTER_EMAILS[0]),
            TeamMember(github="hubot", jira_email=ROSTER_EMAILS[1]),
        ],
        github_token="gh_token",
        github_repo=["exampleorg/widgets"],
        jira_url="https://jira.test",
        jira_email="bot@example.com",
        jira_token="jtok",
        jira_project_key="PROJ",
        llm=LLMSettings(provider="anthropic", model="test-model"),
        digest_enabled=True,
        digest_include_manual=False,
        digest_include_reviews=False,
        digest_repo="owner/repo",
        digest_category_slug="announcements",
    )
    base.update(overrides)
    return AppConfig(**base)


class TestNoEmailsInLogs:
    def test_digest_run_never_logs_roster_email(self, tmp_path, caplog):
        path = _write_state(
            tmp_path,
            [
                {
                    "kind": "pr_linked",
                    "observed_at": "2026-04-20T10:00:00+00:00",
                    "ticket_key": "PROJ-10",
                    "pr_url": "https://github.com/o/r/pull/1",
                    "issue_url": "",
                    "github_user": "octocat",
                    "old_value": "",
                    "new_value": "In Review",
                }
            ],
        )
        caplog.set_level(logging.DEBUG)
        with patch("upstream_jira_sync.digest.post_digest_discussion") as mock_post:
            mock_post.return_value = "https://github.com/o/r/discussions/1"
            rc = run_digest(
                config=_config(),
                state_path=path,
                days=7,
                post=True,
                use_ai=False,
                now=_utc(2026, 4, 22),
            )
        assert rc == 0
        assert caplog.records
        for record in caplog.records:
            message = record.getMessage()
            for email in ROSTER_EMAILS:
                assert email not in message

    def test_emailer_never_logs_recipient_address(self, caplog):
        notifier = GmailNotifier(
            from_addr="bot@example.com", base_url="http://mock.invalid"
        )
        response = MagicMock()
        response.status_code = 200
        response.headers = {}
        notifier._session = MagicMock()
        notifier._session.request.return_value = response

        caplog.set_level(logging.DEBUG)
        notifier.send(ROSTER_EMAILS[0], "Low-confidence match on PR #7", "body")

        assert caplog.records
        for record in caplog.records:
            assert ROSTER_EMAILS[0] not in record.getMessage()

    def test_http_error_never_carries_query_params(self, caplog):
        email = ROSTER_EMAILS[0]
        response = MagicMock()
        response.status_code = 400
        response.headers = {}
        response.url = (
            "https://jira.test/rest/api/3/user/search"
            f"?query={email.replace('@', '%40')}&maxResults=1"
        )
        client = BaseHTTPClient()
        client._session = MagicMock()
        client._session.request.return_value = response

        caplog.set_level(logging.DEBUG)
        with pytest.raises(requests.HTTPError) as exc_info:
            try:
                client._request(
                    "GET",
                    "https://jira.test/rest/api/3/user/search",
                    params={"query": email, "maxResults": 1},
                )
            except requests.HTTPError:
                logging.getLogger("orchestrator-site").exception("lookup failed")
                raise

        assert email not in str(exc_info.value)
        assert "%40" not in str(exc_info.value)
        assert "user/search" in str(exc_info.value)
        assert exc_info.value.response is response
        assert email not in caplog.text
        assert "%40" not in caplog.text


def _collect_key_paths(node, key: str, prefix: str = "") -> list[str]:
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{prefix}.{k}" if prefix else k
            if k == key:
                found.append(here)
            found.extend(_collect_key_paths(v, key, here))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found.extend(_collect_key_paths(v, key, f"{prefix}[{i}]"))
    return found


class TestAttributionNamespace:
    def test_core_dedup_entries_store_no_github_user(self, tmp_path):
        path = str(tmp_path / "state.json")
        state = SyncState(path=path)
        state.record_comment(
            "https://github.com/o/r/pull/1", "PROJ-1", "In Review", "high", "url match"
        )
        state.record_estimation("https://github.com/o/r/pull/1", "PROJ-1", 3)
        state.record_issue_classification(
            "https://github.com/o/r/issues/9", "claiming", "author said so"
        )
        state.record_pr_tracked("https://github.com/o/r/pull/2", "PROJ-2")
        state.record_low_conf_ping("https://github.com/o/r/pull/3")
        state.record_pr_state_snapshot(
            "PROJ-1", "open", "https://github.com/o/r/pull/1", "In Review"
        )

        data = json.loads(Path(path).read_text())
        assert _collect_key_paths(data, "github_user") == []

    def test_attribution_lives_only_under_digest_and_only_when_enabled(self, tmp_path):
        path = str(tmp_path / "state.json")
        state = SyncState(path=path)
        orch = SyncOrchestrator.__new__(SyncOrchestrator)
        orch._state = state

        orch._config = SimpleNamespace(digest_enabled=False)
        orch._digest_event("pr_linked", ticket_key="PROJ-1", github_user="octocat")
        assert state.read_digest_events("") == []

        orch._config = SimpleNamespace(digest_enabled=True)
        orch._digest_event("pr_linked", ticket_key="PROJ-1", github_user="octocat")
        events = state.read_digest_events("")
        assert len(events) == 1
        assert events[0]["github_user"] == "octocat"

        data = json.loads(Path(path).read_text())
        paths = _collect_key_paths(data, "github_user")
        assert paths and all(p.startswith("digest.events[") for p in paths)


class TestNoVolumeRanking:
    def test_digest_groups_alphabetically_not_by_event_count(self):
        busy = [
            DigestEvent(
                kind="pr_linked",
                ticket_key=f"PROJ-{n}",
                timestamp=f"2026-04-2{n}T10:00:00+00:00",
                assignee_email="zed",
            )
            for n in range(3)
        ]
        quiet = DigestEvent(
            kind="pr_linked",
            ticket_key="PROJ-9",
            timestamp="2026-04-20T10:00:00+00:00",
            assignee_email="amy",
        )
        report = DigestReport(
            _utc(2026, 4, 15), _utc(2026, 4, 22), tuple(busy) + (quiet,)
        )
        md = render_markdown(report)
        assert md.index("<b>amy</b>") < md.index("<b>zed</b>")

    def test_review_section_alphabetical_not_by_activity(self):
        stats = [
            MemberReviewStats("zed", 9, 40, 8),
            MemberReviewStats("amy", 1, 1, 0),
        ]
        section = render_review_section(stats, "## Review activity")
        assert section.index("| amy ") < section.index("| zed ")


class TestNoTenantLiterals:
    # Tenant-specific needles come from the TENANT_LEAK_PATTERNS env var
    # (space-separated regexes, matched case-insensitively) so this public
    # file never lists the very strings it guards against. Without the var,
    # only the generic Atlassian-host check runs.
    _FORBIDDEN = os.environ.get("TENANT_LEAK_PATTERNS", "").split()
    _INSTANCE_HOST_SUFFIX = ".atlassian" + ".net"
    _PLACEHOLDER_HOST = "yourcompany" + _INSTANCE_HOST_SUFFIX

    def test_package_source_has_no_tenant_literals(self):
        package_dir = Path(upstream_jira_sync.__file__).parent
        files = [
            p
            for p in package_dir.rglob("*")
            if p.suffix in {".py", ".md", ".yaml", ".yml"} and p.is_file()
        ]
        assert files

        violations = []
        for path in files:
            text = path.read_text().lower()
            for needle in self._FORBIDDEN:
                if re.search(needle, text, re.IGNORECASE):
                    violations.append(f"{path}: {needle}")
            stripped = text.replace(self._PLACEHOLDER_HOST, "")
            if self._INSTANCE_HOST_SUFFIX in stripped:
                violations.append(f"{path}: {self._INSTANCE_HOST_SUFFIX}")
        assert violations == []
