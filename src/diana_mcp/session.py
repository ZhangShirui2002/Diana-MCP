"""Find, launch and talk to live DianaIE sessions that run the bridge."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config
from .bridge_client import BridgeEndpoint, BridgeError, BridgeUnavailable, request

VIEWPOINTS = {
    "top": "Top",
    "bottom": "Bottom",
    "front": "Front",
    "back": "Back",
    "left": "Left",
    "right": "Right",
    "iso1": "Iso1",
    "iso2": "Iso2",
}

# Runs inside DianaIE. Saves if asked, discards what is left so no "save changes?" dialog can block,
# then posts WM_CLOSE to DianaIE's own top-level windows.
_CLOSE_CODE = r"""
def _diana_mcp_close(save):
    import ctypes, os
    from ctypes import wintypes
    if save and projectName() and not saveProject():
        raise RuntimeError("saveProject() failed; DianaIE left open")
    if projectName():
        closeProject()
    user32 = ctypes.WinDLL("user32")
    pid = os.getpid()
    windows = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
            windows.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(collect), 0)
    for hwnd in windows:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)
    return len(windows)
try:
    print(_diana_mcp_close(%(save)r))
finally:
    del _diana_mcp_close
"""

_SUMMARY_CODE = r"""
def _diana_mcp_summary():
    import json
    ns = globals()
    out = {}
    # resultCase() is deliberately not asked for: with no results loaded it parses an empty
    # "analysis/block/case" string and DIANA complains in its message window, which would make the user think
    # something went wrong with their model. diana_load_results lists the result cases when they exist.
    for name in ("diaVersion", "projectName", "modelDimensions", "modelAnalysisAspects", "shapes",
                 "materials", "geometries", "elementGroups", "nodeGroups", "reinforcementShapes",
                 "loadCases", "functions", "selectedShapes"):
        fn = ns.get(name)
        if callable(fn):
            try:
                value = fn()
                out[name] = list(value) if isinstance(value, (list, tuple)) else value
            except Exception as exc:
                out[name] = "<error: %s>" % exc
    if callable(ns.get("analyses")):
        details = {}
        for analysis in ns["analyses"]():
            try:
                details[analysis] = list(ns["analysisCommands"](analysis))
            except Exception as exc:
                details[analysis] = "<error: %s>" % exc
        out["analyses"] = details
    print(json.dumps(out, default=str))
try:
    _diana_mcp_summary()
finally:
    del _diana_mcp_summary
"""


def _kernel32():
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return kernel32


_PARAMETERS_CODE = r"""
def _diana_mcp_parameters(kind, name):
    import json
    rows = []
    for path in availableParameters(kind, name):
        try:
            value = parameter(kind, name, path)
        except Exception as exc:
            value = "<error: " + str(exc) + ">"
        rows.append([path, value])
    print(json.dumps(rows, default=str))
try:
    _diana_mcp_parameters(%(kind)r, %(name)r)
finally:
    del _diana_mcp_parameters
"""

_UNITS_CODE = r"""
def _diana_mcp_units():
    import json
    out = {}
    for q in quantities():
        try:
            out[q] = unit(q)
        except Exception:
            pass
    print(json.dumps(out))
try:
    _diana_mcp_units()
finally:
    del _diana_mcp_units
