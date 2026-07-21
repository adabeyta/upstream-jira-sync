from __future__ import annotations

import logging
import re

from upstream_jira_sync.adf import AdfBuilder
from upstream_jira_sync.ai import IssueSummarizer
from upstream_jira_sync.config import AppConfig
from upstream_jira_sync.github import GitHubClient
from upstream_jira_sync.jira import RFC_EPIC_GLOBAL_ID_PREFIX, JiraClient
from upstream_jira_sync.models import (
    CanonicalStatus,
    JiraTicket,
    LinkedIssue,
    PullRequest,
    TeamMember,
    canonical_github_url,
    repo_from_github_url,
)
from upstream_jira_sync.override_gate import ManualOverrideGate
from upstream_jira_sync.state import SyncState

log = logging.getLogger(__name__)

RFC_TITLE_RE = re.compile(r"^\s*(\[RFC\]|RFC:)", re.IGNORECASE)
_PART_OF_RE = re.compile(r"\bpart of #(\d+)\b", re.IGNORECASE)
_ISSUE_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d+")


class RfcEpicTracker:
    """Resolves and creates container issues for upstream RFC issues, parents children."""

    def __init__(
        self,
        config: AppConfig,
        jira: JiraClient,
        state: SyncState,
        github: GitHubClient,
        summarizer: IssueSummarizer | None = None,
    ) -> None:
        self._config = config
        self._jira = jira
        self._state = state
        self._github = github
        self._summarizer = summarizer

    def ensure_epic(
        self,
        rfc_url: str,
        rfc_title: str,
        rfc_body: str,
        member: TeamMember,
    ) -> str | None:
        """Idempotently resolve or create the container issue tracking an upstream RFC."""
        cached = self._state.get_rfc_epic(rfc_url)
        if cached:
            self._state.record_rfc_epic(rfc_url, cached)
            return cached

        found = self._jira.find_epic_for_rfc(rfc_url, self._config.jira_project_key)
        if found:
            self._state.record_rfc_epic(rfc_url, found.key)
            return found.key

        canonical = canonical_github_url(rfc_url)
        override = next(
            (
                key
                for url, key in self._config.rfc_overrides.items()
                if canonical_github_url(url) == canonical and key
            ),
            None,
        )
        if override:
            self._state.record_rfc_epic(rfc_url, override)
            return override

        if self._config.rfc_epic_mode == "shadow":
            log.info(
                "  [RFC-SHADOW] Would create %s for %s %r",
                self._config.container_issue_type,
                rfc_url,
                rfc_title,
            )
            return None

        description = ""
        if self._summarizer:
            description = self._summarizer.summarize(rfc_title, rfc_body)
        description = (
            description or f"Upstream RFC by @{member.github}."
        ) + f"\n\nUpstream RFC: {rfc_url}"
        epic_summary = rfc_title[:255]
        extra_fields = (
            {self._config.epic_name_field: epic_summary}
            if self._config.epic_name_field
            else None
        )
        epic = self._jira.create_ticket(
            project_key=self._config.jira_project_key,
            summary=epic_summary,
            description_adf=AdfBuilder.issue_description(
                rfc_title, rfc_url, description
            ),
            assignee_email=member.jira_email,
            issuetype=self._config.container_issue_type,
            extra_fields=extra_fields,
            components=self._config.jira_components or None,
            initial_status_name=self._config.status_name(CanonicalStatus.TODO),
        )
        self._state.record_rfc_epic(rfc_url, epic.key)
        try:
            self._jira.add_remote_link(
                epic,
                rfc_url,
                f"RFC: {rfc_title}",
                relationship="GitHub issue",
                global_id=RFC_EPIC_GLOBAL_ID_PREFIX + canonical,
            )
        except Exception:
            log.warning(
                "  Failed to add RFC remote link on %s -- state has the mapping",
                epic.key,
            )
        return epic.key

    def parent_for_pr(self, pr: PullRequest, member: TeamMember) -> str | None:
        """Resolve the single RFC container this PR belongs to, or None.

        Membership signals (precision over recall): closing refs with RFC titles,
        and explicit "Part of #N" body mentions. Multiple distinct RFCs -> skip.
        """
        candidates: dict[str, LinkedIssue] = {}
        examined: set[str] = set()
        for issue in pr.linked_issues:
            url = canonical_github_url(issue.url)
            examined.add(url)
            if RFC_TITLE_RE.match(issue.title or ""):
                candidates[url] = issue

        body = pr.body or ""
        part_of = sorted({int(n) for n in _PART_OF_RE.findall(body)})
        if part_of:
            try:
                repo = repo_from_github_url(pr.url)
            except ValueError:
                part_of = []
            for number in part_of:
                issue = self._github._fetch_issue(repo, number)
                if issue is None:
                    continue
                url = canonical_github_url(issue.url)
                examined.add(url)
                if RFC_TITLE_RE.match(issue.title or ""):
                    candidates.setdefault(url, issue)

        for raw_url in _ISSUE_URL_RE.findall(body):
            if canonical_github_url(raw_url) not in examined:
                log.info(
                    "  PR #%d: possible RFC reference, not parenting: %s",
                    pr.number,
                    raw_url,
                )

        if not candidates:
            return None
        if len(candidates) > 1:
            log.warning(
                "  PR #%d references multiple RFCs (%s) -- not parenting",
                pr.number,
                ", ".join(sorted(candidates)),
            )
            return None
        rfc = next(iter(candidates.values()))
        return self.ensure_epic(rfc.url, rfc.title, rfc.body, member)

    def maybe_reparent(
        self,
        pr: PullRequest,
        ticket: JiraTicket,
        member: TeamMember,
        override_gate: ManualOverrideGate | None = None,
    ) -> None:
        """Parent an orphan ticket under its RFC container. Fails closed on gate doubt."""
        if not self._config.enable_rfc_epics:
            return
        if ticket.parent_key or ticket.is_type(self._config.container_issue_type):
            return
        if override_gate is not None and override_gate.is_unreliable(ticket.key):
            log.info("  Skipping re-parent of %s: changelog unreliable", ticket.key)
            return
        epic_key = self.parent_for_pr(pr, member)
        if not epic_key:
            return
        if self._config.rfc_epic_mode == "shadow":
            log.info("  [RFC-SHADOW] Would parent %s under %s", ticket.key, epic_key)
            return
        try:
            self._jira.set_parent(ticket.key, epic_key)
            log.info("  Parented %s under %s", ticket.key, epic_key)
        except Exception:
            log.exception("  Failed to parent %s under %s", ticket.key, epic_key)
