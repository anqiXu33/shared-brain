"""How old is what the memory says "now"?

`state` and `blocker` entries describe the present: where something stands, what is in the way.
Once nobody has touched one for a while, it may no longer be true; work may have moved on in a
conversation that never saved anything. project_brief marks such entries so the assistant says
"this may be outdated" instead of presenting an old status as current. Decisions, context and
logs are records of what happened and do not go stale this way.

Used by server.py (project_brief), viewer.py (the badge on the overview) and the behaviour
evaluation's sandbox, so all three agree on what counts as stale.

    STALE_DAYS   days without an update after which a state or blocker is flagged (default 14)
"""
import os
from datetime import datetime

STALE_DAYS = int(os.environ.get("STALE_DAYS", "14"))
STALE_KINDS = ("state", "blocker")


def age_days(timestamp: str, now: datetime) -> int:
    """Whole days between an ISO timestamp from the database and `now` (timezone-aware)."""
    return (now - datetime.fromisoformat(timestamp)).days


def mark(rows: list[dict], now: datetime) -> list[dict]:
    """Add days_since_update to each row, and possibly_stale: true where it is STALE_DAYS or more.
    Changes the rows in place and returns them."""
    for r in rows:
        r["days_since_update"] = age_days(r["updated_at"], now)
        if r["days_since_update"] >= STALE_DAYS:
            r["possibly_stale"] = True
    return rows


def notes(state: list[dict], blockers: list[dict], last_updated: str | None, now: datetime) -> list[str]:
    """Plain-language warnings for the assistant, empty when everything is recent.
    Expects state and blockers already passed through mark()."""
    out = []
    for label, rows in (("state", state), ("blocker", blockers)):
        stale = [r for r in rows if r.get("possibly_stale")]
        if stale:
            oldest = max(r["days_since_update"] for r in stale)
            out.append(
                f"{len(stale)} of {len(rows)} {label} entries have not been updated for {STALE_DAYS} days or more "
                f"(oldest: {oldest} days). They may no longer be true. When you rely on one, say how old it is "
                "and ask the user whether it still holds; once they confirm what changed, supersede it with a new "
                "entry or retire it."
            )
    if last_updated is not None:
        idle = age_days(last_updated, now)
        if idle >= STALE_DAYS:
            out.append(
                f"Nothing has been saved for this project in {idle} days. Work may have happened that the memory "
                "does not know about."
            )
    return out
