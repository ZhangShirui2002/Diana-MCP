"""MCP server that lets an AI assistant drive a live DIANA FEA session (the DianaIE GUI)."""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import __version__, autostart, config, episodes, guide, mining, usage
from . import changes as history
from .api_lessons import ApiLessons, error_line
from .api_reference import ApiReference, ApiReferenceStore, dump_code, generate_with_batch_dianaie
from .bridge_client import BridgeError, BridgeTimeout
from .community import CommunityError, CommunityStore, ContributionManager
from .experience import STATUSES, TYPES, ExperienceError, ExperienceStore
from .glossary import GlossaryError, GlossaryStore
from .jobs import Job, JobError, JobManager, referenced_files
from .knowledge import Context, KnowledgeBase
from .manual import ManualStore
from .out_parser import parse_out, tail
from .review_gate import GateError, GuardStore, file_hash, state_fingerprint
from .schema import load_schema
from .session import Session, SessionManager, manual_attach_command, settable_path
from .snapshots import SnapshotStore, diff, project_key, snapshot_saved_project
from .tb_parser import TbError, read_tb_rows, scan_tb, select_blocks, write_csv
from .tracking import Tracker, TrackingError

# Clients cut long instructions (Claude Code keeps the first 2048 characters): only the essentials here, the rest in
# diana_guide. tests/test_server.py checks the length.
INSTRUCTIONS = """\
Controls a live DianaIE on Windows or Linux/Wine; use paths as spelled on that host. Connect with diana_start. Before \
modeling, read diana_guide once for the full workflow, verified scripting facts, GUI change tracking and handbook rules.

Essentials:
- Look things up instead of guessing: diana_api_search/diana_api_doc (script commands), diana_keywords (data-model \
keywords), diana_parameters (paths and values of an item), diana_manual_search/diana_manual_read, \
diana_knowledge_search (handbook, manual, examples, keywords and API at once; Chinese questions work).
- diana_execute runs Python 3.7 on DianaIE's GUI thread; DIANA commands are globals. Use absolute paths. Never call \
pause() or anything that opens a dialog. newProject/openProject/importModel/closeProject discard unsaved changes: \
save or ask first.
- setParameter takes the path below the item's model directory ("TENSIL/TENSTR", "THICK", "FORCE/VALUE"); other \
forms return False without an error, so check the return value.
- DIANA has no fixed units: check them (diana_model_summary) and keep one system.
- diana_solve without an authorization opens mandatory GUI review; only a GUI-approved review, validated baseline, \
or approved parameter campaign may start a background solve. diana_run_solver has the same gate. Poll \
diana_job_status; filter result tables and write large ones to CSV.
- When the user says they changed the model in the GUI, call diana_changes first.
- On a DIANA error or a suspicious result, read related_experience in the tool result and search \
diana_knowledge_search before debugging from scratch. 未验证 entries are hints: say so and check them.
- Record lessons with diana_experience_add (always a draft). Mark an entry verified only after the user explicitly \
agrees in this conversation.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
BLOCKED_SOLVER_CALLS = {"runSolver", "runStabor", "runPipe", "runQuake", "runRCBeam", "runSealec", "runSlab"}

# Split deployments run two servers: the handbook, manual, API index and experience store where the knowledge
# lives (a laptop), the live DianaIE, solver and result files where DIANA runs. DIANA_MCP_PROFILE picks the half;
# "full" (the default) keeps everything in one server.
KNOWLEDGE_TOOLS = frozenset({
    "diana_guide", "diana_keywords", "diana_glossary", "diana_api_search", "diana_api_doc",
    "diana_manual_search", "diana_manual_read", "diana_knowledge_search", "diana_knowledge_feedback",
    "diana_experience_add", "diana_experience_update", "diana_experience_list", "diana_experience_verify",
    "diana_experience_export", "diana_experience_review", "diana_experience_mine_logs",
    "diana_community_status", "diana_community_sync", "diana_community_prepare", "diana_community_preview",
    "diana_community_contribute",
})
PROFILE = (os.environ.get("DIANA_MCP_PROFILE") or "full").strip().lower()
if PROFILE not in ("full", "compute", "knowledge"):
    raise SystemExit(f"DIANA_MCP_PROFILE must be full, compute or knowledge (got {PROFILE!r})")
if PROFILE == "compute":
    INSTRUCTIONS = ("DIANA runs here; handbook, manual, API and experience live in a second diana MCP server.\n\n"
                    + INSTRUCTIONS)
elif PROFILE == "knowledge":
    INSTRUCTIONS = ("Knowledge only: handbook, manual, API, glossary, experience. The live DianaIE, solver and "
                    "results are a second diana MCP server.\n\n") + INSTRUCTIONS

mcp = MCPServer("diana", instructions=INSTRUCTIONS, version=__version__)
sessions = SessionManager()
api_store = ApiReferenceStore()
guard = GuardStore()
jobs = JobManager(guard=guard)
manuals = ManualStore()
snapshots = SnapshotStore()
tracker = Tracker(sessions, snapshots)
experiences = ExperienceStore()
community = CommunityStore()
contributions = ContributionManager(experiences, community=community)
glossaries = GlossaryStore()


def _learn(job: Job, manager: JobManager) -> None:
    episodes.learn(job, manager, experiences)


def _jobs() -> JobManager:
    """The job manager, with the learning hook attached (also to a manager swapped in by tests)."""
    if _learn not in jobs.on_finished:
        jobs.on_finished.append(_learn)
    return jobs


def _known_commands() -> set[str] | None:
    try:
        path = api_store.path_for(config.default_install().version)
        return set(ApiReference.load(path).entries) if path.is_file() else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def _related(texts: list[str], context: str, limit: int = 3) -> list[dict[str, Any]]:
    try:
        boost = {i: u.factor for i, u in usage.experience_usage().items()}
        found = experiences.recommend(texts, limit, boost=boost)
        usage.log_recommendation(context, [r["id"] for r in found])
        return found
    except Exception:  # recommendations are a bonus; never fail the tool over them
        return []


_tips_shown = False
_warming = False


def _installed_version() -> str | None:
    try:
        return config.default_install().version
    except FileNotFoundError:
        return None


def _with_tips(result: dict[str, Any]) -> dict[str, Any]:
    """Add the handbook's resident tips to the first tool result of this server process. Clients cut long
    instructions, so the tips travel with the first call of a session instead, and cost nothing when DIANA is not
    used."""
    global _tips_shown
    if not _tips_shown:
        _tips_shown = True
        try:
            from .tips import resident_tips

            tips = resident_tips(experiences, usage.experience_usage(), _installed_version())
        except Exception:  # a broken handbook must never break a tool
            tips = {}
        if tips:
            result["handbook_tips"] = tips
    return result


def _warm_up_vectors() -> None:
    """Load the semantic search model in the background once a DIANA session starts, so that the first
    diana_knowledge_search does not wait for it (only when a vector model is configured)."""
    global _warming
    if _warming:
        return
    _warming = True

    def run() -> None:
        try:
            from . import vector

            vector.warm_up()
        except Exception:  # only a head start: a search loads the model itself
            pass

    threading.Thread(target=run, name="diana-mcp-vector-warm-up", daemon=True).start()


def _tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, BridgeTimeout):
        return ToolError(
            f"{exc}. DianaIE may still be running the code (long operation or an open dialog); "
            "call diana_status before sending more work, or raise timeout_seconds."
        )
    return ToolError(str(exc))


@mcp.tool(annotations=READ_ONLY)
def diana_status() -> dict[str, Any]:
    """Report DIANA installations, live DianaIE bridge sessions and cached API references."""
    try:
        installs = [{"version": i.version, "home": str(i.home)} for i in config.find_installs()]
    except FileNotFoundError as exc:
        installs = [{"error": str(exc)}]
    current_pid = sessions.current.pid if sessions.current else None
    try:
        auto = autostart.status(config.default_install())
    except (FileNotFoundError, OSError) as exc:
        auto = {"error": str(exc)}
    live = sessions.discover()
    bridged = {s.pid for s in live}
    return _with_tips({
        "installs": installs,
        "sessions": [dict(s.describe(), current=s.pid == current_pid) for s in live],
        "dianaie_without_bridge": [
            {"pid": pid, "history_file": str(log.path)}
            for pid, log in history.logs_of_running_dianaie().items() if pid not in bridged
        ],
        "autostart": auto,
        "api_reference_cached_versions": api_store.cached_versions(),
        "manual_attach": (
            "To connect to a DianaIE the user opened themselves, have them run this in its command console, "
            "then call diana_start (or enable diana_autostart once): " + manual_attach_command()
        ),
        "state_dir": str(config.state_dir()),
    })


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def diana_guide(topic: Literal["all", "workflow", "scripting", "tracking", "knowledge", "rules"] = "all") -> str:
    """The full guide to this server: workflow, verified DIANA 10.5 scripting facts, following the user's GUI edits,
    the experience handbook's rules, general rules, and the user's most useful verified lessons. Read it once per
    conversation before modeling; the server instructions only hold the essentials.

    Args:
        topic: all, or one part: workflow, scripting, tracking, knowledge, rules.
    """
    global _tips_shown
    try:
        from .tips import resident_section

        tips = resident_section(experiences, usage.experience_usage(), _installed_version())
    except Exception:  # a broken handbook must never break the guide
        tips = ""
    if tips and topic in ("all", "knowledge"):
        _tips_shown = True
    return guide.text(topic, tips)


@mcp.tool()
def diana_start(
    project_path: str | None = None,
    working_dir: str | None = None,
    attach_only: bool = False,
    timeout_seconds: float = 180,
) -> dict[str, Any]:
    """Connect to a live DianaIE session, launching DianaIE with the bridge if none is running.

    Args:
        project_path: absolute path of a DIANA project (.dpf) to open. Refused if another project is open,
            because opening a project discards unsaved changes in the current one.
        working_dir: working directory for a newly launched DianaIE (default: the diana-mcp work folder).
        attach_only: only attach to a running bridge; never launch DianaIE.
        timeout_seconds: how long to wait for a newly launched DianaIE to come up.
    """
    try:
        try:
            session = sessions.attach()
            action = "attached"
        except BridgeError:
            if attach_only:
                raise
            workdir = Path(working_dir) if working_dir else config.work_dir()
            session = sessions.launch(config.default_install(), workdir, timeout=timeout_seconds)
            action = "launched"
        if project_path:
            _open_project(session, Path(project_path))
    except (BridgeError, FileNotFoundError) as exc:
        raise _tool_error(exc) from exc
    _warm_up_vectors()
    return _with_tips({"action": action, **session.describe()})


def _open_project(session: Session, path: Path) -> None:
    if path.suffix.lower() != ".dpf" or not path.is_file():
        raise ToolError(f"not an existing DIANA project (.dpf): {path}")
    current = sessions.run_checked("print(projectName())", timeout=30).strip()
    if current and current != path.stem:
        raise ToolError(
            f"project '{current}' is open in DianaIE. Save it (diana_execute: saveProject()) or close it "
            "(closeProject()) first; opening another project would discard its unsaved changes."
        )
    if current != path.stem:
        opened = sessions.run_checked(f"print(openProject({str(path)!r}))", timeout=600).strip()
        if not opened:
            raise ToolError(f"DianaIE could not open {path}")
    session.info["project"] = path.stem


@mcp.tool()
def diana_execute(code: str, timeout_seconds: float = 300) -> dict[str, Any]:
    """Run Python code inside the live DianaIE session (DianaIE's embedded Python 3.7, GUI thread).

    DIANA script commands are globals and state persists across calls. Returns stdout, stderr and the
    repr of the final expression's value; a Python exception becomes a tool error with the traceback.
    Never call pause() or anything that opens a dialog (it blocks until the user closes it). newProject, openProject,
    importModel and closeProject discard unsaved changes. setParameter needs the path below the item's model
    directory (diana_parameters shows it) and returns False, changing nothing, for other forms.

    Args:
        code: Python 3.7 source. Use absolute paths.
        timeout_seconds: how long to wait for DianaIE to finish; raise it for meshing or solver runs.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        tree = None  # DianaIE will return the authoritative Python 3.7 syntax error.
    if tree is not None:
        direct_solver_calls = []
        for node in ast.walk(tree):
            name = node.id if isinstance(node, ast.Name) else (
                node.attr if isinstance(node, ast.Attribute) else (
                    node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""
                )
            )
            if name in BLOCKED_SOLVER_CALLS:
                direct_solver_calls.append(str(name))
        if direct_solver_calls:
            raise ToolError(
                "禁止通过 diana_execute 直接提交计算（发现 %s）；请使用 diana_solve / diana_run_solver，"
                "它们会执行人工审阅或已批准参数方案的提交门禁。" % ", ".join(sorted(set(direct_solver_calls)))
            )
    try:
        response = sessions.execute(code, timeout=timeout_seconds)
    except BridgeError as exc:
        raise _tool_error(exc) from exc
    lessons = ApiLessons(known=_known_commands())
    if not response.get("ok"):
        error = response.get("error") or "DianaIE reported an unknown error"
        parts = [error]
        if response.get("stdout"):
            parts.append("--- stdout before the error ---\n" + response["stdout"])
        if response.get("stderr"):
            parts.append("--- stderr ---\n" + response["stderr"])
        lessons.record_failure(code, error)
        related = _related([error_line(error), code], "execute")
        if related:
            parts.append("--- 相关经验（diana_experience_list(id=...) 查看全文；未验证的只是线索）---\n" + "\n".join(
                f"- {r['id']}（{r['status']}）：{r['title']}" for r in related))
        raise ToolError("\n".join(parts))
    result = {k: response[k] for k in ("stdout", "stderr", "value", "seconds") if response.get(k) not in (None, "")}
    drafts = [d for d in (_draft_api_lesson(lesson) for lesson in lessons.resolve(code)) if d]
    if drafts:
        result["auto_drafts"] = drafts
        result["auto_drafts_note"] = ("刚才报错的命令这次换了参数后成功，已起草为 api-usage 经验（未验证）："
                                      "用 diana_experience_update 补充原因，再询问用户是否批准。")
    return _with_tips(result)


def _draft_api_lesson(lesson: dict[str, str]) -> dict[str, str] | None:
    message = re.sub(r"^[\w.]+:\s*", "", lesson["error"])
    key = mining.normalize(message)[:60]
    for entry in experiences.entries():
        if lesson["command"] in entry.triggers and key and key in mining.normalize(entry.body):
            return None  # this lesson is already recorded
    try:
        entry, _ = experiences.add(
            title=f"{lesson['command']}：报错“{message[:50]}”，改用另一种写法后成功",
            kind="api-usage",
            symptom=f"调用\n\n```python\n{lesson['failed']}\n```\n\n报错：`{lesson['error']}`",
            cause="（待 AI 分析补充：两种写法的区别以及为什么前者报错。）",
            fix=f"改为\n\n```python\n{lesson['worked']}\n```\n\n后调用成功。",
            verification=f"{time.strftime('%Y-%m-%d %H:%M')} 同一会话中随后的调用成功（diana_execute）。",
            triggers=[lesson["command"]] + ([key] if key else []),
            tags=["自动起草", "脚本"], source="ai", diana_version=episodes._installed_version(),
        )
    except ExperienceError:
        return None
    return {"id": entry.id, "title": entry.title}


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def diana_screenshot(
    view: Literal["current", "MESH", "RESULT"] = "current",
    viewpoint: Literal["current", "top", "bottom", "front", "back", "left", "right", "iso1", "iso2"] = "current",
    fit: bool = True,
    width: int = 1280,
    height: int = 800,
) -> list[Any]:
    """Capture DianaIE's 3D window as a PNG image to check the model visually.

    Args:
        view: switch to the MESH or RESULT view first, or keep the current one (geometry view by default).
        viewpoint: camera direction; 2D models in the XY plane are best seen from "top".
        fit: zoom so the whole model fits.
        width: image width in pixels (200-4096).
        height: image height in pixels (200-4096).
    """
    width, height = (max(200, min(4096, int(v))) for v in (width, height))
    path = config.screenshots_dir() / f"{uuid.uuid4().hex}.png"
    try:
        sessions.screenshot(path, view, viewpoint, fit, width, height)
        data = path.read_bytes()
    except BridgeError as exc:
        raise _tool_error(exc) from exc
    finally:
        path.unlink(missing_ok=True)
    return [f"DianaIE {view} view, viewpoint {viewpoint}, {width}x{height}", Image(data=data, format="png")]


@mcp.tool(annotations=READ_ONLY)
def diana_model_summary() -> dict[str, Any]:
    """Summarize the open project: dimension, analysis aspects, shapes, materials, geometries, groups,
    load cases, each analysis with its commands, the current units (DIANA has no fixed units: all input must use
    one consistent system) and the issues reported by validateModel('ALL')."""
    try:
        summary = sessions.model_summary()
    except BridgeError as exc:
        raise _tool_error(exc) from exc
    related = _related(summary.get("validation_issues") or [], "validate")
    _condense_summary(summary)
    if related:
        summary["related_experience"] = related
    return _with_tips(summary)


SUMMARY_LIST_LIMIT = 30
SUMMARY_ISSUE_LIMIT = 20


def _condense_summary(summary: dict[str, Any]) -> None:
    """Keep a big model's summary small: long name lists are cut (their lengths are reported) and repeated
    validation messages that differ only in names or numbers are merged with a count."""
    cut = {}
    for key, value in summary.items():
        if key != "validation_issues" and isinstance(value, list) and len(value) > SUMMARY_LIST_LIMIT:
            summary[key] = value[:SUMMARY_LIST_LIMIT]
            cut[key] = len(value)
    if cut:
        summary["lists_cut"] = {"total": cut, "note": f"只列出前 {SUMMARY_LIST_LIMIT} 项；完整列表用 diana_execute 读取"}
    issues = [str(i) for i in summary.get("validation_issues") or []]
    groups: dict[str, list[Any]] = {}
    for issue in issues:
        groups.setdefault(mining.normalize(issue), [issue, 0])[1] += 1
    if len(groups) < len(issues) or len(groups) > SUMMARY_ISSUE_LIMIT:
        ranked = sorted(groups.values(), key=lambda g: -g[1])[:SUMMARY_ISSUE_LIMIT]
        summary["validation_issues"] = [text if n == 1 else f"{text}（同类共 {n} 条）" for text, n in ranked]
        summary["validation_issues_total"] = len(issues)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def diana_keywords(query: str, limit: int = 12) -> str:
    """Look up DIANA data-model keywords in the installation's schema (lib/SCHEMA).

    Finds material classes and models for addMaterial (e.g. CONCR / TSCR), geometry and element classes for
    addGeometry / setElementClassType (e.g. SHEET / MEMBRA / PLSTRA / AXISYM), load and support types, and
    parameters with title, unit, domain, default and allowed values. The manual does not list these keywords.
    The directories shown in brackets ([tscr], [tensil], ...) form the setParameter path (e.g. TENSIL/TENSTR);
    use diana_parameters to see the exact paths of an existing item.

    Args:
        query: words or keywords, e.g. "masonry", "tensile strength", "MEMBRA", "TSCR", "LODTYP".
        limit: number of entries to return.
    """
    try:
        schema = load_schema(config.default_install())
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc
    hits = schema.search(query, limit=max(1, min(limit, 50)))
    if not hits:
        return f"No schema keyword matches {query!r}; try other words or diana_manual_search."
    return "\n".join([f"DIANA schema entries for {query!r}:"] + [f"- {e.one_line(schema.context(e))}" for e in hits])


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def diana_parameters(item_type: str, name: str) -> str:
    """List every parameter of one item of the open project with its current value, plus the schema's title,
    unit, domain and allowed values.

    Each line shows the form setParameter accepts ("set as", e.g. LINEAR/ELASTI/YOUNG): the full path listed first
    does NOT work there (setParameter then returns False and changes nothing), while parameter() reads with any
    form. Call it after addMaterial / addGeometry / createPointLoad etc. to see what the chosen class and model
    offer; parameters appear once they have a value.

    Args:
        item_type: MATERIAL, GEOMET, DATA, GEOMETRYLOAD, GEOMETRYSUPPORT, GEOMETRYTYING, GEOMETRYCONNECTION,
            GEOMETRYBC, GEOMETRYFIXPOT or GEOMETRYINIFIELD.
        name: name of the item.
    """
    try:
        rows = sessions.parameters(item_type.upper(), name)
    except BridgeError as exc:
        raise _tool_error(exc) from exc
    try:
        schema = load_schema(config.default_install())
    except FileNotFoundError:
        schema = None
    lines = [f"{item_type.upper()} {name!r}: {len(rows)} parameters"]
    for path, value in rows:
        parts = [p for p in path.strip("/").split("/") if p]
        info = ""
        if schema and parts:
            entry = schema.describe(re.sub(r"\(\d*\)$", "", parts[-1]), parts[-2] if len(parts) > 1 else None)
            if entry:
                bits = [entry.title] if entry.title else []
                bits += [f"unit {entry.quantity}"] if entry.quantity else []
                bits += [f"domain {entry.domain}"] if entry.domain else []
                bits += [f"default {entry.default}"] if entry.default else []
                if entry.enum:
                    bits.append("values " + "; ".join(f"{k}={v}" for k, v in entry.enum[:25]))
                info = " — " + ", ".join(bits) if bits else ""
        settable = settable_path(path)
        where = f" (set as {settable!r})" if settable else ""
        lines.append(f"- {path}{where} = {value!r}{info}")
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def diana_analysis_commands(analysis: str) -> str:
    """An analysis of the open project as DIANA command-file (.dcf) text, written by saveAnalysisCommands.

    Use it to check what setAnalysisCommandDetail did. Detail paths follow the .dcf block nesting joined by '/',
    with (n) numbering repeated blocks: EXECUT(1)/LOAD/STEPS/EXPLIC/SIZES, EXECUT/NMODES, OUTPUT(2)/LAYOUT/LINPAG.
    The keywords are documented in manual Appendices H (analysis procedures) and I (output); the path spelling was
    found by experiment, so verify each change here.
    """
    path = config.state_dir() / "tmp" / f"{uuid.uuid4().hex}.dcf"
    try:
        return sessions.analysis_commands(analysis, path)
    except BridgeError as exc:
        raise _tool_error(exc) from exc
    finally:
        path.unlink(missing_ok=True)


def _projects_in(log: history.HistoryFile) -> list[str]:
    entries, _ = history.read_log(log.path)
    found = [str(e.args[0]) for e in entries if e.name in ("newProject", "openProject", "saveProjectAs") and e.args]
    return list(dict.fromkeys(Path(p).stem for p in found))


@mcp.tool(annotations=READ_ONLY)
def diana_command_log(
    lines: int = 60,
    find: str | None = None,
    session: str | None = None,
    edits_only: bool = False,
    source: Literal["all", "user", "ai"] = "all",
    list_sessions: bool = False,
) -> dict[str, Any]:
    """DianaIE's command history: every command of a DianaIE session, GUI clicks included, as the exact Python call
    with a plain-language description, who ran it (user = GUI or command console, ai = this server's bridge,
    unknown = no bridge journal) and, for undo/redo, what it reverted (undone steps are marked).

    Works without a bridge and for earlier sessions. To learn the exact call behind a GUI action, ask the user to do
    it once and read it here. For "what did the user change since I last looked" use diana_changes instead.

    Args:
        lines: number of entries to return, counted from the end.
        find: only entries whose call or description contains this text (case-insensitive).
        session: an earlier session's history file or its key (IEhistory_yymmdd_hhmmss, see list_sessions);
            default the DianaIE being tracked (the bridge's, else a running one, else the newest).
        edits_only: leave out views, result plots, queries and reports.
        source: only the user's or only the AI's commands.
        list_sessions: list the DianaIE sessions on disk (start time, running or not, projects opened) instead.
    """
    try:
        if list_sessions:
            files = history.history_files()[-30:]
            return {"sessions": [f.describe() | {"key": f.key, "projects": _projects_in(f)} for f in reversed(files)]}
        if session:
            log = history.find_history(session)
            if log is None:
                raise ToolError(f"no DianaIE history file {session!r}; call with list_sessions=True")
            journal = history.journal_path(log.key)
        else:
            log, journal = tracker.current_log(tracker.live_session())
    except TrackingError as exc:
        raise ToolError(str(exc)) from exc
    data = history.load_history(log, journal, _schema_or_none())
    entries = data.entries
    if edits_only:
        entries = [e for e in entries if e.category in history.DEFAULT_CATEGORIES]
    if source != "all":
        entries = [e for e in entries if e.source == source]
    if find:
        needle = find.lower()
        entries = [e for e in entries if needle in e.text.lower() or needle in e.description.lower()]
    count = max(1, min(int(lines), 2000))
    return {
        "history_file": str(log.path),
        "tracking": "journal" if data.journal else "history only (no bridge journal: source unknown)",
        "matches": len(entries),
        "entries": [e.view() for e in entries[-count:]],
    }


def _schema_or_none() -> Any:
    try:
        return load_schema(config.default_install())
    except (FileNotFoundError, OSError):
        return None


@mcp.tool(annotations=READ_ONLY)
def diana_changes(
    wait_seconds: float = 0,
    quiet_seconds: float = 3,
    include_view: bool = False,
    include_ai: bool = False,
    peek: bool = False,
    max_operations: int = 50,
) -> dict[str, Any]:
    """What changed in DianaIE since you last looked.

    Returns (1) the user's operations since the last call, in order, each with a description, undone steps marked
    and what every undo/redo reverted, and (2) the net model changes compared with the last snapshot of this project:
    items added/removed and parameters with old -> new values (materials, geometries, loads with their attachment
    points, supports, shapes with bounding boxes, mesh size, analysis commands as .dcf lines, units), each attributed
    to the user or the AI where the history shows it. The baseline is kept on disk per project, so edits made in an
    earlier DianaIE session show up too. Without a bridge only the operations are available.

    Args:
        wait_seconds: first wait up to this long for new edits, returning once the user has paused.
        quiet_seconds: how long the user must pause before returning (with wait_seconds).
        include_view: also list view changes and result plots.
        include_ai: also list your own operations (hidden by default; their count is reported).
        peek: do not advance the "last looked" point.
        max_operations: most recent operations to list (the total is always reported); model changes are cut at
            twice this number.
    """
    categories = history.ALL_CATEGORIES if include_view else history.DEFAULT_CATEGORIES
    sources = ("user", "unknown", "ai") if include_ai else ("user", "unknown")
    limit = max(1, int(max_operations))
    try:
        if wait_seconds > 0:
            tracker.wait_for_changes(min(float(wait_seconds), 3600), quiet=max(0.5, float(quiet_seconds)),
                                     categories=categories, sources=sources)
        result = tracker.changes(advance=not peek, categories=categories, sources=sources, max_operations=limit)
    except (TrackingError, BridgeError) as exc:
        raise _tool_error(exc) from exc
    found = result.get("model_changes") or []
    if len(found) > 2 * limit:
        result["model_changes"] = found[:2 * limit]
        result["model_changes_total"] = len(found)
    return result


def _snapshot_counts(snapshot: dict[str, Any]) -> dict[str, int]:
    return {k: len(v) for k, v in snapshot.items() if isinstance(v, (dict, list)) and k not in ("project", "errors")}


@mcp.tool()
def diana_model_snapshot(label: str | None = None, dpf_path: str | None = None) -> dict[str, Any]:
    """Save a snapshot of the model (every item with all parameters) as a named checkpoint for diana_model_diff.

    Args:
        label: name for the checkpoint, e.g. "before mesh refinement".
        dpf_path: snapshot this saved project instead of the open one; a short batch DianaIE reads it, the live
            session is not touched (needs a free license seat, ~30 s).
    """
    try:
        if dpf_path:
            snapshot = snapshot_saved_project(config.default_install(), Path(dpf_path))
            info = snapshots.save(snapshot, label=label, origin="file")
        else:
            snapshot = tracker.take_live()
            info = snapshots.save(snapshot, label=label)
    except (BridgeError, FileNotFoundError, RuntimeError) as exc:
        raise _tool_error(exc) from exc
    return info.describe() | {"counts": _snapshot_counts(snapshot), "errors": snapshot.get("errors")}


def _resolve_snapshot(ref: str, key: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(snapshot, description) for "current", "latest", an id, a label or a .dpf path."""
    if ref == "current":
        snapshot = tracker.take_live()
        return snapshot, {"ref": "current", "taken": "now"}
    if ref.lower().endswith(".dpf"):
        snapshot = snapshot_saved_project(config.default_install(), Path(ref))
        return snapshot, {"ref": ref, "origin": "file"}
    info = snapshots.latest(key) if ref == "latest" and key else snapshots.find(ref, key)
    if info is None:
        raise ToolError(f"no snapshot {ref!r}" + (f" for project {key}" if key else "") +
                        "; take one with diana_model_snapshot")
    return snapshots.load(info), info.describe()


@mcp.tool(annotations=READ_ONLY)
def diana_model_diff(before: str = "latest", after: str = "current", max_changes: int = 100) -> dict[str, Any]:
    """Compare two states of a model: items added/removed and parameters with old -> new values.

    Each side is "current" (the project open in DianaIE, now), "latest" (the last stored snapshot of that project,
    e.g. from diana_changes or diana_model_snapshot), a snapshot id or label, or the path of a saved .dpf (read by a
    batch DianaIE). Comparing does not move diana_changes' baseline.

    Args:
        before: the older state.
        after: the newer state.
        max_changes: changes to return.
    """
    try:
        after_snap, after_info = _resolve_snapshot(after, None) if after != "latest" else (None, None)
        key = None
        if after_snap is not None:
            project = after_snap.get("project") or {}
            key = project_key(project.get("path"), project.get("name"))
        before_snap, before_info = _resolve_snapshot(before, key)
        if after_snap is None:
            project = before_snap.get("project") or {}
            after_snap, after_info = _resolve_snapshot("latest", project_key(project.get("path"), project.get("name")))
    except (BridgeError, FileNotFoundError, RuntimeError) as exc:
        raise _tool_error(exc) from exc
    found = diff(before_snap, after_snap)
    return {
        "before": before_info,
        "after": after_info,
        "count": len(found),
        "changes": [c.view() for c in found[: max(1, int(max_changes))]],
    }


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=False))
def diana_autostart(action: Literal["status", "enable", "disable"] = "status") -> dict[str, Any]:
    """Make every DianaIE the user opens start the bridge by itself, so change tracking and all live tools work
    without the command-console line or diana_start.

    enable writes two small files (diana_mcp_autostart.pth / .py) into the user site-packages folder of DianaIE's
    embedded Python; they do nothing outside DianaIE. The bridge listens on 127.0.0.1 only, with a random token in
    the user's local app data. Only enable or disable it when the user asks; it takes effect for DianaIE sessions
    started afterwards.
    """
    try:
        install = config.default_install()
        return {"status": autostart.status, "enable": autostart.enable, "disable": autostart.disable}[action](install)
    except (FileNotFoundError, FileExistsError, OSError) as exc:
        raise ToolError(str(exc)) from exc


def _safe_name(text: str) -> str:
    return re.sub(r"[^\w.-]+", "_", text).strip("_") or "analysis"


def _live_project_state(analysis: str | None = None) -> dict[str, Any]:
    state = json.loads(sessions.run_checked(
        "import json\nprint(json.dumps({'path': currentProject(), 'name': projectName(), "
        "'analyses': list(analyses()), 'version': str(diaVersion())}))", timeout=30
    ).strip().splitlines()[-1])
    if not state.get("path"):
        raise ToolError("no project is open in DianaIE")
    if analysis is not None and analysis not in state.get("analyses", []):
        raise ToolError(f"no analysis named {analysis!r}; the project has {state.get('analyses', [])}")
    name = _safe_name(state.get("name") or Path(state["path"]).stem)
    project = config.to_host(state["path"]).parent / f"{name}.dpf"
    state.update({"safe_name": name, "project": project})
    return state


def _export_live_inputs(analysis: str, folder: Path, name: str) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    dat, dcf = folder / f"{name}.dat", folder / f"{name}.dcf"
    sessions.run_checked(
        f"exportModel({config.to_windows(dat)!r}, 8)\n"
        f"saveAnalysisCommands({analysis!r}, {config.to_windows(dcf)!r}, 6)", timeout=600
    )
    if not (dat.is_file() and dcf.is_file()):
        raise ToolError(f"DianaIE did not export {dat.name} and {dcf.name} to {folder}")
    return dat, dcf


def _validation_issues() -> list[str]:
    result = sessions.run_checked(
        "import json\nprint(json.dumps([str(i) for i in validateModel('ALL')]))", timeout=300
    )
    return json.loads(result.strip().splitlines()[-1])


def _submission_self_check(dat: Path, dcf: Path, validation_issues: list[str] | None = None) -> dict[str, Any]:
    """Small, deterministic checks run before every permit; JobManager adds reference-file checks afterwards."""
    checks: list[str] = []
    failures: list[str] = []
    for label, path in (("模型 .dat", dat), ("求解命令 .dcf", dcf)):
        if path.is_file() and path.stat().st_size:
            checks.append(f"{label} 存在且非空")
        else:
            failures.append(f"{label} 不存在或为空：{path}")
    if dcf.is_file() and re.search(rb"(?mi)^\s*\*[A-Z][A-Z0-9]*\b", dcf.read_bytes()):
        checks.append(".dcf 包含 DIANA 分析命令")
    else:
        failures.append(".dcf 没有可识别的 DIANA 分析命令")
    if validation_issues is not None:
        if validation_issues:
            failures.extend(f"validateModel: {issue}" for issue in validation_issues)
        else:
            checks.append("validateModel('ALL') 未报告问题")
    return {"passed": not failures, "checks": checks, "failures": failures}


def _copy_inputs(dat: Path, dcf: Path, folder: Path, name: str) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    targets = folder / f"{name}.dat", folder / f"{name}.dcf"
    for source, target in zip((Path(dat), Path(dcf)), targets):
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
    return targets


def _change_path(change: Any) -> str:
    prefixes = {
        "materials": "MATERIAL", "geometries": "GEOMETRY", "element_data": "ELEMENT_DATA",
        "loads": "LOAD", "supports": "SUPPORT", "tyings": "TYING",
        "boundary_conditions": "BOUNDARY_CONDITION", "fixed_potentials": "FIXED_POTENTIAL",
        "initial_fields": "INITIAL_FIELD", "shapes": "SHAPE", "mesh": "MESH",
        "analyses": "ANALYSIS", "units": "UNIT", "definitions": "DEFINITION",
        "directions": "DIRECTION", "load_cases": "LOAD_CASE", "support_sets": "SUPPORT_SET",
        "load_combinations": "LOAD_COMBINATION", "node_groups": "NODE_GROUP",
        "node_group_items": "NODE_GROUP_ITEM", "element_sets": "ELEMENT_SET",
        "reinforcement_sets": "REINFORCEMENT_SET", "functions": "FUNCTION", "project": "PROJECT",
    }
    parts = [prefixes.get(change.section, change.section.upper())]
    if change.item is not None:
        parts.append(str(change.item))
    if change.key:
        parts.append(str(change.key))
    return "/".join(part.strip("/") for part in parts if part != "")


def _review_summary(record: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "kind", "status", "created", "project", "analysis", "name", "baseline_id", "decision",
            "confirmation", "decided", "decision_source", "validation_issues", "parameters", "combination",
            "self_checks", "quality_checks", "state_fingerprint")
    return {key: record[key] for key in keys if key in record}


@mcp.tool()
def diana_review_prepare(analysis: str) -> dict[str, Any]:
    """Prepare a mandatory human review for a development-stage calculation and open it inside DianaIE.

    The 3D window is switched to a complete fitted model view. A modeless native review panel owned by DianaIE
    shows the full model snapshot, units, validation findings, changes since the preceding review and the complete
    analysis .dcf. The MCP cannot approve it: the user must click one of the panel's buttons. Closing the panel does
    not approve anything. Call diana_review_status afterwards, then pass the approved review_id to diana_solve.
    """
    staging = guard.root / "staging" / uuid.uuid4().hex
    try:
        state = _live_project_state(analysis)
        snapshot = tracker.take_live()
        dat, dcf = _export_live_inputs(analysis, staging, state["safe_name"])
        issues = _validation_issues()
        changes = []
        previous = guard.latest_review(str(state["project"]), analysis)
        if previous and Path(previous.get("snapshot", "")).is_file():
            before = json.loads(Path(previous["snapshot"]).read_text(encoding="utf-8"))
            changes = [change.view() for change in diff(before, snapshot)]
        record = guard.create_review(
            project=str(state["project"]), analysis=analysis, snapshot=snapshot, dat=dat, dcf=dcf,
            diana_version=state.get("version"), units=snapshot.get("units") or {},
            validation_issues=issues, changes=changes,
        )
        has_mesh = int((snapshot.get("mesh") or {}).get("elements") or 0) > 0
        sessions.show_review(guard.record_path("reviews", record["id"]), has_mesh=has_mesh)
    except (BridgeError, GateError, OSError, ValueError) as exc:
        raise _tool_error(exc) from exc
    finally:
        if staging.is_dir():
            for path in staging.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
            try:
                staging.rmdir()
            except OSError:
                pass
    return {
        "review_required": True,
        **_review_summary(record),
        "next": "请用户在 DianaIE 审阅窗口核对并点击决定；随后调用 diana_review_status。",
    }


@mcp.tool()
def diana_review_status(id: str | None = None) -> dict[str, Any]:
    """Read human-review, baseline and parameter-study status after the user acts in DianaIE.

    If a review was approved as a baseline, this call finalizes its immutable baseline artifacts. With no id it
    lists recent reviews, baselines and campaigns. It cannot approve any object.
    """
    try:
        if id is None:
            return {
                "reviews": [_review_summary(row) for row in guard.list_records("reviews")],
                "baselines": [_review_summary(row) for row in guard.list_records("baselines")],
                "campaigns": [_review_summary(row) for row in guard.list_records("campaigns")],
            }
        if id.startswith("review-"):
            record = guard.get_review(id)
            result = {"review": _review_summary(record)}
            if record.get("status") == "approved" and record.get("decision") == "approve_baseline":
                result["baseline"] = _review_summary(guard.finalize_baseline(id))
            return result
        if id.startswith("baseline-"):
            return {"baseline": _review_summary(guard.get_baseline(id))}
        if id.startswith("campaign-"):
            return {"campaign": _review_summary(guard.get_campaign(id))}
        raise GateError(f"unknown review/baseline/campaign id {id!r}")
    except GateError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def diana_parameter_study_prepare(
    baseline_id: str,
    name: str,
    parameters: list[dict[str, Any]],
    combination: str,
    self_checks: list[str],
    quality_checks: list[str],
) -> dict[str, Any]:
    """Propose one parameter-study campaign and open it for one-time human approval inside DianaIE.

    Each parameter needs a unique ``name`` and a model ``path``, plus either ``min``/``max`` or discrete ``values``.
    The approved plan may change geometry, mesh, material, load and solver settings named here. Cases inside that
    envelope need no per-case GUI review, but every case is range-checked and self-checked before submission.
    """
    try:
        record = guard.create_campaign(
            baseline_id, name=name, parameters=parameters, combination=combination,
            self_checks=self_checks, quality_checks=quality_checks,
        )
        baseline = guard.get_baseline(baseline_id)
        snapshot = json.loads(Path(baseline["snapshot"]).read_text(encoding="utf-8"))
        sessions.show_review(guard.record_path("campaigns", record["id"]),
                             has_mesh=int((snapshot.get("mesh") or {}).get("elements") or 0) > 0)
    except (BridgeError, GateError, OSError, ValueError) as exc:
        raise _tool_error(exc) from exc
    return {
        "approval_required": True,
        **_review_summary(record),
        "next": "请用户在 DianaIE 审阅参数范围、自检和质量规则并点击批准；随后调用 diana_review_status。",
    }


def _job_view(job: Job, tail_lines: int = 0) -> dict[str, Any]:
    info = job.describe()
    for key in ("recommended", "resolves", "resolved_by", "draft", "case"):
        info.pop(key, None)  # shown in plain words by job_advice below
    out = job.path(".out")
    if out.is_file():
        info["progress"] = parse_out(out).summary()
        if tail_lines:
            info["out_tail"] = tail(out, tail_lines)
    if job.state == "failed" and job.log.is_file():
        info["solver_log_tail"] = tail(job.log, 15)
    if job.state != "running":
        try:
            info.update(episodes.job_advice(job, jobs, experiences))
        except Exception:
            pass
    return info


@mcp.tool()
def diana_solve(
    analysis: str,
    job_dir: str | None = None,
    threads: int | None = None,
    keep_ff: bool = False,
    tb_no_paging: bool = True,
    tb_digits: int | None = None,
    review_id: str | None = None,
    baseline_id: str | None = None,
    campaign_id: str | None = None,
    case_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit the open DianaIE model through the mandatory development/baseline/parameter-study gate.

    With no authorization id this does **not** start a solver: it prepares the complete model and solver settings,
    fits the model in the DianaIE 3D view and opens the native human-review panel. After the user clicks a decision,
    call again with ``review_id``. A review approved as a baseline becomes trusted only after this validation run
    passes post-solve quality checks. Later calls may use that ``baseline_id`` or an approved ``campaign_id``;
    campaign variants are compared with the baseline snapshot and need no per-case human review while they stay in
    the approved parameter envelope and pass self-checks.

    Args:
        analysis: name of an analysis of the open project (see diana_model_summary).
        job_dir: folder for the job files; default <project folder>/<project>_jobs/<analysis>_<timestamp>.
        threads: threads for element loops (diana.exe -n); the sparse solver's threads are set in the .dcf
            (SOLVE PARDIS ... MAXTHR, manual H.6.1).
        keep_ff: keep the FILOS database (.ff, often hundreds of MB) after the run, e.g. to restart from it.
        tb_no_paging: write TABULA (.tb) output without page layout (LINPAG 0): no page headers and no column
            headers repeated every 66 lines. Only the job's copy of the .dcf is changed.
        tb_digits: significant digits of results in the .tb (1-10; DIANA's default is 4, e.g. 2.385E+01).
        review_id: GUI-approved development review for exactly one validation/debug run.
        baseline_id: trusted baseline produced by a human-reviewed validation run.
        campaign_id: one-time GUI-approved parameter-study plan for this variant.
        case_parameters: complete name/value map required by the approved campaign.
    """
    authorizations = [value for value in (review_id, baseline_id, campaign_id) if value]
    if len(authorizations) > 1:
        raise ToolError("review_id、baseline_id、campaign_id 只能指定一个")
    if not authorizations:
        return diana_review_prepare(analysis)

    staging = guard.root / "staging" / uuid.uuid4().hex
    try:
        state = _live_project_state(analysis)
        name, project = state["safe_name"], Path(state["project"])
        folder = Path(job_dir) if job_dir else (
            project.parent / f"{name}_jobs" / f"{_safe_name(analysis)}_{time.strftime('%Y%m%d-%H%M%S')}"
        )
        issues = _validation_issues()

        if review_id:
            reviewed = guard.get_review(review_id)
            if reviewed.get("analysis") != analysis or Path(reviewed.get("project", "")) != project:
                raise GateError("人工审阅记录不属于当前项目和分析")
            snapshot = tracker.take_live()
            current_dat, current_dcf = _export_live_inputs(analysis, staging, name)
            fingerprint = state_fingerprint(snapshot, current_dcf.read_bytes())
            self_check = _submission_self_check(Path(reviewed["dat"]), Path(reviewed["dcf"]), issues)
            permit = guard.permit_review(review_id, fingerprint, file_hash(current_dat), self_check)
            if reviewed.get("decision") == "approve_baseline":
                guard.finalize_baseline(review_id)
            dat, dcf = _copy_inputs(Path(reviewed["dat"]), Path(reviewed["dcf"]), folder, name)
        elif baseline_id:
            baseline = guard.get_baseline(baseline_id)
            if baseline.get("analysis") != analysis or Path(baseline.get("project", "")) != project:
                raise GateError("基准模型不属于当前项目和所指定的分析")
            dat, dcf = _copy_inputs(Path(baseline["dat"]), Path(baseline["dcf"]), folder, name)
            self_check = _submission_self_check(dat, dcf)
            if not self_check["passed"]:
                raise GateError("基准模型提交前自检失败：" + "; ".join(self_check["failures"]))
            permit = guard.permit_baseline(baseline_id)
        else:
            campaign = guard.get_campaign(str(campaign_id))
            baseline = guard.get_baseline(str(campaign["baseline_id"]))
            if baseline.get("analysis") != analysis or Path(baseline.get("project", "")) != project:
                raise GateError("参数分析方案的基准模型不属于当前项目和分析")
            snapshot = tracker.take_live()
            dat, dcf = _export_live_inputs(analysis, folder, name)
            before = json.loads(Path(baseline["snapshot"]).read_text(encoding="utf-8"))
            changes = [_change_path(change) for change in diff(before, snapshot)]
            self_check = _submission_self_check(dat, dcf, issues)
            permit = guard.permit_variant(
                str(campaign_id), dat, dcf, case_parameters or {}, changes=changes, self_check=self_check
            )

        job = _jobs().submit(dat, dcf, threads=threads, keep_ff=keep_ff, tb_no_paging=tb_no_paging, tb_digits=tb_digits,
                          search_dirs=[project.parent], analysis=analysis, project=str(project),
                          permit_id=permit["id"])
    except (BridgeError, GateError, JobError, FileNotFoundError, OSError, ValueError) as exc:
        raise _tool_error(exc) from exc
    finally:
        if staging.is_dir():
            for path in staging.iterdir():
                if path.is_file():
                    path.unlink(missing_ok=True)
            try:
                staging.rmdir()
            except OSError:
                pass
    view = _job_view(job)
    view["authorization"] = {
        "type": permit.get("source_type"), "source_id": permit.get("source_id"), "permit_id": permit["id"]
    }
    view["self_check"] = self_check
    if review_id and guard.get_review(review_id).get("decision") == "approve_baseline":
        view["baseline_candidate"] = _review_summary(guard.finalize_baseline(review_id))
    return view


@mcp.tool()
def diana_run_solver(
    dat_path: str,
    dcf_path: str,
    name: str | None = None,
    threads: int | None = None,
    overwrite: bool = False,
    keep_ff: bool = False,
    tb_no_paging: bool = True,
    tb_digits: int | None = None,
    restart_ff: str | None = None,
    baseline_id: str | None = None,
    campaign_id: str | None = None,
    case_parameters: dict[str, Any] | None = None,
    change_manifest: list[str] | None = None,
) -> dict[str, Any]:
    """Solve an already trusted baseline or approved parameter variant from .dat/.dcf files.

    This is the fast solver stage, not a way to build or bypass review. Exactly one ``baseline_id`` or
    ``campaign_id`` is required. A baseline pair must be byte-for-byte identical to the archived model that passed
    GUI review, validation solve and post-solve checks. A campaign variant must provide the complete approved
    parameter values and a semantic change manifest; both are range/envelope checked and all files are bound to a
    one-use permit before the common process-launch layer can run diana.exe.

    Outputs <name>.out/.tb/.dnb are written next to the .dat. A .dcf exported by DianaIE (analysis blocks only) is
    completed with the *FILOS/INITIA/*INPUT header (after any ':' comments and control commands) and *END that a
    standalone run needs; that and the TABULA layout options go into a copy, <name>.run.dcf. The original file is
    not modified. Relative input files it refers to (user DLLs via *FORTRAN USE, *INPUT READ FILE=, IMPORT) must be
    next to the .dcf or .dat; missing ones are reported before the run.

    Args:
        dat_path: absolute path of the model file (.dat).
        dcf_path: absolute path of the command file (.dcf).
        name: base name of the outputs (default: the .dat file name).
        threads: threads for element loops (diana.exe -n); solver threads are set by SOLVE PARDIS MAXTHR in the .dcf.
        overwrite: delete outputs of an earlier run with the same name first (DIANA appends to an existing .dnb).
        keep_ff: keep the FILOS database (.ff) after the run.
        tb_no_paging: write TABULA (.tb) output without page layout (LINPAG 0, COLLIN 1000).
        tb_digits: significant digits of results in the .tb (1-10; DIANA's default is 4).
        restart_ff: continue on this existing FILOS database (.ff) instead of initializing one: restarts, RESTOR,
            or a stand-alone *REDUCT / *ELIQUE / *ECREEP after the analysis that created it (manual H.2, H.16.4,
            H.17-H.19). The .dcf is then used as written (start it with *FILOS / UNLOCK after a killed run) and the
            database is kept.
        baseline_id: certified baseline whose exact files are being solved.
        campaign_id: GUI-approved parameter-study plan for a variant.
        case_parameters: complete name/value map required by the campaign.
        change_manifest: model/solver paths changed from the baseline, using the paths displayed in the campaign.
    """
    if bool(baseline_id) == bool(campaign_id):
        raise ToolError("必须且只能指定 baseline_id 或 campaign_id；纯文件批处理不能绕过基准模型/参数方案门禁")
    try:
        dat, dcf = Path(dat_path).resolve(), Path(dcf_path).resolve()
        self_check = _submission_self_check(dat, dcf)
        if not self_check["passed"]:
            raise GateError("批处理提交前自检失败：" + "; ".join(self_check["failures"]))
        if baseline_id:
            permit = guard.permit_baseline(baseline_id)
        else:
            campaign = guard.get_campaign(str(campaign_id))
            baseline = guard.get_baseline(str(campaign["baseline_id"]))
            changed_files = []
            if dat.is_file() and file_hash(dat) != baseline.get("dat_sha256"):
                changed_files.append(".dat")
            if dcf.is_file() and file_hash(dcf) != baseline.get("dcf_sha256"):
                changed_files.append(".dcf")
            if changed_files and not change_manifest:
                raise GateError("参数变体改变了 %s，但没有提供相对基准模型的 change_manifest" % ", ".join(changed_files))
            permit = guard.permit_variant(
                str(campaign_id), dat, dcf, case_parameters or {}, changes=change_manifest or [],
                self_check=self_check,
            )
        job = _jobs().submit(dat, dcf, name=name, threads=threads, overwrite=overwrite,
                          keep_ff=keep_ff, tb_no_paging=tb_no_paging, tb_digits=tb_digits,
                          ff_path=Path(restart_ff) if restart_ff else None, permit_id=permit["id"])
    except (GateError, JobError, FileNotFoundError, OSError, ValueError) as exc:
        raise _tool_error(exc) from exc
    view = _job_view(job)
    view["authorization"] = {
        "type": permit.get("source_type"), "source_id": permit.get("source_id"), "permit_id": permit["id"]
    }
    view["self_check"] = self_check
    return view


@mcp.tool(annotations=READ_ONLY)
def diana_job_status(job_id: str | None = None, tail_lines: int = 0) -> dict[str, Any]:
    """Status of a background solver job, or of the 20 most recent jobs when no id is given.

    Includes state (running/finished/failed/cancelled), elapsed time, output file sizes and the progress read from
    the .out file: analysis blocks, load steps, convergence and errors.

    Args:
        job_id: id returned by diana_solve or diana_run_solver.
        tail_lines: also return the last lines of the .out file.
    """
    try:
        if job_id is None:
            return {"jobs": [
                {k: v for k, v in _job_view(job).items() if k in ("id", "name", "state", "directory", "elapsed_s", "analysis", "progress")}
                for job in _jobs().list()
            ]}
        return _job_view(_jobs().get(job_id), tail_lines)
    except JobError as exc:
        raise _tool_error(exc) from exc


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
def diana_job_cancel(job_id: str) -> dict[str, Any]:
    """Stop a running solver job (kills diana.exe and its solver segment processes)."""
    try:
        return _job_view(_jobs().cancel(job_id))
    except JobError as exc:
        raise _tool_error(exc) from exc


@mcp.tool()
def diana_load_results(
    job_id: str | None = None, dnb_path: str | None = None, analysis: str | None = None, replace: bool = False
) -> dict[str, Any]:
    """Load solver results (.dnb) into an analysis of the open DianaIE project, to plot them there.

    Pass a job id from diana_solve (file and analysis are known), or dnb_path together with analysis.
    Returns the result cases and the result labels/components that diana_plot_result accepts.

    Args:
        job_id: a finished job started by diana_solve.
        dnb_path: absolute path of a .dnb file (instead of job_id).
        analysis: analysis to attach the results to (default: the job's analysis).
        replace: clear results already attached to the analysis first; loadResults refuses otherwise.
    """
    try:
        if job_id:
            job = _jobs().get(job_id)
            if job.state != "finished":
                raise ToolError(f"job {job_id} is {job.state}; results can be loaded once it has finished")
            dnb_path = dnb_path or str(job.path(".dnb"))
            analysis = analysis or job.analysis
        if not dnb_path or not analysis:
            raise ToolError("pass job_id, or dnb_path together with analysis")
        if not Path(dnb_path).is_file():
            raise ToolError(f"results file not found: {dnb_path}")
        code = (
            "import json\n"
            + (f"clearResults({analysis!r})\n" if replace else "")
            + f"if not loadResults({analysis!r}, {config.to_windows(dnb_path)!r}):\n"
            "    raise RuntimeError('loadResults failed; if the analysis already has results, pass replace=True')\n"
            f"print(json.dumps([list(r) for r in availableResults({{'analysis': {analysis!r}}})]))"
        )
        rows = json.loads(sessions.run_checked(code, timeout=600).strip().splitlines()[-1])
    except (BridgeError, JobError) as exc:
        raise _tool_error(exc) from exc
    cases = list(dict.fromkeys(r[0] for r in rows))
    grouped: dict[tuple, list[str]] = {}
    for case, block, label, location, component in rows:
        components = grouped.setdefault((block, label, location), [])
        if component not in components:
            components.append(component)
    return {
        "analysis": analysis,
        "dnb": dnb_path,
        "result_cases": cases if len(cases) <= 30 else cases[:15] + ["..."] + cases[-5:],
        "result_case_count": len(cases),
        "results": [{"block": b, "result": r, "location": loc, "components": c} for (b, r, loc), c in grouped.items()],
    }


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def diana_plot_result(
    analysis: str,
    result: str,
    component: str,
    case: str | None = None,
    block: str | None = None,
    item_type: Literal["node", "element", "reinforcement", "pile", "sheet-pile-wall", "anchor"] | None = None,
    location: Literal["node", "element"] | None = None,
    layer: str | None = None,
    plot: Literal["contours", "vectors", "diagram"] = "contours",
    viewpoint: Literal["current", "top", "bottom", "front", "back", "left", "right", "iso1", "iso2"] = "current",
    width: int = 1280,
    height: int = 800,
) -> list[Any]:
    """Plot a result in DianaIE's RESULT view and return a screenshot.

    Args:
        analysis: analysis with loaded results.
        result: result label from diana_load_results, e.g. "Displacements" or "Cauchy Total Stresses".
        component: component, e.g. "DtY", "SXX".
        case: result case (load case / step), default the first one.
        block: output block, needed only when the analysis has several.
        item_type: result owner; required by DianaIE unless the label starts with it (e.g. "Reinforcement ...")
            or the result is nodal.
        location: where the result was computed, "node" or "element" (see diana_load_results).
        layer: fibre / layer of beams and shells: a number, "maximum" or "minimum".
        plot: contours, vectors or diagram.
        viewpoint: camera direction; 2D models in the XY plane are best seen from "top".
        width: image width in pixels.
        height: image height in pixels.
    """
    case_path = "/".join(part for part in (analysis, block, case) if part)
    spec: dict[str, str] = {"result": result, "component": component}
    if item_type:
        spec["type"] = item_type
    if location:
        spec["location"] = location
    code = (
        "showView('RESULT')\n"
        f"setResultCase({case_path!r})\n"
        + (f"setResultLayer({int(layer) if str(layer).isdigit() else layer!r})\n" if layer else "")
        + f"if not selectResult({spec!r}):\n"
        f"    raise RuntimeError('selectResult found no {result}/{component}; check diana_load_results')\n"
        f"setResultPlot({plot!r})\n"
    )
    width, height = (max(200, min(4096, int(v))) for v in (width, height))
    path = config.screenshots_dir() / f"{uuid.uuid4().hex}.png"
    try:
        sessions.run_checked(code, timeout=300)
        sessions.screenshot(path, "current", viewpoint, True, width, height)
        data = path.read_bytes()
    except BridgeError as exc:
        raise _tool_error(exc) from exc
    finally:
        path.unlink(missing_ok=True)
    return [f"{analysis} / {case or 'first case'}: {result} {component} ({plot})", Image(data=data, format="png")]


@mcp.tool(annotations=READ_ONLY)
def diana_results_table(
    analysis: str,
    result: str,
    components: list[str] | None = None,
    cases: list[str] | None = None,
    block: str | None = None,
    location: Literal["node", "element", "mappedintpnt", "mappedcrack"] | None = None,
    nodes: list[int] | None = None,
    elements: list[int] | None = None,
    reinforcements: list[int] | None = None,
    piles: list[int] | None = None,
    layer: int | None = None,
    coordinate_system: Literal["local", "global"] | None = None,
    max_rows: int = 30,
    digits: int = 6,
    csv_path: str | None = None,
) -> dict[str, Any]:
    """Result values from results loaded in DianaIE (DianaIE's resultsTable), up to full double precision.

    Works on what diana_load_results lists (labels like "Displacements", "Cauchy Total Stresses"), including
    reinforcement, pile and layer results. Complements diana_read_tb, which reads the solver's .tb text output
    (4 significant digits unless tb_digits was raised).

    Args:
        analysis: analysis with loaded results.
        result: result label, e.g. "Displacements".
        components: component names, e.g. ["DtY"]; default all.
        cases: result case names; default all.
        block: output block, needed only when there are several.
        location: node, element, mappedintpnt or mappedcrack.
        nodes: node numbers.
        elements: element numbers.
        reinforcements: reinforcement numbers.
        piles: pile numbers.
        layer: layer / fibre number.
        coordinate_system: local or global.
        max_rows: rows to return inline; filter with nodes/elements/cases, or use csv_path, rather than raising it.
        digits: significant digits of the inline values; 0 = full double precision.
        csv_path: absolute path of a CSV file to write every row to (via DianaIE's exportResults).
    """
    options: dict[str, Any] = {"analysis": analysis, "result": result}
    for key, value in (("components", components), ("cases", cases), ("block", block), ("location", location),
                       ("nodes", nodes), ("elements", elements), ("reinforcements", reinforcements),
                       ("piles", piles), ("layer", layer), ("coordinate-system", coordinate_system)):
        if value is not None:
            options[key] = value
    code = (
        "import json\n"
        f"_rows = resultsTable({options!r})\n"
        "print(json.dumps(None if _rows is None else [list(r) for r in _rows], default=str))\n"
        "del _rows\n"
    )
    if csv_path:
        code += f"exportResults({config.to_windows(csv_path)!r}, {options!r})\n"
    try:
        table = json.loads(sessions.run_checked(code, timeout=600).strip().splitlines()[-1])
    except BridgeError as exc:
        raise _tool_error(exc) from exc
    if not table:
        raise ToolError(f"no results for {options}; check labels, locations and cases with diana_load_results")
    header, rows = table[0], table[1:]
    shown = rows[:max(1, int(max_rows))]
    if digits and int(digits) > 0:
        shown = [[_significant(v, int(digits)) for v in row] for row in shown]
    return {"columns": header, "rows_total": len(rows), "rows": shown, "csv": csv_path}


def _significant(value: Any, digits: int) -> Any:
    return float(f"{value:.{digits}g}") if isinstance(value, float) else value


@mcp.tool(annotations=READ_ONLY)
def diana_read_out(path: str, tail_lines: int = 20) -> dict[str, Any]:
    """Summarize a DIANA solver log (.out): analysis commands, load steps with convergence, errors, warnings and
    whether the run terminated normally. Works on finished and on still-running jobs.

    Args:
        path: absolute path of the .out file.
        tail_lines: number of last lines to include (0 for none).
    """
    out = Path(path)
    if not out.is_file():
        raise ToolError(f"file not found: {path}")
    parsed = parse_out(out)
    info = parsed.summary()
    if tail_lines:
        info["tail"] = tail(out, tail_lines)
    if parsed.errors or parsed.stopped or parsed.aborted or info.get("not_converged_steps"):
        texts = [f"{m.severity} {m.code} {m.text}" for m in parsed.errors[:10]] + [str(info.get("end"))]
        if info.get("not_converged_steps"):
            texts.append("NO CONVERGENCE")
        related = _related(texts, f"read_out:{out.name}")
        if related:
            info["related_experience"] = related
    return info


@mcp.tool()
def diana_experience_mine_logs(folders: list[str], dry_run: bool = False) -> dict[str, Any]:
    """Learn from old solver logs: scan every .out file under these folders and draft one experience entry per kind
    of DIANA message (error code, or message text without a code), with counts, example files and DIANA's own
    explanation and advice. The drafts are 未验证 (source 历史日志): when the same message comes up in real work,
    propose the entry to the user for approval together with the new evidence. Running it again only refreshes the
    statistics of existing entries. All drafts of one run form a single git commit.

    Args:
        folders: absolute paths of folders (searched recursively) or single .out files. A folder may also be
            ssh://user@host/absolute/path: those logs are listed and read over SSH, parsed from a temporary file
            that is deleted at once, and recorded under their remote path — nothing is stored on this machine.
        dry_run: only report what would be created.
    """
    paths = mining.find_out_files(folders)
    if not paths:
        raise ToolError(f"no .out files under {folders}")
    kinds, stats = mining.scan(paths)
    try:
        version = config.default_install().version
    except FileNotFoundError:
        version = None
    report = mining.seed(experiences, kinds, dry_run=dry_run, diana_version=version,
                         message=f"knowledge: 从 {stats['files']} 个历史日志生成草稿")
    return {"files": stats["files"], "skipped": stats["skipped"], "endings": stats["endings"],
            "message_kinds": len(kinds), **report}


def _ranges(numbers: list[int]) -> str:
    numbers = sorted(set(numbers))
    parts, start = [], None
    for i, n in enumerate(numbers):
        if start is None:
            start = n
        if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
            parts.append(str(start) if start == n else f"{start}-{n}")
            start = None
    return ", ".join(parts)


@mcp.tool(annotations=READ_ONLY)
def diana_read_tb(
    path: str,
    result: str | None = None,
    step: int | None = None,
    load_case: int | None = None,
    mode: int | None = None,
    node_ids: list[int] | None = None,
    element_ids: list[int] | None = None,
    ids: dict[str, list[int]] | None = None,
    max_rows: int = 30,
    csv_path: str | None = None,
) -> dict[str, Any]:
    """Read a DIANA tabulated results file (.tb), streaming so multi-GB files work.

    Without `result`: an index of the file (result types with their steps / load cases / modes, columns and row
    counts). With `result`, e.g. "DISPLA", "REACTI", "STRESS TOTAL CAUCHY", "CRKWDT" or "VONMIS": the matching rows,
    optionally for one step, load case or mode and for given node / element numbers. Leaving `step` out gives the
    history over all steps, e.g. a load-displacement curve of one node. Results are written for non-converged steps
    too, so check diana_read_out / diana_job_status before trusting the last steps. Only the first max_rows rows are
    returned; csv_path writes all of them.

    Args:
        path: absolute path of the .tb file.
        result: part of the result name to select.
        step: load step number (nonlinear and transient analyses).
        load_case: load case number (linear analyses).
        mode: mode number (eigenvalue analyses).
        node_ids: keep rows of these node numbers (Nodnr column).
        element_ids: keep rows of these element numbers (Elmnr column).
        ids: filters on any id column, e.g. {"Reinnr": [3]} or {"Intpt": [1, 2]}.
        max_rows: rows to return inline.
        csv_path: absolute path of a CSV file to write every matching row to.

    Layout notes: LAYOUT COMBIN tables have result "Combination" and blank cells where an item does not apply;
    extreme-value selections (SELECT LOADS/STEPS ... MIN|MAX) add a "<column> case" field with the load case of
    each value and list the overall extremes in the block's "extremes"; COOR adds X0/Y0(/Z0) columns and AXES the
    direction cosines of principal values.
    """
    tb = Path(path)
    try:
        blocks = scan_tb(tb)
    except TbError as exc:
        raise ToolError(str(exc)) from exc
    if not result:
        groups: dict[tuple, dict[str, Any]] = {}
        for block in blocks:
            key = (block.result, block.operation, block.location, tuple(block.columns))
            group = groups.setdefault(key, {
                "result": block.result, "operation": block.operation, "location": block.location,
                "columns": block.columns, "blocks": 0, "rows": 0, "steps": [], "load_cases": [], "modes": [],
            })
            group["blocks"] += 1
            group["rows"] += block.rows
            for field, value in (("steps", block.step), ("load_cases", block.load_case), ("modes", block.mode)):
                if value is not None:
                    group[field].append(value)
        results = []
        for group in groups.values():
            for field in ("steps", "load_cases", "modes"):
                group[field] = _ranges(group[field])
            results.append({k: v for k, v in group.items() if v not in (None, "")})
        return {
            "file": path,
            "blocks": len(blocks),
            "analysis_types": sorted({b.analysis_type for b in blocks if b.analysis_type}),
            "skipped_lines": sum(b.skipped for b in blocks),
            "results": results,
        }
    selected = select_blocks(blocks, result=result, step=step, load_case=load_case, mode=mode)
    if not selected:
        raise ToolError(
            f"no block matches result={result!r}, step={step}, load_case={load_case}, mode={mode}; "
            "call without result for the index"
        )
    rows = read_tb_rows(tb, selected, node_ids=node_ids, element_ids=element_ids, ids=ids)
    shown: list[dict] = []
    total = 0
    if csv_path:
        everything = list(rows)
        write_csv(Path(csv_path), everything)
        total, shown = len(everything), everything[:max_rows]
    else:
        for row in rows:
            total += 1
            if len(shown) < max_rows:
                shown.append(row)
    trimmed = [{k: v for k, v in row.items() if v is not None} for row in shown]
    return {"file": path, "matched_blocks": len(selected), "rows_total": total, "rows": trimmed, "csv": csv_path}


def _reference() -> ApiReference:
    session = sessions.current
    if session is None:
        live = [s for s in sessions.discover() if s.responsive]
        session = live[0] if live else None
    if session is not None and session.info.get("diana_version"):
        version = session.info["diana_version"]

        def generate(path: Path) -> None:
            sessions.current = session
            sessions.run_checked(dump_code(path), timeout=300)
    else:
        install = config.default_install()
        version = install.version

        def generate(path: Path) -> None:
            generate_with_batch_dianaie(install, path)

    return api_store.get(version, generate)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def diana_api_search(query: str, limit: int = 15) -> str:
    """Search the DianaIE script commands of the installed DIANA version by name and description.

    The first call builds the reference from DIANA itself (a few seconds with a live session,
    about half a minute otherwise).

    Args:
        query: words or a partial command name, e.g. "mesh size", "addMaterial", "reinforcement bar".
        limit: maximum number of matches.
    """
    try:
        reference = _reference()
    except (BridgeError, FileNotFoundError, RuntimeError) as exc:
        raise _tool_error(exc) from exc
    matches = reference.search(query, limit=max(1, min(limit, 100)))
    if not matches:
        return f"No DIANA script command matches {query!r}."
    lines = [f"{reference.diana_version} script commands matching {query!r}:"]
    lines += [f"- {entry.one_line()}" for entry in matches]
    lines.append("Use diana_api_doc(name) for the full documentation of a command.")
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def diana_api_doc(name: str) -> str:
    """Full documentation (usage, parameters, return value) of one DianaIE script command or constant.

    Args:
        name: exact command name, e.g. "createSheet"; close matches are suggested otherwise.
    """
    try:
        reference = _reference()
    except (BridgeError, FileNotFoundError, RuntimeError) as exc:
        raise _tool_error(exc) from exc
    entry = reference.get(name)
    if entry is not None:
        return f"{entry.name} ({entry.kind})\n\n{entry.doc or '(no documentation)'}"
    if name in reference.constants:
        return f"{name} = {reference.constants[name]!r} (constant)"
    suggestions = reference.suggest(name)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else " Try diana_api_search."
    raise ToolError(f"No DIANA script command named {name!r}.{hint}")


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def diana_manual_search(query: str, limit: int = 8) -> str:
    """Search the DIANA user's manual of the installed version (read from its local help file).

    Covers theory, modeling, the element and material libraries, all analysis procedures and result items, the
    batch syntax of .dat/.dcf files (Appendices E-I), script commands (Appendix B), running batch jobs (Appendix C),
    user-supplied subroutines, and the verification manual with complete .dcf examples. Use it for solver command
    syntax, material model parameters, element types and output options; use diana_api_search for DianaIE
    script-command signatures. The first call builds a local index (a few seconds).

    Args:
        query: keywords, e.g. "LINPAG tabular layout", "total strain crack Hordijk", "EIGEN modes output".
        limit: number of pages to return.
    """
    try:
        manual = manuals.get(config.default_install())
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc
    hits = manual.search(query, limit=max(1, min(limit, 30)))
    if not hits:
        return f"No manual page matches {query!r}."
    lines = [f"DIANA {manual.version} manual pages for {query!r}:"]
    for hit in hits:
        where = f" — {hit.section}" if hit.section else ""
        lines.append(f"- {hit.page} | {hit.title}{where}\n    …{hit.snippet}…")
    lines.append("Read a page with diana_manual_read(page, find=...).")
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
def diana_manual_read(page: str, find: str | None = None, offset: int = 0, max_chars: int = 6000) -> dict[str, Any]:
    """Read a page of the DIANA user's manual as text, e.g. from diana_manual_search results.

    Args:
        page: page name such as "Theory/Theoryse738.html" (also accepts "Theoryse738").
        find: start reading shortly before the first occurrence of this text, e.g. "LINPAG" or "I.1.1.2".
        offset: character offset to start from; use next_offset of a previous call to continue.
        max_chars: characters to return.
    """
    try:
        manual = manuals.get(config.default_install())
        result = manual.read(page, find=find, offset=offset, max_chars=min(max_chars, 20000))
    except KeyError as exc:
        raise ToolError(f"no manual page {page!r}; use diana_manual_search to find page names") from exc
    except (LookupError, FileNotFoundError) as exc:
        raise ToolError(str(exc)) from exc
    try:
        linked = [e for e in experiences.entries() if e.status != "obsolete"
                  and any(manual.resolve(ref) == result["page"] for ref in e.manual)]
    except Exception:
        linked = []
    if linked:
        result["experience_notes"] = [{"id": e.id, "title": e.title, "status": STATUSES[e.status]} for e in linked]
        result["experience_note"] = "经验手册中引用了本页的条目：实践中的补充、限制或反例，使用前请一并阅读。"
    return result


def _knowledge() -> KnowledgeBase:
    try:
        install = config.default_install()
    except FileNotFoundError:
        install = None
    glossary = None
    if install is not None:
        try:
            glossary = glossaries.get(install)
        except (FileNotFoundError, GlossaryError, OSError, ValueError):
            glossary = None
    return KnowledgeBase(Context(install, experiences, manuals, glossary, api_store, community=community))


KnowledgeSource = Literal["experience", "community", "manual", "example", "keyword", "api"]


@mcp.tool(annotations=READ_ONLY)
def diana_knowledge_search(
    query: str,
    sources: list[KnowledgeSource] | None = None,
    limit: int = 10,
    include_obsolete: bool = False,
) -> dict[str, Any]:
    """Search everything known about DIANA at once: the experience handbook (lessons from real modeling, 已验证 =
    approved by the user, 未验证 = draft), the user's manual, the shipped tutorials and verification examples, the
    data-model keywords (SCHEMA) and the script commands.

    Chinese questions work: DIANA terms in them are translated with DianaIE's official GUI glossary and the English
    sources are searched with the translation (shown as searched_in_english). Results are merged by rank, experiences
    first. Read more with diana_experience_list(id=...), diana_manual_read(page=...), diana_keywords(keyword) or
    diana_api_doc(name). When a result solved the problem, call diana_knowledge_feedback with the search_id.

    Args:
        query: the question or keywords, Chinese or English, e.g. "受压断裂能怎么取值", "LB40/0053", "LINPAG".
        sources: restrict to some of experience, community, manual, example, keyword, api.
        limit: number of results.
        include_obsolete: also return experiences marked obsolete.
    """
    return _with_tips(_knowledge().search(query, sources=sources, limit=max(1, min(int(limit), 40)),
                                          include_obsolete=include_obsolete))


@mcp.tool(annotations=READ_ONLY)
def diana_community_status() -> dict[str, Any]:
    """Show whether the optional public community knowledge cache is synchronized, its exact Git commit and trust
    levels, plus low-risk local entries that could be prepared for review. This performs no network access."""
    result = community.status()
    eligible = contributions.eligible()
    result["local_candidates"] = len(eligible)
    result["low_risk_candidates"] = [item for item in eligible if not item["obvious_sensitive_matches"]]
    result["upload_policy"] = "人工确认后才上传"
    return result


@mcp.tool(annotations=ToolAnnotations(idempotent_hint=True, open_world_hint=True))
def diana_community_sync() -> dict[str, Any]:
    """Explicitly download/update the optional public community experience repository.

    Community content is read only as Markdown data; no contributed Python or other executable file is run. There
    is no hidden startup synchronization, so this tool is the network-consent boundary.
    """
    try:
        report = community.sync()
        report["next"] = "社区经验已接入 diana_knowledge_search；来源、commit、状态和可信度会随结果显示。"
        return report
    except CommunityError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(open_world_hint=False))
def diana_community_prepare(entry_ids: list[str] | None = None, recommended_only: bool = True) -> dict[str, Any]:
    """Create local-only redacted copies of private experience entries for human review; never upload them.

    Args:
        entry_ids: exact private experience ids. Omit to select entries without history-log provenance, scripts or
            obvious sensitive-pattern matches.
        recommended_only: when entry_ids is omitted, exclude entries with obvious sensitive-pattern matches.
    """
    try:
        selected = entry_ids
        if selected is None:
            eligible = contributions.eligible()
            selected = [item["id"] for item in eligible
                        if not recommended_only or not item["obvious_sensitive_matches"]]
        report = contributions.prepare(selected)
        report["next"] = (f"调用 diana_community_preview(batch_id={report['batch_id']!r}) 逐条审阅；"
                          "不要仅凭自动扫描结果批准公开。")
        return report
    except (CommunityError, ExperienceError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=READ_ONLY)
def diana_community_preview(batch_id: str) -> dict[str, Any]:
    """Read every redacted candidate and its risk report before deciding whether it may be public."""
    try:
        return contributions.preview(batch_id, include_text=True)
    except CommunityError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=True))
