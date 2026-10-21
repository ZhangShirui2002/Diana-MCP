"""Learning from script errors: a DIANA command that failed and then worked with other arguments.

When diana_execute raises from a DIANA command (not from a Python slip such as a NameError or a syntax error),
the failing calls are remembered for a while. If a later successful diana_execute calls the same command with
different arguments, that pair is drafted as an api-usage experience: the failing call, DIANA's error, the call
that worked. The AI then explains the difference and asks the user to approve it.
"""
from __future__ import annotations

import ast
import builtins
import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

from . import config

PYTHON_SLIPS = {"NameError", "SyntaxError", "IndentationError", "TabError", "ImportError", "ModuleNotFoundError",
                "KeyboardInterrupt", "SystemExit", "ZeroDivisionError", "UnboundLocalError"}
KEEP_SECONDS = 2 * 3600
MAX_PENDING = 30


def error_line(traceback_text: str) -> str:
    lines = [l.strip() for l in traceback_text.strip().splitlines() if l.strip()]
    return lines[-1] if lines else ""


def error_type(traceback_text: str) -> str:
    match = re.match(r"([A-Za-z_][\w.]*)(?::|$)", error_line(traceback_text))
    return match.group(1).split(".")[-1] if match else ""


def calls(code: str, line: int | None = None) -> list[tuple[str, str]]:
    """(function name, source text) of the calls in ``code`` (only those on ``line`` when given)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if line is None or node.lineno <= line <= getattr(node, "end_lineno", node.lineno):
                text = ast.get_source_segment(code, node) or ""
                found.append((node.func.id, re.sub(r"\s+", " ", text)[:300]))
    return found


def failing_line(traceback_text: str) -> int | None:
    """Line of the submitted code that failed: the first <mcp> frame (later <mcp> frames can be functions defined
    by earlier diana_execute calls)."""
    lines = re.findall(r'File "<mcp>", line (\d+)', traceback_text)
    return int(lines[0]) if lines else None


class ApiLessons:
    def __init__(self, path: Path | None = None, known: Iterable[str] | None = None):
        self.path = path or config.state_dir() / "knowledge" / "pending_api_errors.json"
        self.known = set(known) if known is not None else None  # DIANA command names, when the reference is cached

    def _load(self) -> list[dict[str, Any]]:
        try:
            items = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        cutoff = time.time() - KEEP_SECONDS
        return [i for i in items if i.get("t", 0) >= cutoff]

    def _store(self, items: list[dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(items[-MAX_PENDING:], ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _diana_calls(self, found: list[tuple[str, str]]) -> list[tuple[str, str]]:
        return [(n, t) for n, t in found if (self.known is None and n[:1].islower() and not hasattr(builtins, n))
                or (self.known is not None and n in self.known)]

    def record_failure(self, code: str, traceback_text: str) -> bool:
        """Remember the DIANA calls on the failing line; False for Python slips or code without DIANA calls."""
        if error_type(traceback_text) in PYTHON_SLIPS:
            return False
        found = self._diana_calls(calls(code, failing_line(traceback_text))) or []
        if not found:
            return False
        items = self._load()
        items.append({"t": round(time.time(), 1), "calls": found, "error": error_line(traceback_text)[:400]})
        self._store(items)
        return True

    def resolve(self, code: str) -> list[dict[str, str]]:
        """Lessons for pending failures that this successful code fixed: same command, other arguments."""
        succeeded = self._diana_calls(calls(code))
        if not succeeded:
            return []
        items = self._load()
        lessons, remaining = [], []
        for item in items:
            fixed = None
            for name, failed_text in item["calls"]:
                match = next((t for n, t in succeeded if n == name and t != failed_text), None)
                if match:
                    fixed = {"command": name, "failed": failed_text, "error": item["error"], "worked": match}
                    break
            if fixed:
                lessons.append(fixed)
            else:
                remaining.append(item)
        if lessons:
            self._store(remaining)
        return lessons
