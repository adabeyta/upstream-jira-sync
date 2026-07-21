from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from upstream_jira_sync.ai import WeeklyDigestSummarizer
from upstream_jira_sync.config import AppConfig, LLMSettings
from upstream_jira_sync.digest import (
    DigestEvent,
    DigestReport,
    aggregate,
    events_to_json,
    jira_changes_to_events,
    post_digest_discussion,
    read_bot_events,
    render_markdown,
    run_digest,
    window_for,
)
from upstream_jira_sync.jira import JiraClient
from upstream_jira_sync.models import JiraTicketChange, TeamMember
from upstream_jira_sync.skill_loader import SkillLoader


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


# State-file tests need timestamps inside the STATE_TTL_DAYS pruning window,
# so they must be relative to now (fixed dates age out and start failing).
_NOW = datetime.now(timezone.utc)
_RECENT = (_NOW - timedelta(days=2)).isoformat()
_WINDOW_START = _NOW - timedelta(days=7)


def _event(kind: str, observed_at: str, **fields) -> dict:
    entry = {
        "kind": kind,
        "observed_at": observed_at,
        "ticket_key": "",
        "pr_url": "",
        "issue_url": "",
        "github_user": "",
        "old_value": "",
        "new_value": "",
    }
    entry.update(fields)
    return entry


def _write_state(tmp_path, events: list[dict]) -> str:
    path = tmp_path / "sync_state.json"
    path.write_text(json.dumps({"version": 1, "digest": {"events": events}}))
    return str(path)


class TestReadBotEvents:
    def test_pr_linked_within_window(self, tmp_path):
        path = _write_state(
            tmp_path,
            [
                _event(
                    "pr_linked",
                    _RECENT,
                    ticket_key="PROJ-10",
                    pr_url="https://github.com/o/r/pull/1",
                    new_value="In Review",
                )
            ],
        )
        events = read_bot_events(path, _WINDOW_START)
        assert len(events) == 1
        assert events[0].kind == "pr_linked"
        assert events[0].ticket_key == "PROJ-10"
        assert events[0].new_value == "In Review"

    def test_issue_claimed_uses_issue_url_as_new_value(self, tmp_path):
        path = _write_state(
            tmp_path,
            [
                _event(
                    "issue_claimed",
                    _RECENT,
                    ticket_key="PROJ-42",
                    issue_url="https://github.com/o/r/issues/99",
                )
            ],
        )
        events = read_bot_events(path, _WINDOW_START)
        assert len(events) == 1
        assert events[0].kind == "issue_claimed"
        assert events[0].new_value == "https://github.com/o/r/issues/99"
        assert events[0].ticket_key == "PROJ-42"

    def test_non_bot_kind_excluded(self, tmp_path):
        path = _write_state(
            tmp_path,
            [
                _event(
                    "manual_transition",
                    _RECENT,
                    ticket_key="PROJ-10",
                )
            ],
        )
        assert read_bot_events(path, _WINDOW_START) == []

    def test_event_attributed_via_roster(self, tmp_path):
        path = _write_state(
            tmp_path,
            [
                _event(
                    "pr_linked",
                    _RECENT,
                    ticket_key="PROJ-10",
                    github_user="alice",
                )
            ],
        )
        events = read_bot_events(path, _WINDOW_START, {"alice"})
        assert len(events) == 1
        assert events[0].assignee_email == "alice"

    def test_non_roster_github_user_gets_empty_assignee(self, tmp_path):
        path = _write_state(
            tmp_path,
            [
                _event(
                    "pr_linked",
                    _RECENT,
                    ticket_key="PROJ-10",
                    github_user="ex-employee",
                )
            ],
        )
        events = read_bot_events(path, _WINDOW_START, {"alice"})
        assert events[0].assignee_email == ""


class TestAggregate:
    def test_empty_inputs_produce_empty_report(self):
        report = aggregate([], [], _utc(2026, 4, 15), _utc(2026, 4, 22))
        assert report.is_empty
        assert report.events == ()

    def test_events_sorted_by_timestamp(self):
        a = DigestEvent(
            kind="pr_linked", ticket_key="X-1", timestamp="2026-04-22T10:00:00+00:00"
        )
        b = DigestEvent(
            kind="pr_linked", ticket_key="X-2", timestamp="2026-04-20T10:00:00+00:00"
        )
        report = aggregate([a], [b], _utc(2026, 4, 15), _utc(2026, 4, 22))
        assert [e.ticket_key for e in report.events] == ["X-2", "X-1"]


