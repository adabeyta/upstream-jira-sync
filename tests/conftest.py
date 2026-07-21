"""Shared fixtures and helpers for the upstream-jira-sync test suite."""

from __future__ import annotations

from upstream_jira_sync.config import AppConfig, LLMSettings, TeamSpec
from upstream_jira_sync.models import (
    GitHubIssue,
    JiraTicket,
    LinkedIssue,
    PullRequest,
    TeamMember,
)


def make_pr(
    number: int = 1,
    title: str = "Fix transport layer reconnect",
    body: str = "Implements a bounded retry loop for the transport client.",
    state: str = "open",
    merged: bool = False,
    draft: bool = False,
    labels: tuple[str, ...] = (),
    merge_labels: tuple[str, ...] = (),
    linked_issues: tuple[LinkedIssue, ...] = (),
    last_human_activity_at: str = "",
    author: str = "",
    commit_authors: tuple[str, ...] = (),
) -> PullRequest:
    return PullRequest(
        number=number,
        title=title,
        url=f"https://github.com/exampleorg/widgets/pull/{number}",
        state=state,
        merged=merged,
        draft=draft,
        updated_at="2026-03-20T10:00:00Z",
        last_human_activity_at=last_human_activity_at,
        body=body,
        author=author,
        labels=labels,
        commit_authors=commit_authors,
        merge_labels=merge_labels,
        linked_issues=linked_issues,
    )


def make_ticket(key: str, summary: str, status: str = "In Progress") -> JiraTicket:
    return JiraTicket(
        key=key,
        summary=summary,
        status=status,
        url=f"https://jira.example.com/browse/{key}",
    )


def make_issue(
    number: int = 42, title: str = "Fix overflow in range op"
) -> GitHubIssue:
    return GitHubIssue(
        number=number,
        title=title,
        url=f"https://github.com/exampleorg/widgets/issues/{number}",
        body="Test issue body",
        state="open",
        latest_comment="I'll submit a fix for this",
    )


def make_linked_issue(
    number: int = 999, title: str = "Fix overflow in range op"
) -> LinkedIssue:
    return LinkedIssue(
        number=number,
        title=title,
        url=f"https://github.com/exampleorg/widgets/issues/{number}",
        body="Overflow when using 64-bit indices.",
    )


def make_config(**overrides) -> AppConfig:
    """AppConfig with neutral test defaults; keyword args override."""
    base = dict(
        team=[TeamMember(github="octocat", jira_email="octocat@example.com")],
        github_token="tok",
        github_repo=["exampleorg/widgets"],
        jira_url="https://jira.example.com",
        jira_email="bot@example.com",
        jira_token="jtok",
        jira_project_key="PROJ",
        llm=LLMSettings(provider="anthropic", model="test-model"),
    )
    base.update(overrides)
    return AppConfig(**base)


def make_teams() -> list[TeamSpec]:
    return [
        TeamSpec(name="Team Alpha", label="team-alpha", team_id="team-uuid-alpha"),
        TeamSpec(name="Team Beta", label="team-beta", team_id="team-uuid-beta"),
        TeamSpec(name="Team Gamma", label="team-gamma"),
    ]


def make_graphql_pr_node(
    number: int = 1,
    title: str = "Fix transport layer reconnect",
    state: str = "OPEN",
    is_draft: bool = False,
    merged: bool = False,
    review_decision: str | None = None,
    reviews: list[dict] | None = None,
    labels: list[str] | None = None,
    commit_authors: list[str | None] | None = None,
) -> dict:
    node = {
        "number": number,
        "title": title,
        "url": f"https://github.com/exampleorg/widgets/pull/{number}",
        "state": state,
        "isDraft": is_draft,
        "merged": merged,
        "updatedAt": "2026-03-20T10:00:00Z",
        "body": "Test PR body",
        "author": {"login": "octocat"},
        "labels": {"nodes": [{"name": n} for n in (labels or [])]},
        "reviewDecision": review_decision,
        "reviews": {"nodes": reviews or []},
    }
    if commit_authors is not None:
        node["commits"] = {
            "nodes": [
                {
                    "commit": {
                        "committedDate": "2026-03-20T09:00:00Z",
                        "authors": {
                            "nodes": [
                                {"user": {"login": a} if a else None}
                                for a in commit_authors
                            ]
                        },
                    }
                }
            ]
        }
    return node


class FakeLLM:
    """LLMProvider stub returning canned text and recording every call."""

    def __init__(self, response_text: str = "", error: Exception | None = None) -> None:
        self.response_text = response_text
        self.error = error
        self.calls: list[dict] = []

    def complete(self, system: str, user_message: str, max_tokens: int = 256) -> str:
        self.calls.append(
            {"system": system, "user_message": user_message, "max_tokens": max_tokens}
        )
        if self.error is not None:
            raise self.error
        return self.response_text