def diana_community_contribute(batch_id: str, confirmation: str) -> dict[str, Any]:
    """Upload a reviewed redaction batch to a public contribution branch.

    This is a public external write. It runs only when confirmation is exactly ``UPLOAD <batch_id>`` from the user
    after reviewing diana_community_preview. Uploaded entries remain low-trust candidates; repository automation
    validates them and the returned compare URL is used to open a pull request.
    """
    try:
        return contributions.contribute(batch_id, confirmation)
    except CommunityError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def diana_knowledge_feedback(
    search_id: str, helpful: list[str], note: str | None = None, unhelpful: list[str] | None = None
) -> dict[str, Any]:
    """Record which results of a diana_knowledge_search, or which recommended experiences, actually helped (an
    empty list means none did) and which were misleading.

    Helpful entries rank a little higher from then on and misleading ones lower; the most helpful verified entries
    become resident tips shown at the start of each session. The log is also the evaluation set for improving
    retrieval.

    Args:
        search_id: the search_id returned by diana_knowledge_search, or a short label such as "job:<id>" for
            experiences that a tool recommended by itself.
        helpful: refs of the helpful results as listed there, e.g. ["attach-snaps-to-existing-vertex",
            "Theory/Theoryse738.html"].
        note: what the right answer turned out to be, if no result had it.
        unhelpful: refs that were shown but wrong or misleading for this problem.
    """
    return _knowledge().feedback(search_id, helpful, note, unhelpful)