class TestWindowFor:
    def test_default_seven_days(self):
        now = _utc(2026, 4, 22)
        start, end = window_for(now)
        assert end == now
        assert end - start == timedelta(days=7)


class TestRenderMarkdown:
    def test_empty_report_renders_no_activity(self):
        report = DigestReport(_utc(2026, 4, 15), _utc(2026, 4, 22), ())
        md = render_markdown(report)
        assert "No ticket activity" in md
        assert "2026-04-15" in md and "2026-04-22" in md

    def test_event_row_rendered(self):
        report = DigestReport(
            _utc(2026, 4, 15),
            _utc(2026, 4, 22),
            (
                DigestEvent(
                    kind="pr_linked",
                    ticket_key="PROJ-10",
                    new_value="In Review",
                    timestamp="2026-04-20T10:00:00+00:00",
                ),
            ),
        )
        md = render_markdown(report)
        assert "`PROJ-10`" in md
        assert "In Review" in md
        assert "pr_linked" in md
        assert "| --- |" in md

    def test_narrative_included_as_blockquote(self):
        report = DigestReport(
            _utc(2026, 4, 15),
            _utc(2026, 4, 22),
            (
                DigestEvent(
                    kind="pr_linked",
                    ticket_key="A-1",
                    timestamp="2026-04-20T10:00:00+00:00",
                ),
            ),
        )
        md = render_markdown(report, narrative="One ticket moved to review.")
        assert "> One ticket moved to review." in md

    def test_groups_events_by_assignee(self):
        report = DigestReport(
            _utc(2026, 4, 15),
            _utc(2026, 4, 22),
            (
                DigestEvent(
                    kind="manual_transition",
                    ticket_key="A-1",
                    old_value="X",
                    new_value="Y",
                    timestamp="2026-04-20T10:00:00+00:00",
                    source="jira",
                    assignee_email="alice",
                ),
                DigestEvent(
                    kind="ticket_created",
                    ticket_key="A-2",
                    ticket_summary="new",
                    timestamp="2026-04-21T10:00:00+00:00",
                    source="jira",
                    assignee_email="bob",
                ),
                DigestEvent(
                    kind="issue_claimed",
                    ticket_key="",
                    new_value="https://github.com/o/r/issues/1",
                    timestamp="2026-04-22T10:00:00+00:00",
                ),
            ),
        )
        md = render_markdown(report)
        assert "<details><summary><b>alice</b>" in md
        assert "<details><summary><b>bob</b>" in md
        assert "Unassigned / bot-only" in md
        assert md.index("Unassigned") > md.index("alice")
        assert md.index("Unassigned") > md.index("bob")
        assert md.index("alice") < md.index("bob")
        assert "2 assignee(s)" in md

    def test_manual_source_badge(self):
        report = DigestReport(
            _utc(2026, 4, 15),
            _utc(2026, 4, 22),
            (
                DigestEvent(
                    kind="manual_transition",
                    ticket_key="PROJ-12",
                    old_value="To Do",
                    new_value="In Progress",
                    timestamp="2026-04-20T10:00:00+00:00",
                    source="jira",
                ),
            ),
        )
        md = render_markdown(report)
        assert "_(manual)_" in md
        assert "To Do → In Progress" in md

    def test_manual_points_change_renders_unset_for_blank_old_value(self):
        report = DigestReport(
            _utc(2026, 4, 15),
            _utc(2026, 4, 22),
            (
                DigestEvent(
                    kind="manual_points_change",
                    ticket_key="PROJ-12",
                    old_value="",
                    new_value="5",
                    timestamp="2026-04-20T10:00:00+00:00",
                    source="jira",
                ),
            ),
        )
        md = render_markdown(report)
        assert "(unset) → 5 pts" in md


