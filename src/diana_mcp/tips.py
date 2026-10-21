"""Resident tips: the most useful verified experiences, shown to the AI at the start of every session.

The AI then knows the user's most important lessons without searching. Clients cut long server instructions (Claude
Code keeps 2048 characters), so the tips travel with the first tool result of a server process (handbook_tips) and
with diana_guide. The list is ranked by how often an entry helped and was used, so it follows real work, and it
reminds the AI when entries were last verified on another DIANA version than the installed one.
"""
from __future__ import annotations

from typing import Any

from .experience import ExperienceStore
from .usage import Usage

MAX_TIPS = 12
READ_HINT = "read an entry with diana_experience_list(id=...) before relying on details"


def ranked_verified(store: ExperienceStore, stats: dict[str, Usage]) -> list[Any]:
    verified = [e for e in store.entries() if e.status == "verified"]

    def score(entry: Any) -> tuple:
        use = stats.get(entry.id, Usage())
        return (-use.helpful, -(use.recommended + use.shown), -(use.last_used or 0), entry.id)

    return sorted(verified, key=score)


def _stale_note(store: ExperienceStore, installed_version: str | None) -> str | None:
    if not installed_version:
        return None
    stale = [e for e in store.entries() if e.checks and str(e.checks.get("diana_version")) != installed_version]
    if not stale:
        return None
    return (f"{len(stale)} experience entries were last verified automatically on another DIANA version than the "
            f"installed {installed_version}: offer the user to re-run diana_experience_verify.")


def resident_tips(store: ExperienceStore, stats: dict[str, Usage], installed_version: str | None,
                  limit: int = MAX_TIPS) -> dict[str, Any]:
    """The tips as a small dict for a tool result; empty when there is nothing to say."""
    out: dict[str, Any] = {}
    tips = ranked_verified(store, stats)[:limit]
    if tips:
        out["verified_lessons"] = [f"{e.title} [{e.id}]" for e in tips]
        out["how"] = READ_HINT
    note = _stale_note(store, installed_version)
    if note:
        out["note"] = note
    return out


def resident_section(store: ExperienceStore, stats: dict[str, Usage], installed_version: str | None,
                     limit: int = MAX_TIPS) -> str:
    """The tips as text, for the end of diana_guide."""
    lines: list[str] = []
    tips = ranked_verified(store, stats)[:limit]
    if tips:
        lines.append("")
        lines.append(f"Verified lessons from the user's experience handbook (most useful first; {READ_HINT}):")
        lines += [f"- {e.title} [{e.id}]" for e in tips]
    note = _stale_note(store, installed_version)
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)