@mcp.tool(annotations=READ_ONLY)
def diana_glossary(term: str, limit: int = 12) -> dict[str, Any]:
    """Look up DIANA terminology in English and Chinese, from DianaIE's own GUI translation (~15,000 labels,
    options and messages): the Chinese label the user sees for an English term, or the English term (for the manual
    and the keywords) behind a Chinese label, e.g. "基于总应变开裂模型" or "Compressive fracture energy".

    Args:
        term: an English or Chinese term.
        limit: number of pairs.
    """
    try:
        glossary = glossaries.get(config.default_install())
    except (FileNotFoundError, GlossaryError, OSError, ValueError) as exc:
        raise ToolError(f"glossary unavailable: {exc}") from exc
    pairs = glossary.lookup(term, limit=max(1, min(int(limit), 50)))
    english, terms = glossary.translate(term)
    out: dict[str, Any] = {"term": term, "pairs": pairs}
    if terms and english != term:
        out["terms_found"] = [{"中文": zh, "English": ens} for zh, ens in terms]
    return out


ExperienceType = Literal["error-fix", "modeling-pitfall", "api-usage", "convergence", "parameter",
                         "result-interpretation", "workflow"]


def _commit_note(commit: dict[str, Any]) -> str:
    return f"已提交 git {commit['commit']}" if commit.get("committed") else f"未提交 git：{commit.get('reason')}"