class TestJiraChangesToEvents:
    def test_created_becomes_ticket_created_event(self):
        change = JiraTicketChange(
            ticket_key="PROJ-1",
            ticket_summary="fix thing",
            field="Created",
            from_value="",
            to_value="fix thing",
            changed_at="2026-04-20T10:00:00+00:00",
            author_email="",
        )
        events = jira_changes_to_events(
            [change], "bot@example.com", "customfield_12345"
        )
        assert len(events) == 1
        assert events[0].kind == "ticket_created"
        assert events[0].source == "jira"
        assert events[0].ticket_summary == "fix thing"

    def test_status_change_becomes_manual_transition(self):
        change = JiraTicketChange(
            ticket_key="PROJ-1",
            ticket_summary="s",
            field="status",
            from_value="To Do",
            to_value="In Progress",
            changed_at="2026-04-20T10:00:00+00:00",
            author_email="dev@example.com",
        )
        events = jira_changes_to_events(
            [change], "bot@example.com", "customfield_12345"
        )
        assert events[0].kind == "manual_transition"
        assert events[0].old_value == "To Do"
        assert events[0].new_value == "In Progress"

    def test_story_points_change_by_name(self):
        change = JiraTicketChange(
            ticket_key="PROJ-1",
            ticket_summary="s",
            field="Story Points",
            from_value="3",
            to_value="5",
            changed_at="2026-04-20T10:00:00+00:00",
            author_email="dev@example.com",
        )
        events = jira_changes_to_events(
            [change], "bot@example.com", "customfield_12345"
        )
        assert events[0].kind == "manual_points_change"

    def test_bot_authored_change_filtered_case_insensitive(self):
        change = JiraTicketChange(
            ticket_key="PROJ-1",
            ticket_summary="s",
            field="status",
            from_value="To Do",
            to_value="In Review",
            changed_at="2026-04-20T10:00:00+00:00",
            author_email="Bot@Example.com",
        )
        assert (
            jira_changes_to_events([change], "bot@example.com", "customfield_12345")
            == []
        )

    def test_unknown_field_ignored(self):
        change = JiraTicketChange(
            ticket_key="PROJ-1",
            ticket_summary="s",
            field="priority",
            from_value="Low",
            to_value="High",
            changed_at="2026-04-20T10:00:00+00:00",
            author_email="dev@example.com",
        )
        assert (
            jira_changes_to_events([change], "bot@example.com", "customfield_12345")
            == []
        )


