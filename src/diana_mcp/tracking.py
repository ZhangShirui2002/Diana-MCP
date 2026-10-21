"""'What changed since the last look': the user's operations from DianaIE's history plus the net model difference.

Shared by the diana_changes tool and the follow-mode watcher (python -m diana_mcp watch), so both advance the same
cursor (per DianaIE session) and the same baseline snapshot (per project).
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from . import changes as ch
from . import config
from .bridge_client import BridgeError
from .session import Session, SessionManager
from .snapshots import SnapshotInfo, SnapshotStore, attribute, diff, live_snapshot_code, project_key


class TrackingError(RuntimeError):
    pass


def _schema() -> Any:
    try:
        from .schema import load_schema
        return load_schema(config.default_install())
    except Exception:
        return None


class Tracker:
    def __init__(self, sessions: SessionManager, store: SnapshotStore | None = None):
        self.sessions = sessions
        self.store = store or SnapshotStore()

    # --- where to look ------------------------------------------------------------------------------------------

    def live_session(self) -> Session | None:
        try:
            return self.sessions.require()
        except BridgeError:
            return None

    def current_log(self, session: Session | None = None) -> tuple[ch.HistoryFile, Path | None]:
        """History file (and journal) of the DianaIE being tracked: the bridge's, else a running DianaIE's, else
        the newest one on disk."""
        if session is not None:
            log_file = session.info.get("log_file")
            if not log_file and session.responsive:
                try:
                    log_file = str(self.sessions.command_log_path())
                except BridgeError:
                    log_file = None
            if log_file:
                # The bridge reports both the way DIANA sees them; they are read here, on the host.
                found = ch.find_history(str(config.to_host(log_file)))
                if found is not None:
                    journal = session.info.get("journal")
                    return found, config.to_host(journal) if journal else ch.journal_path(found.key)
        running = ch.logs_of_running_dianaie()
        if running:
            newest = max(running.values(), key=lambda h: h.started)
            return newest, ch.journal_path(newest.key)
        files = ch.history_files()
        if not files:
            raise TrackingError("no DianaIE command history found (looked in " +
                                ", ".join(str(d) for d in ch.log_dirs()) + ")")
        return files[-1], ch.journal_path(files[-1].key)

    # --- snapshots ----------------------------------------------------------------------------------------------

    def take_live(self) -> dict[str, Any]:
        out = config.state_dir() / "tmp" / f"snapshot-{uuid.uuid4().hex}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.sessions.run_checked(live_snapshot_code(out), timeout=600)
            snapshot = json.loads(out.read_text(encoding="utf-8"))
        finally:
            out.unlink(missing_ok=True)
        try:
            log, _ = self.current_log(self.live_session())
            refine_project_path(snapshot, ch.read_log(log.path)[0])
        except (TrackingError, OSError):
            pass
        return snapshot

    def baseline(self, snapshot: dict[str, Any]) -> SnapshotInfo | None:
        project = snapshot.get("project") or {}
        return self.store.latest(project_key(project.get("path"), project.get("name")))

    # --- changes ------------------------------------------------------------------------------------------------

    def changes(self, advance: bool = True, categories: Iterable[str] = ch.DEFAULT_CATEGORIES,
                sources: Iterable[str] = ("user", "unknown"), with_diff: bool = True,
                max_operations: int = 200) -> dict[str, Any]:
        session = self.live_session()
        log, journal = self.current_log(session)
        history = ch.load_history(log, journal, _schema())
        cursor = ch.load_cursor(log.key)
        since = cursor if cursor is not None and cursor <= history.end else 0
        window = [e for e in history.entries if e.offset >= since]
        operations = ch.select_entries(window, 0, categories, sources)
        ai_steps = [e for e in window if e.source == "ai" and e.modifying]
        notes: list[str] = []
        result: dict[str, Any] = {
            "dianaie": {
                "pid": session.pid if session else None,
                "history_file": str(log.path),
                "tracking": "journal" if history.journal else "history only",
            },
            "since": "上次查看之后" if cursor is not None else "本次 DianaIE 会话开始以来",
            "operations": [e.view() for e in operations[-max_operations:]],
            "operations_total": len(operations),
        }
        if len(operations) > max_operations:
            notes.append(f"只列出最近 {max_operations} 条操作")
        if ai_steps and "ai" not in set(sources):
            result["ai_operations_hidden"] = len(ai_steps)
        if history.journal is None:
            notes.append("该 DianaIE 没有运行新版 bridge：操作来源（你 / AI）无法区分，撤销对象按顺序推断")
        if with_diff and session is not None and session.responsive:
            try:
                snapshot = self.take_live()
            except BridgeError as exc:
                notes.append(f"无法读取模型快照：{exc}")
            else:
                base = self.baseline(snapshot)
                if base is None:
                    result["model_changes"] = []
                    notes.append("首次记录这个项目：已保存基线快照，以后再查就能看到参数的新旧值对比")
                else:
                    found = diff(self.store.load(base), snapshot)
                    attribute(found, window)
                    result["model_changes"] = [c.view() for c in found]
                    result["baseline"] = base.describe() | {
                        "from_earlier_dianaie_session": base.taken < log.started.timestamp()}
                if advance or base is None:
                    self.store.save(snapshot)
        elif with_diff:
            notes.append("没有连接 bridge：只能列出操作，无法对比参数的新旧值（启用 diana_autostart 可自动连接）")
        if advance:
            ch.save_cursor(log.key, history.end)
        if notes:
            result["notes"] = notes
        return result

    # --- waiting ------------------------------------------------------------------------------------------------

    def wait_for_changes(self, timeout: float, quiet: float = 3.0, poll: float = 0.5,
                         categories: Iterable[str] = ch.DEFAULT_CATEGORIES,
                         sources: Iterable[str] = ("user", "unknown"), should_stop: Any = None) -> bool:
        """Block until the tracked DianaIE has new relevant operations after the cursor and its history file has
        been quiet for ``quiet`` seconds (the user paused). False on timeout or when DianaIE went away."""
        deadline = time.monotonic() + timeout
        session = self.live_session()
        log, journal = self.current_log(session)
        cursor = ch.load_cursor(log.key)
        if cursor is None:
            cursor = log.size
            ch.save_cursor(log.key, cursor)
        last_size, last_change = -1, time.monotonic()
        categories, sources = tuple(categories), tuple(sources)
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                return False
            if not log.path.exists():
                return False
            size = log.size
            now = time.monotonic()
            if size != last_size:
                last_size, last_change = size, now
            elif size > cursor and now - last_change >= quiet:
                entries, _ = ch.read_log(log.path, cursor)
                ch.annotate(entries, ch.read_journal(journal if journal and journal.is_file() else None)[1])
                if ch.select_entries(entries, 0, categories, sources):
                    return True
                cursor = size  # only queries, views or the AI's own calls: skip them
                ch.save_cursor(log.key, cursor)
            time.sleep(poll)
        return False


def refine_project_path(snapshot: dict[str, Any], entries: list[ch.Entry]) -> None:
    """Use the session's last newProject / openProject / saveProjectAs of this project as its path: DianaIE's
    currentProject() keeps a stale path after saveProjectAs (even in the saved file), which would mix up the
    baselines of the original and the copy."""
    project = snapshot.get("project") or {}
    name = project.get("name")
    if not name:
        return
    for entry in reversed(entries):
        if entry.name in ("newProject", "openProject", "saveProjectAs") and entry.args and isinstance(entry.args[0], str):
            # DianaIE reports the path the way Windows sees it ('W:\\jobs\\x.dpf'); store it host-side.
            path = config.to_host(entry.args[0])
            if path.suffix.lower() != ".dpf":
                path = path.with_name(path.name + ".dpf")
            if path.stem == name:
                if project.get("path") and Path(project["path"]) != path:
                    project.setdefault("stored_path", project["path"])
                project["path"] = path.as_posix()
            return


def format_report(report: dict[str, Any], max_lines: int = 40) -> str:
    """Compact Chinese text of a changes() result, for follow-mode notifications."""
    lines = [f"[DianaIE 变更 {time.strftime('%H:%M:%S')}]"]
    operations = report.get("operations") or []
    if operations:
        lines.append(f"你的操作（{report.get('operations_total', len(operations))} 步）：")
        for op in operations[-max_lines:]:
            mark = "（已撤销）" if op.get("undone") else ""
            line = f"  · {op['what']}{mark}"
            if op.get("reverted"):
                line += " → 撤销/重做了：" + "；".join(op["reverted"])
            lines.append(line)
    model_changes = report.get("model_changes")
    if model_changes:
        lines.append("模型净变化（与上次查看相比）：")
        for change in model_changes[:max_lines]:
            by = {"user": "", "ai": "（AI）", "user+ai": "（你和 AI）"}.get(change.get("by") or "", "")
            lines.append(f"  · {change['text']}{by}")
        if len(model_changes) > max_lines:
            lines.append(f"  … 另有 {len(model_changes) - max_lines} 项")
    elif model_changes is not None and operations:
        lines.append("模型净变化：无（操作已被撤销，或只改了视图/结果显示）")
    for note in report.get("notes") or []:
        lines.append(f"注：{note}")
    return "\n".join(lines)
