from __future__ import annotations

from datetime import date, timedelta


def current_sprint_number(
    anchor: date, today: date, anchor_number: int, sprint_days: int = 14
) -> int | None:
    """Sprint number containing 'today' via the configured cadence, or None before anchor."""
    if today < anchor:
        return None
    return anchor_number + (today - anchor).days // sprint_days


def sprint_window(
    anchor: date, anchor_number: int, number: int, sprint_days: int = 14
) -> tuple[date, date]:
    """(start, end) for sprint `number`; end is the next sprint's start, one cadence on."""
    start = anchor + timedelta(days=sprint_days * (number - anchor_number))
    return start, start + timedelta(days=sprint_days)


def sprints_to_provision(
    anchor: date, today: date, anchor_number: int, lookahead: int, sprint_days: int = 14
) -> list[int]:
    """Upcoming sprint numbers that should already exist: current+1 .. current+lookahead.

    A rolling buffer kept current_sprint-relative, so the next sprints always exist
    ahead of time. Empty before the anchor or when lookahead is not positive.
    """
    current = current_sprint_number(anchor, today, anchor_number, sprint_days)
    if current is None or lookahead <= 0:
        return []
    return [current + offset for offset in range(1, lookahead + 1)]


def sweep_cutoff_date(
    anchor: date, today: date, lookback_sprints: int, sprint_days: int = 14
) -> date | None:
    """Start of the sprint `lookback_sprints` before the current one, or None before anchor.

    Anchored to sprint boundaries (not a rolling window) so a carried-over card
    does not age out of eligibility partway through a sprint. lookback_sprints=0
    is the current sprint start; 1 reaches back to the previous sprint.
    """
    if today < anchor:
        return None
    index = (today - anchor).days // sprint_days
    current_start = anchor + timedelta(days=sprint_days * index)
    return current_start - timedelta(days=sprint_days * lookback_sprints)