class TestSearchTicketChanges:
    @staticmethod
    def _client():
        client = JiraClient.__new__(JiraClient)
        client._base = "https://jira.test"
        client._session = MagicMock()
        return client

    @staticmethod
    def _resp(payload):
        r = MagicMock()
        r.status_code = 200
        r.raise_for_status = MagicMock()
        r.json.return_value = payload
        return r

    def test_parses_status_transition(self):
        client = self._client()
        client._session.request.return_value = self._resp(
            {
                "issues": [
                    {
                        "key": "PROJ-1",
                        "fields": {
                            "summary": "fix thing",
                            "created": "2026-01-01T00:00:00+00:00",
                            "assignee": {"emailAddress": "owner@example.com"},
                        },
                        "changelog": {
                            "histories": [
                                {
                                    "created": "2026-04-20T10:00:00+00:00",
                                    "author": {"emailAddress": "dev@example.com"},
                                    "items": [
                                        {
                                            "field": "status",
                                            "fromString": "To Do",
                                            "toString": "In Progress",
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                ]
            }
        )
        changes = client.search_ticket_changes(
            "project = PROJ", "2026-04-15T00:00:00+00:00", "customfield_12345"
        )
        assert len(changes) == 1
        assert changes[0].ticket_key == "PROJ-1"
        assert changes[0].field == "status"
        assert changes[0].from_value == "To Do"
        assert changes[0].to_value == "In Progress"
        assert changes[0].author_email == "dev@example.com"
        assert changes[0].ticket_assignee_email == "owner@example.com"

    def test_parses_ticket_created_when_in_window(self):
        client = self._client()
        client._session.request.return_value = self._resp(
            {
                "issues": [
                    {
                        "key": "PROJ-2",
                        "fields": {
                            "summary": "new ticket",
                            "created": "2026-04-20T10:00:00+00:00",
                        },
                        "changelog": {"histories": []},
                    }
                ]
            }
        )
        changes = client.search_ticket_changes(
            "project = PROJ", "2026-04-15T00:00:00+00:00", "customfield_12345"
        )
        assert len(changes) == 1
        assert changes[0].field == "Created"

    def test_ignores_history_before_window(self):
        client = self._client()
        client._session.request.return_value = self._resp(
            {
                "issues": [
                    {
                        "key": "PROJ-1",
                        "fields": {
                            "summary": "s",
                            "created": "2026-01-01T00:00:00+00:00",
                        },
                        "changelog": {
                            "histories": [
                                {
                                    "created": "2026-04-10T00:00:00+00:00",
                                    "author": {"emailAddress": "dev@example.com"},
                                    "items": [
                                        {
                                            "field": "status",
                                            "fromString": "a",
                                            "toString": "b",
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                ]
            }
        )
        assert (
            client.search_ticket_changes(
                "project = PROJ", "2026-04-15T00:00:00+00:00", "customfield_12345"
            )
            == []
        )

    def test_request_shape(self):
        client = self._client()
        client._session.request.return_value = self._resp({"issues": []})
        client.search_ticket_changes(
            "project = PROJ", "2026-04-15T00:00:00+00:00", "customfield_12345"
        )
        call = client._session.request.call_args
        assert call.args[0] == "GET"
        assert call.args[1].endswith("/rest/api/3/search/jql")
        params = call.kwargs["params"]
        assert params["expand"] == "changelog"
        assert "customfield_12345" in params["fields"]


class _StubProvider:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def complete(self, system: str, user_message: str, max_tokens: int = 256) -> str:
        return self.text


class _FailingProvider:
    def complete(self, system: str, user_message: str, max_tokens: int = 256) -> str:
        raise RuntimeError("boom")


class TestWeeklyDigestSummarizer:
    def test_returns_narrative_text(self, tmp_path):
        (tmp_path / "weekly_digest_summary.md").write_text("You summarize digests.")
        summarizer = WeeklyDigestSummarizer(
            _StubProvider("Two tickets moved to review."),
            SkillLoader(override_dir=str(tmp_path)),
        )
        assert summarizer.summarize('[{"kind":"pr_linked"}]') == (
            "Two tickets moved to review."
        )

    def test_api_failure_returns_empty(self, tmp_path):
        (tmp_path / "weekly_digest_summary.md").write_text("You summarize digests.")
        summarizer = WeeklyDigestSummarizer(
            _FailingProvider(), SkillLoader(override_dir=str(tmp_path))
        )
        assert summarizer.summarize("[]") == ""


class TestEventsToJson:
    def test_serializes_events(self):
        events = [
            DigestEvent(kind="pr_linked", ticket_key="A-1", new_value="In Review"),
            DigestEvent(kind="ticket_created", ticket_key="A-2", source="jira"),
        ]
        out = events_to_json(events)
        parsed = json.loads(out)
        assert len(parsed) == 2
        assert parsed[0]["kind"] == "pr_linked"
        assert parsed[1]["source"] == "jira"


def _digest_config(**overrides) -> AppConfig:
    base = dict(
        team=[TeamMember(github="octocat", jira_email="octocat@example.com")],
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


class TestRunDigest:
    def test_disabled_returns_zero(self, tmp_path):
        cfg = _digest_config(digest_enabled=False)
        rc = run_digest(
            config=cfg,
            state_path=str(tmp_path / "nope.json"),
            days=7,
            post=True,
            use_ai=False,
        )
        assert rc == 0

    def test_empty_window_skips(self, tmp_path):
        cfg = _digest_config()
        path = _write_state(tmp_path, [])
        rc = run_digest(
            config=cfg,
            state_path=path,
            days=7,
            post=False,
            use_ai=False,
            now=_NOW,
        )
        assert rc == 0

    def test_dry_run_prints_markdown_does_not_post(self, tmp_path, capsys):
        cfg = _digest_config()
        path = _write_state(
            tmp_path,
            [
                _event(
                    "pr_linked",
                    _RECENT,
                    ticket_key="PROJ-10",
                    new_value="In Review",
                )
            ],
        )
        with patch("upstream_jira_sync.digest.post_digest_discussion") as mock_post:
            rc = run_digest(
                config=cfg,
                state_path=path,
                days=7,
                post=False,
                use_ai=False,
                now=_NOW,
            )
        out = capsys.readouterr().out
        assert rc == 0
        assert "PROJ-10" in out
        mock_post.assert_not_called()

    def test_post_calls_create_discussion(self, tmp_path):
        cfg = _digest_config()
        path = _write_state(
            tmp_path,
            [
                _event(
                    "pr_linked",
                    _RECENT,
                    ticket_key="PROJ-10",
                    new_value="In Review",
                )
            ],
        )
        with patch("upstream_jira_sync.digest.post_digest_discussion") as mock_post:
            mock_post.return_value = "https://github.com/o/r/discussions/1"
            rc = run_digest(
                config=cfg,
                state_path=path,
                days=7,
                post=True,
                use_ai=False,
                now=_NOW,
            )
        assert rc == 0
        mock_post.assert_called_once()
        kwargs = mock_post.call_args.kwargs
        assert kwargs["repo"] == "owner/repo"
        assert kwargs["category_slug"] == "announcements"
        assert "Weekly digest" in kwargs["title"]

    def test_post_fails_with_missing_config(self, tmp_path):
        cfg = _digest_config(digest_repo="")
        path = _write_state(
            tmp_path,
            [
                _event(
                    "pr_linked",
                    _RECENT,
                    ticket_key="PROJ-10",
                    new_value="In Review",
                )
            ],
        )
        with patch("upstream_jira_sync.digest.post_digest_discussion") as mock_post:
            rc = run_digest(
                config=cfg,
                state_path=path,
                days=7,
                post=True,
                use_ai=False,
                now=_NOW,
            )
        assert rc == 1
        mock_post.assert_not_called()


class TestAppConfigDigestFields:
    def test_load_from_yaml_reads_digest_section(self, tmp_path, monkeypatch):
        (tmp_path / "team_roster.yaml").write_text(
            "- github: octocat\n  jira_email: octocat@example.com\n"
        )
        (tmp_path / "config.yaml").write_text(
            "settings:\n"
            "  github_repo: exampleorg/widgets\n"
            "  jira_url: https://yourcompany.atlassian.net\n"
            "  jira_project_key: PROJ\n"
            "  llm:\n"
            "    provider: anthropic\n"
            "    model: test-model\n"
            "  digest_enabled: true\n"
            "  digest_repo: owner/repo\n"
            "  digest_category_slug: general\n"
        )
        monkeypatch.delenv("ROSTER_YAML", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "t")
        monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
        monkeypatch.setenv("JIRA_API_TOKEN", "jt")

        cfg = AppConfig.load(str(tmp_path / "config.yaml"))

        assert cfg.digest_enabled is True
        assert cfg.digest_repo == "owner/repo"
        assert cfg.digest_category_slug == "general"


class TestPostDigestDiscussion:
    @staticmethod
    def _lookup_response():
        return {
            "data": {
                "repository": {
                    "id": "R_kgABC",
                    "discussionCategories": {
                        "nodes": [
                            {"id": "DIC_1", "slug": "announcements"},
                            {"id": "DIC_2", "slug": "general"},
                        ]
                    },
                }
            }
        }

    @staticmethod
    def _create_response():
        return {
            "data": {
                "createDiscussion": {
                    "discussion": {"url": "https://github.com/o/r/discussions/42"}
                }
            }
        }

    def _mock_post(self, responses):
        mock = MagicMock()
        resps = []
        for r in responses:
            m = MagicMock()
            m.raise_for_status = MagicMock()
            m.json.return_value = r
            resps.append(m)
        mock.side_effect = resps
        return mock

    def test_posts_and_returns_url(self):
        with patch("upstream_jira_sync.digest.requests.post") as mock_post:
            mock_post.side_effect = self._mock_post(
                [self._lookup_response(), self._create_response()]
            ).side_effect
            url = post_digest_discussion(
                token="tok",
                repo="owner/repo",
                category_slug="announcements",
                title="T",
                body="B",
            )
        assert url == "https://github.com/o/r/discussions/42"
        assert mock_post.call_count == 2
        mutation_call = mock_post.call_args_list[1]
        assert "createDiscussion" in mutation_call.kwargs["json"]["query"]
        assert mutation_call.kwargs["json"]["variables"]["repo"] == "R_kgABC"
        assert mutation_call.kwargs["json"]["variables"]["cat"] == "DIC_1"

    def test_missing_category_raises_with_available_slugs(self):
        with patch("upstream_jira_sync.digest.requests.post") as mock_post:
            mock_post.side_effect = self._mock_post(
                [self._lookup_response()]
            ).side_effect
            with pytest.raises(RuntimeError, match="announcements.*general"):
                post_digest_discussion(
                    token="tok",
                    repo="owner/repo",
                    category_slug="does-not-exist",
                    title="T",
                    body="B",
                )

    def test_graphql_errors_raise(self):
        with patch("upstream_jira_sync.digest.requests.post") as mock_post:
            mock_post.side_effect = self._mock_post(
                [{"errors": [{"message": "bad auth"}]}]
            ).side_effect
            with pytest.raises(RuntimeError, match="GraphQL errors"):
                post_digest_discussion(
                    token="tok",
                    repo="owner/repo",
                    category_slug="announcements",
                    title="T",
                    body="B",
                )

    def test_invalid_repo_raises(self):
        with pytest.raises(ValueError, match="Invalid repo"):
            post_digest_discussion(
                token="tok",
                repo="noslash",
                category_slug="announcements",
                title="T",
                body="B",
            )

    def test_unknown_repo_raises(self):
        with patch("upstream_jira_sync.digest.requests.post") as mock_post:
            mock_post.side_effect = self._mock_post(
                [{"data": {"repository": None}}]
            ).side_effect
            with pytest.raises(RuntimeError, match="not found or token lacks access"):
                post_digest_discussion(
                    token="tok",
                    repo="owner/repo",
                    category_slug="announcements",
                    title="T",
                    body="B",
                )


class TestReviewActivity:
    def _intro(self):
        return SkillLoader().load("review_activity_intro")

    def _stats(self):
        from upstream_jira_sync.review_activity import MemberReviewStats

        return [
            MemberReviewStats("zara", 2, 5, 1),
            MemberReviewStats("adam", 1, 3, 0),
            MemberReviewStats("inactive", 0, 0, 0),
        ]

    def test_fetch_filters_window_and_bots(self):
        from upstream_jira_sync.review_activity import (
            MemberReviewStats,
            fetch_review_stats,
        )

        window_start = _utc(2026, 6, 1)
        github = MagicMock()
        github.get_review_activity.return_value = [
            {
                "pr_url": "https://github.com/exampleorg/widgets/pull/1",
                "pr_author": "human",
                "reviews": [
                    {
                        "state": "APPROVED",
                        "submitted_at": "2026-06-03T10:00:00+00:00",
                        "comment_count": 4,
                    },
                    {
                        "state": "COMMENTED",
                        "submitted_at": "2026-05-20T10:00:00+00:00",
                        "comment_count": 9,
                    },
                ],
            },
            {
                "pr_url": "https://github.com/exampleorg/widgets/pull/2",
                "pr_author": "examplebot",
                "reviews": [
                    {
                        "state": "APPROVED",
                        "submitted_at": "2026-06-03T11:00:00+00:00",
                        "comment_count": 2,
                    }
                ],
            },
        ]
        team = [TeamMember(github="zara", jira_email="zara@example.com")]

        stats = fetch_review_stats(
            github, team, window_start, frozenset({"examplebot"})
        )

        assert stats == [MemberReviewStats("zara", 1, 4, 1)]

    def test_render_alphabetical_with_totals(self):
        from upstream_jira_sync.review_activity import render_review_section

        section = render_review_section(self._stats(), self._intro())

        lines = [line for line in section.splitlines() if line.startswith("| ")]
        assert lines[2].startswith("| adam ")
        assert lines[3].startswith("| zara ")
        assert "**Team total** | **3** | **8** | **1**" in lines[4]
        assert "inactive" not in section
        assert "2 of 3 team members" in section
        assert section.startswith("## Upstream review activity")

    def test_digest_fail_soft_on_github_error(self):
        from upstream_jira_sync.review_activity import (
            fetch_review_stats,
            render_review_section,
        )

        github = MagicMock()
        github.get_review_activity.side_effect = RuntimeError("api down")
        team = [TeamMember(github="zara", jira_email="zara@example.com")]

        stats = fetch_review_stats(github, team, _utc(2026, 6, 1), frozenset())
        section = render_review_section(stats, self._intro())

        assert stats[0].prs_reviewed == 0
        assert "No upstream review activity recorded" in section
