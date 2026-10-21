"""How the knowledge is used: search results, recommendations and feedback, per entry.

Everything is derived from append-only logs in the state folder (state/knowledge/*.jsonl), so using an entry never
changes the git repository. The statistics feed the ranking (entries that helped rise a little, entries reported
as unhelpful sink), the resident tips in the server instructions and the review list.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import config

SOURCES = ("experience", "manual", "example", "keyword", "api")


def log_dir() -> Path:
    return config.state_dir() / "knowledge"


def append(name: str, record: dict[str, Any], folder: Path | None = None) -> None:
    folder = folder or log_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read(name: str, folder: Path | None = None) -> list[dict[str, Any]]:
    path = (folder or log_dir()) / name
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def log_recommendation(context: str, ids: Iterable[str], folder: Path | None = None) -> None:
    ids = [i for i in ids if i]
    if ids:
        append("recommendations.jsonl", {"t": round(time.time(), 3), "context": context, "ids": ids}, folder)


def key_of(ref: str) -> str:
    """'experience:x' / 'manual:Theory/...' stay as they are; a bare ref is an experience id or a manual page."""
    source, _, rest = ref.partition(":")
    if rest and source in SOURCES:
        return ref
    return f"manual:{ref}" if ref.endswith(".html") or "/" in ref else f"experience:{ref}"


@dataclass
class Usage:
    shown: int = 0  # in search results
    recommended: int = 0  # attached to a tool result by itself
    helpful: int = 0
    unhelpful: int = 0
    last_used: float | None = None

    def touch(self, t: float | None) -> None:
        if t and (self.last_used is None or t > self.last_used):
            self.last_used = t

    @property
    def factor(self) -> float:
        """Ranking multiplier: helpful reports lift an entry, unhelpful ones push it down (bounded)."""
        return max(0.5, 1.0 + 0.1 * min(self.helpful, 5) - 0.15 * min(self.unhelpful, 3))

    def view(self) -> dict[str, Any]:
        out = {k: v for k, v in (("shown", self.shown), ("recommended", self.recommended),
                                 ("helpful", self.helpful), ("unhelpful", self.unhelpful)) if v}
        if self.last_used:
            out["last_used"] = time.strftime("%Y-%m-%d", time.localtime(self.last_used))
        return out


_CACHE: dict[str, tuple[tuple, dict[str, Usage]]] = {}


def load(folder: Path | None = None) -> dict[str, Usage]:
    """Usage per key ('experience:<id>', 'manual:<page>', ...), cached until a log file changes."""
    folder = folder or log_dir()
    names = ("search_log.jsonl", "feedback.jsonl", "recommendations.jsonl")
    stamp = tuple((folder / n).stat().st_mtime_ns if (folder / n).exists() else 0 for n in names)
    cached = _CACHE.get(str(folder))
    if cached and cached[0] == stamp:
        return cached[1]
    usage: dict[str, Usage] = {}
    for record in read("search_log.jsonl", folder):
        for result in record.get("results", []):
            item = usage.setdefault(result.get("key", ""), Usage())
            item.shown += 1
            item.touch(record.get("t"))
    for record in read("recommendations.jsonl", folder):
        for entry_id in record.get("ids", []):
            item = usage.setdefault(f"experience:{entry_id}", Usage())
            item.recommended += 1
            item.touch(record.get("t"))
    for record in read("feedback.jsonl", folder):
        for ref in record.get("helpful") or []:
            item = usage.setdefault(key_of(ref), Usage())
            item.helpful += 1
            item.touch(record.get("t"))
        for ref in record.get("unhelpful") or []:
            usage.setdefault(key_of(ref), Usage()).unhelpful += 1
    usage.pop("", None)
    _CACHE[str(folder)] = (stamp, usage)
    return usage


def experience_usage(folder: Path | None = None) -> dict[str, Usage]:
    return {k.split(":", 1)[1]: v for k, v in load(folder).items() if k.startswith("experience:")}
