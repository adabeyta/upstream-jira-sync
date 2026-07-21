"""Upstream review activity for the weekly digest. Read-only: GitHub queries and markdown."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from upstream_jira_sync.github import GitHubClient
from upstream_jira_sync.models import TeamMember

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemberReviewStats:
    github: str
    prs_reviewed: int
    review_comments: int
    approvals: int


def fetch_review_stats(
    github: GitHubClient,
    team: list[TeamMember],
    window_start: datetime,
    bot_logins: frozenset[str],
) -> list[MemberReviewStats]:
    stats = []
    for member in team:
        if not member.github:
            continue
        prs_reviewed = 0
        review_comments = 0
        approvals = 0
        try:
            for pr in github.get_review_activity(
                member.github, since=window_start.isoformat()
            ):
                if pr["pr_author"] in bot_logins:
                    continue
                in_window = [
                    r
                    for r in pr["reviews"]
                    if r["state"] != "PENDING"
                    and r["submitted_at"]
                    and datetime.fromisoformat(r["submitted_at"]) >= window_start
                ]
                if not in_window:
                    continue
                prs_reviewed += 1
                review_comments += sum(r["comment_count"] for r in in_window)
                approvals += sum(1 for r in in_window if r["state"] == "APPROVED")
        except Exception:
            log.warning(
                "Review activity fetch failed for @%s; reporting zero",
                member.github,
                exc_info=True,
            )
        stats.append(
            MemberReviewStats(
                github=member.github,
                prs_reviewed=prs_reviewed,
                review_comments=review_comments,
                approvals=approvals,
            )
        )
    return stats


def render_review_section(stats: list[MemberReviewStats], intro: str) -> str:
    lines = [intro.strip(), ""]
    active = sorted(
        (s for s in stats if s.prs_reviewed), key=lambda s: s.github.lower()
    )
    if not active:
        lines.append("_No upstream review activity recorded this window._")
        return "\n".join(lines)

    lines += [
        "| Member | PRs reviewed | Review comments | Approvals |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {s.github} | {s.prs_reviewed} | {s.review_comments} | {s.approvals} |"
        for s in active
    ]
    lines.append(
        f"| **Team total** | **{sum(s.prs_reviewed for s in active)}**"
        f" | **{sum(s.review_comments for s in active)}**"
        f" | **{sum(s.approvals for s in active)}** |"
    )
    lines += [
        "",
        f"_{len(active)} of {len(stats)} team members had review activity this window._",
    ]
    return "\n".join(lines)