@mcp.tool()
def diana_experience_add(
    title: str,
    type: ExperienceType,
    symptom: str,
    cause: str = "",
    fix: str = "",
    verification: str = "",
    notes: str = "",
    triggers: list[str] | None = None,
    tags: list[str] | None = None,
    manual: list[str] | None = None,
    evidence: list[str] | None = None,
    related: list[str] | None = None,
    diana_version: str | None = None,
    source: Literal["ai", "user"] = "ai",
    id: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Record a lesson in the experience handbook as a draft (未验证); only the user can approve it later.

    Write it in the user's language, concretely: what was observed (error code, message, wrong result), why, what
    fixed it and how that was confirmed. Refused when a similar entry exists (similar title or shared trigger):
    update that one instead, or pass force=True if it really is a different lesson.

    Args:
        title: one-sentence summary.
        type: error-fix, modeling-pitfall, api-usage, convergence, parameter, result-interpretation or workflow.
        symptom: what was observed (becomes the 现象 section).
        cause: why it happens (原因).
        fix: what solved it (解决).
        verification: how the fix was confirmed (验证).
        notes: anything else (备注).
        triggers: error codes, message fragments or command names that should bring this entry up, e.g.
            ["/DIANA/NL/IS41/0001", "Too small step"]; prefix "re:" for a regular expression.
        tags: e.g. ["非线性", "界面单元"].
        manual: related manual pages, e.g. ["Theory/Theoryse738.html"].
        evidence: job ids, files, test names, dates that back it up.
        related: ids of related entries.
        diana_version: DIANA version it applies to (default: the installed one).
        source: "ai" when you drafted it, "user" when the user dictated it.
        id: file name (lowercase letters, digits, hyphens), default derived from the title.
        force: create it even though similar entries exist.
    """
    if not force:
        duplicates = experiences.likely_duplicates(title, triggers or [])
        if duplicates:
            return {"created": False, "reason": "已有相似经验：更新它，或确认是不同的经验后传 force=true",
                    "similar": [e.summary() for e in duplicates]}
    if diana_version is None:
        try:
            diana_version = config.default_install().version
        except FileNotFoundError:
            diana_version = None
    try:
        entry, commit = experiences.add(
            title=title, kind=type, symptom=symptom, cause=cause, fix=fix, verification=verification, notes=notes,
            triggers=triggers or [], tags=tags or [], manual=manual or [], evidence=evidence or [],
            related=related or [], diana_version=diana_version, source=source, entry_id=id)
    except ExperienceError as exc:
        raise ToolError(str(exc)) from exc
    return {"created": True, "id": entry.id, "file": str(entry.path), "status": STATUSES[entry.status],
            "git": _commit_note(commit)}


@mcp.tool()
def diana_experience_update(
    id: str,
    title: str | None = None,
    type: ExperienceType | None = None,
    symptom: str | None = None,
    cause: str | None = None,
    fix: str | None = None,
    verification: str | None = None,
    notes: str | None = None,
    triggers: list[str] | None = None,
    tags: list[str] | None = None,
    manual: list[str] | None = None,
    related: list[str] | None = None,
    add_evidence: list[str] | None = None,
    diana_version: str | None = None,
    status: Literal["verified", "disputed", "obsolete", "draft"] | None = None,
    approval: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Edit an experience entry, add evidence, or change its status. Every change is committed to git.

    status="verified" is the user's decision: set it only after the user explicitly approved this entry in the
    conversation, and pass their words verbatim as approval. "disputed" (evidence against it), "obsolete" (no longer
    applies, e.g. after a DIANA upgrade) and "draft" (back to unverified) need a reason. Entries are never deleted.

    Args:
        id: the entry id.
        title: new title.
        type: new type.
        symptom: replaces the 现象 section.
        cause: replaces the 原因 section.
        fix: replaces the 解决 section.
        verification: replaces the 验证 section.
        notes: replaces the 备注 section.
        triggers: replaces the triggers.
        tags: replaces the tags.
        manual: replaces the manual references.
        related: replaces the related ids.
        add_evidence: evidence to append, e.g. "2026-10-02 作业 ab12cd 改用弧长法后收敛".
        diana_version: DIANA version it applies to.
        status: verified, disputed, obsolete or draft.
        approval: the user's approval in their own words (required for verified).
        reason: why the status changes (required for disputed, obsolete, draft).
    """
    sections = {k: v for k, v in (("现象", symptom), ("原因", cause), ("解决", fix), ("验证", verification),
                                  ("备注", notes)) if v is not None}
    commits = []
    try:
        if sections or any(v is not None for v in (title, type, triggers, tags, manual, related, diana_version)) \
                or add_evidence:
            entry, commit = experiences.update(
                id, title=title, kind=type, sections=sections or None, triggers=triggers, tags=tags, manual=manual,
                related=related, add_evidence=add_evidence or [], diana_version=diana_version)
            commits.append(_commit_note(commit))
        if status == "verified":
            if not approval:
                raise ToolError("status verified needs approval: the user's own words approving this entry")
            entry, commit = experiences.approve(id, approval)
            commits.append(_commit_note(commit))
        elif status is not None:
            entry, commit = experiences.set_status(id, status, reason or "")
            commits.append(_commit_note(commit))
        if not commits:
            raise ToolError("nothing to change")
    except KeyError as exc:
        raise ToolError(f"no experience entry {id!r}; see diana_experience_list") from exc
    except ExperienceError as exc:
        raise ToolError(str(exc)) from exc
    return entry.summary() | {"git": commits}


@mcp.tool(annotations=READ_ONLY)
def diana_experience_list(
    id: str | None = None,
    status: Literal["verified", "draft", "disputed", "obsolete"] | None = None,
    type: ExperienceType | None = None,
    tag: str | None = None,
    query: str | None = None,
    limit: int = 30,
    details: bool = False,
) -> dict[str, Any]:
    """Browse the experience handbook, or read one entry in full (with id).

    Without id: entries (newest first; id, title, status, type) filtered by status (draft = the review queue of 未验证
    entries), type, tag or a text query, plus counts per status and any files that do not parse or are not committed
    yet.

    Args:
        id: read this entry in full.
        status: verified, draft, disputed or obsolete.
        type: entry type.
        tag: only entries with this tag.
        query: only entries whose title, triggers, tags or text contain these words.
        limit: number of entries.
        details: also list tags, triggers, source, date and usage of each entry.
    """
    stats = usage.experience_usage()
    if id:
        try:
            view = experiences.get(id).view()
        except KeyError as exc:
            raise ToolError(f"no experience entry {id!r}") from exc
        except ExperienceError as exc:
            raise ToolError(str(exc)) from exc
        if id in stats and stats[id].view():
            view["usage"] = stats[id].view()
        return view
    entries, problems = experiences.load()
    counts = {STATUSES[s]: sum(1 for e in entries if e.status == s) for s in STATUSES}
    selected = [e for e in entries if (status is None or e.status == status) and (type is None or e.type == type)
                and (tag is None or tag in e.tags)]
    if query:
        bm25, by_id = experiences.index(selected)
        selected = [by_id[i] for i, _ in bm25.search(query, len(selected))]
    out: dict[str, Any] = {
        "folder": str(experiences.root),
        "counts": counts,
        "matches": len(selected),
        "entries": [_listed(e, stats, details) for e in selected[: max(1, min(int(limit), 500))]],
    }
    if problems:
        out["invalid_files"] = problems
    pending = experiences.uncommitted()
    if pending:
        out["uncommitted"] = pending
    return out


def _listed(entry: Any, stats: dict[str, Any], details: bool) -> dict[str, Any]:
    if not details:
        return {"id": entry.id, "title": entry.title, "status": STATUSES[entry.status], "type": TYPES[entry.type]}
    use = stats[entry.id].view() if entry.id in stats else None
    return entry.summary() | ({"usage": use} if use else {})


@mcp.tool()
def diana_experience_verify(ids: list[str] | None = None, timeout_seconds: float = 900) -> dict[str, Any]:
    """Re-run the verification scripts of experience entries on the installed DIANA (after an upgrade, or to
    confirm a draft before proposing it for approval).

    Entries can carry a "## 验证脚本" section with a ```python block: it runs inside a short-lived batch DianaIE
    (never the user's live session), with WORKDIR set to an empty temporary folder in which it creates its own
    project, and passes when it finishes without an exception (use assert). Results are stored in each entry's
    checks field; a verified entry that fails is marked disputed. One git commit per run. Needs a DIANA license
    seat for about 15 s plus the scripts' run time.

    Args:
        ids: entries to check; default every entry with a script.
        timeout_seconds: limit for the whole batch run.
    """
    from .verify import verify

    try:
        return verify(experiences, config.default_install(), ids, timeout=max(60.0, float(timeout_seconds)))
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def diana_experience_export(include_drafts: bool = True, folder: str | None = None) -> dict[str, Any]:
    """Export the experience handbook as one readable document, 《DIANA 经验手册》: a Markdown file and a
    self-contained HTML page (opens in any browser, prints to PDF), grouped by type, with status, triggers,
    verification results, usage and an index of error codes. Default folder: knowledge/export (not in git).

    Args:
        include_drafts: also include unverified and disputed entries (clearly marked); False = verified only.
        folder: absolute path of another output folder.
    """
    from .handbook import export

    statuses = ("verified", "draft", "disputed") if include_drafts else ("verified",)
    return export(experiences, usage.experience_usage(), Path(folder) if folder else None, statuses)


@mcp.tool(annotations=READ_ONLY)
def diana_experience_review(limit: int = 15) -> dict[str, Any]:
    """Periodic review of the experience handbook: what to bring to the user.

    Returns drafts worth proposing for approval (reported helpful, or recommended for a solver problem that was
    solved afterwards), AI drafts still missing their cause, groups of similar entries that could be merged or
    generalized into one rule, entries whose automatic verification failed or ran on another DIANA version, and
    entries repeatedly reported as unhelpful. Changes nothing: propose each step to the user, then apply it with
    diana_experience_update (merging = combine into one entry, mark the others obsolete with "合并到 <id>").

    Args:
        limit: items per list.
    """
    from . import review as review_module

    solved: dict[str, list[str]] = {}
    for job in _jobs().list(limit=500):
        if job.resolves:
            try:
                failed = jobs.get(job.resolves)
            except JobError:
                continue
            for entry_id in failed.recommended:
                solved.setdefault(entry_id, []).append(failed.id)
    try:
        version = config.default_install().version
    except FileNotFoundError:
        version = None
    report = review_module.review(experiences, usage.experience_usage(), solved, version, max(1, int(limit)))
    report["next"] = ("逐条向用户说明并请其决定：批准草稿要引用用户原话；合并相似条目先起草合并后的内容请用户确认；"
                      "缺原因的草稿可以先结合手册补充（仍是草稿）；验证失败或被多次标为无用的，建议标为存疑。")
    return report


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
def diana_stop(save: bool, close_diana: bool | None = None) -> dict[str, Any]:
    """End the live session.

    Args:
        save: save the open project first. Otherwise unsaved changes are lost if DianaIE closes.
        close_diana: close the DianaIE window. Defaults to True for a DianaIE launched by this server and
            False for one the user opened (then only the bridge stops and DianaIE stays open).
    """
    try:
        session = sessions.require()
        close = session.launched_here if close_diana is None else close_diana
        if close:
            sessions.close_diana(save)
        else:
            if save:
                sessions.run_checked("print(saveProject() if projectName() else 'no project')", timeout=300)
            sessions.detach()
    except BridgeError as exc:
        raise _tool_error(exc) from exc
    return {"pid": session.pid, "saved": save, "diana_closed": close}


def _apply_profile() -> None:
    """Drop the tools the other half of a split deployment serves (see KNOWLEDGE_TOOLS)."""
    if PROFILE == "full":
        return
    for tool in list(mcp._tool_manager.list_tools()):
        if (tool.name in KNOWLEDGE_TOOLS) != (PROFILE == "knowledge"):
            mcp.remove_tool(tool.name)


_apply_profile()


def main() -> None:
    mcp.run()