"""


def settable_path(path: str) -> str | None:
    """The form of an availableParameters path that setParameter accepts (verified on DIANA 10.5):
    below the model directory for materials and geometries ('/MATERI(1)/MATCLS/CONCR/LEI/LINEAR/ELASTI/YOUNG' ->
    'LINEAR/ELASTI/YOUNG'), below the item root otherwise ('/POINT(1)/FORCE/VALUE' -> 'FORCE/VALUE'). Longer or
    shorter forms make setParameter return False without changing anything."""
    import re

    model = re.match(r"^/(?:MATERI|GEOMET)\(\d+\)/(?:MATCLS|GEOCLS)/[^/]+/[^/]+/(.+)$", path)
    if model:
        return model.group(1)
    if re.match(r"^/(?:MATERI|GEOMET)\(\d+\)/", path):
        return None  # class, model and assignment entries are set with addMaterial / addGeometry / assign*
    rooted = re.match(r"^/[A-Z]+\(\d+\)/(.+)$", path)
    return rooted.group(1) if rooted else None


def pid_alive(pid: int) -> bool:
    """True if a host process with this id is running. Never use os.kill here: on Windows it terminates.

    DianaIE's own pid is not a host pid under Wine — use ``session_alive`` for a session.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def process_image(pid: int) -> str | None:
    """Executable path of a running process, used to detect pid reuse. Windows only."""
    if os.name != "nt" or pid <= 0:
        return None
    import ctypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        size = ctypes.c_ulong(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        return buf.value if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else None
    finally:
        kernel32.CloseHandle(handle)


def kill_tree(pid: int) -> None:
    """Force-kill a *host* process with its children (communicator.exe outlives a plain kill of DianaIE).

    Only for pids of processes this server started itself (a solver run, a DianaIE launcher). A session's pid is
    a Wine pid off Windows — use :func:`kill_dianaie` for that.
    """
    if config.IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except OSError:
        pass


def kill_processes_in(directory: Path) -> int:
    """Kill the host processes working in this folder; returns how many.

    A DIANA run is diana.exe plus the segment executable it starts (seg_apnl41.exe and friends). Under Wine those
    are separate host processes owned by wineserver, so killing the launcher leaves the one doing the computing
    behind, still burning CPU and writing to the job's files. They all inherit the job folder as their working
    directory, which is what identifies them here.
    """
    if config.IS_WINDOWS:  # taskkill /T already covers the tree
        return 0
    target = directory.resolve()
    killed = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if Path(os.readlink(str(entry / "cwd"))) == target:
                os.kill(int(entry.name), getattr(signal, "SIGKILL", signal.SIGTERM))
                killed += 1
        except OSError:
            continue
    return killed


def kill_dianaie(session: "Session") -> None:
    """Stop this DianaIE. Off Windows its pid belongs to Wine, so kill the host process running DianaIE.exe —
    never `wineserver -k`, which would also abort solver runs sharing the prefix.
    """
    if config.IS_WINDOWS:
        kill_tree(session.pid)
        return
    if session.process is not None and session.process.poll() is None:
        kill_tree(session.process.pid)
    subprocess.run(["pkill", "-f", r"DianaIE\.exe"], capture_output=True, check=False)


def write_loader(path: Path) -> Path:
    """Startup script for DianaIE: runs the bridge in its own module namespace (DianaIE has no __file__).

    Skips the start when the autostart hook already started a bridge: restarting it would drop the session the
    launcher is about to ping.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Generated by diana-mcp. Starts the MCP bridge inside this DianaIE session (unless autostart did).\n"
        "import builtins, runpy\n"
        "_diana_mcp_running = getattr(builtins, '_diana_mcp_bridge', None)\n"
        "if _diana_mcp_running is None or _diana_mcp_running.closed:\n"
        f"    runpy.run_path({config.to_windows(config.BRIDGE_SCRIPT)!r}, init_globals={{'DIANA_NAMESPACE': globals()}},"
        " run_name='diana_mcp_bridge')\n"
        "del builtins, runpy, _diana_mcp_running\n",
        encoding="utf-8",
    )
    return path


def manual_attach_command() -> str:
    return (
        f"import runpy; runpy.run_path({config.to_windows(config.BRIDGE_SCRIPT)!r}, "
        "init_globals={'DIANA_NAMESPACE': globals()}, run_name='diana_mcp_bridge')"
    )


def session_alive(session: "Session") -> bool:
    """Is this DianaIE still running? Its pid is a host pid only on Windows; under Wine it belongs to the
    Windows process namespace, so there the bridge socket is the only honest answer."""
    if config.IS_WINDOWS:
        return pid_alive(session.pid)
    try:
        request(session.endpoint, "ping", timeout=3)
    except BridgeUnavailable:
        return False
    except BridgeError:
        pass  # busy with a long operation or a modal dialog, but the process is there
    return True


@dataclass
class Session:
    pid: int
    endpoint: BridgeEndpoint
    session_file: Path
    info: dict[str, Any] = field(default_factory=dict)
    responsive: bool = True
    process: subprocess.Popen | None = None

    @property
    def launched_here(self) -> bool:
        return self.process is not None

    def describe(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "port": self.endpoint.port,
            "responsive": self.responsive,
            "launched_by_mcp": self.launched_here,
            **self.info,
        }


class SessionManager:
    def __init__(self, session_dirs: list[Path] | None = None):
        dirs = session_dirs or [config.sessions_dir()]
        self.session_dirs = list(dict.fromkeys(Path(d) for d in dirs))
        self.current: Session | None = None
        self._launched: dict[int, subprocess.Popen] = {}

    def _scan(self) -> list[tuple[dict[str, Any], Path]]:
        found = []
        for directory in self.session_dirs:
            for path in sorted(directory.glob("*.json")):
                try:
                    found.append((json.loads(path.read_text(encoding="utf-8")), path))
                except (OSError, ValueError):
                    continue
        return found

    def _session_from(self, data: dict[str, Any], path: Path) -> Session:
        endpoint = BridgeEndpoint(data.get("host", "127.0.0.1"), int(data["port"]), str(data["token"]))
        pid = int(data["pid"])
        return Session(pid, endpoint, path, process=self._launched.get(pid))

    def discover(self) -> list[Session]:
        """Live bridge sessions; stale session files (dead process, closed bridge) are removed."""
        sessions = []
        for data, path in self._scan():
            try:
                session = self._session_from(data, path)
            except (KeyError, ValueError):
                continue
            if config.IS_WINDOWS and not pid_alive(session.pid):  # off Windows the ping below decides
                path.unlink(missing_ok=True)
                continue
            try:
                session.info = request(session.endpoint, "ping", timeout=3).get("info", {})
            except BridgeUnavailable:
                path.unlink(missing_ok=True)
                continue
            except BridgeError:
                session.responsive = False  # alive but busy (long operation or modal dialog)
            sessions.append(session)
        return sessions

    def attach(self, pid: int | None = None) -> Session:
        sessions = [s for s in self.discover() if pid is None or s.pid == pid]
        if not sessions:
            raise BridgeError("no running DianaIE with the diana-mcp bridge was found" + (f" (pid {pid})" if pid else ""))
        candidates = [s for s in sessions if s.responsive] or sessions
        self.current = max(candidates, key=lambda s: s.session_file.stat().st_mtime)
        return self.current

    def launch(self, install: config.DianaInstall, workdir: Path, timeout: float = 180) -> Session:
        workdir.mkdir(parents=True, exist_ok=True)
        session_dir = self.session_dirs[0]
        session_dir.mkdir(parents=True, exist_ok=True)
        loader = write_loader(config.state_dir() / "start_bridge.py")
        launch_id = uuid.uuid4().hex
        # DianaIE reads these, so they are spelled as Windows paths; cwd below stays a host path.
        env = config.launch_env(
            DIANA_MCP_SESSION_DIR=config.to_windows(session_dir),
            DIANA_MCP_LAUNCH_ID=launch_id,
            DIANA_MCP_WORKDIR=config.to_windows(workdir),
        )
        command = config.launch_argv(
            install.dianaie, "--keepOpen", "--noSplash",
            "--workingDir", config.to_windows(workdir), config.to_windows(loader),
        )
        process = subprocess.Popen(
            command, cwd=workdir, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for data, path in self._scan():
                if data.get("launch_id") == launch_id:
                    self._launched[int(data["pid"])] = process
                    session = self._session_from(data, path)
                    session.info = request(session.endpoint, "ping", timeout=15).get("info", {})
                    self.current = session
                    return session
            if process.poll() is not None:
                raise BridgeError(
                    f"DianaIE exited with code {process.returncode} before the bridge started; "
                    "check that DianaIE starts normally and a license is available"
                )
            time.sleep(0.5)
        kill_tree(process.pid)
        raise BridgeError(f"the bridge did not start within {timeout:g} s; the DianaIE it launched was killed")

    def require(self) -> Session:
        """The current session, re-attaching automatically (e.g. to a bridge the user started by hand)."""
        current = self.current
        if current is not None and current.session_file.exists() and session_alive(current):
            return current
        self.current = None
        try:
            return self.attach()
        except BridgeError as exc:
            raise BridgeError(f"{exc}. Call diana_start first.") from exc

    def execute(self, code: str, timeout: float) -> dict[str, Any]:
        return request(self.require().endpoint, "exec", timeout=timeout, code=code)

    def run_checked(self, code: str, timeout: float) -> str:
        """Execute helper code and return its stdout, raising on DIANA-side errors."""
        response = self.execute(code, timeout)
        if not response.get("ok"):
            raise BridgeError(response.get("error") or "DianaIE reported an unknown error")
        return response.get("stdout") or ""

    def screenshot(self, path: Path, view: str, viewpoint: str, fit: bool, width: int, height: int) -> Path:
        lines = []
        if view != "current":
            lines.append(f"showView({view!r})")
        if viewpoint != "current":
            lines.append(f"setViewPoint{VIEWPOINTS[viewpoint]}()")
        if fit:
            lines.append("fitAll()")
        path.parent.mkdir(parents=True, exist_ok=True)
        lines.append(f"saveImage('PNG', {str(path)!r}, {int(width)}, {int(height)})")
        self.run_checked("\n".join(lines), timeout=120)
        if not path.is_file():
            raise BridgeError("saveImage did not write an image (is a model view open?)")
        return path

    def show_review(self, record_path: Path, *, has_mesh: bool = True) -> None:
        """Put the complete model in DianaIE's 3D view and open the native, modeless human-review panel."""
        lines = ["import runpy"]
        if has_mesh:
            lines += [
                "showView('MESH')",
                "showAllElements()",
                "showAllNodes()",
                "showAllReinforcements()",
            ]
        else:
            lines += ["hideView('MESH')", "showAll('SHAPE')"]
        lines += [
            "fitAll()",
            "runpy.run_path(%r, init_globals={'DIANA_REVIEW_RECORD': %r, 'DIANA_REVIEW_AUDIT': %r}, "
            "run_name='diana_mcp_review_panel')" % (
                config.to_windows(config.REVIEW_SCRIPT), config.to_windows(record_path),
                config.to_windows(record_path.parents[2] / "audit.jsonl")
            ),
        ]
        self.run_checked("\n".join(lines), timeout=120)

    def model_summary(self) -> dict[str, Any]:
        stdout = self.run_checked(_SUMMARY_CODE, timeout=120)
        summary = json.loads(stdout.strip().splitlines()[-1])
        summary["units"] = json.loads(self.run_checked(_UNITS_CODE, timeout=60).strip().splitlines()[-1])
        issues = self.run_checked("import json\nprint(json.dumps([str(i) for i in validateModel('ALL')]))", timeout=300)
        summary["validation_issues"] = json.loads(issues.strip().splitlines()[-1])
        return summary

    def parameters(self, kind: str, name: str) -> list[list[Any]]:
        stdout = self.run_checked(_PARAMETERS_CODE % {"kind": kind, "name": name}, timeout=120)
        return json.loads(stdout.strip().splitlines()[-1])

    def analysis_commands(self, analysis: str, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run_checked(f"saveAnalysisCommands({analysis!r}, {config.to_windows(path)!r}, 6)", timeout=120)
        return path.read_text(encoding="utf-8", errors="replace")

    def command_log_path(self) -> Path:
        """DianaIE's history file, as a host path (DianaIE reports it the way Windows sees it)."""
        return config.to_host(self.run_checked("print(logFileName())", timeout=30).strip().splitlines()[-1])

    def detach(self) -> None:
        """Stop the bridge but leave DianaIE running."""
        session = self.require()
        request(session.endpoint, "stop", timeout=15)
        self.current = None

    def close_diana(self, save: bool, timeout: float = 90) -> None:
        session = self.require()
        self.run_checked(_CLOSE_CODE % {"save": save}, timeout=300)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and session_alive(session):
            time.sleep(0.5)
        self.current = None
        if session_alive(session):
            if session.process is None:
                raise BridgeError("DianaIE did not close; close it manually")
            kill_dianaie(session)
