"""DIANA MCP bridge: runs INSIDE DianaIE (embedded Python 3.7) and executes code sent by the MCP server.

Why a Win32 timer instead of a background thread: DianaIE keeps the GIL while its Qt event loop is
idle, so background Python threads never get scheduled. A thread timer (SetTimer with a TIMERPROC)
is dispatched by DianaIE's own message loop, which gives the bridge short slices on the MAIN thread,
the only place where DIANA API calls are safe.

Protocol: one JSON object per line over TCP on 127.0.0.1, one request per connection.
  request:  {"token": str, "id": str, "op": "ping" | "exec" | "stop", "code": str,
             "deadline": epoch seconds after which the request is dropped unexecuted}
  response: {"id": str, "ok": bool, "stdout": str, "stderr": str, "value": str | None,
             "error": str | None, "seconds": float, ...}

Change journal: DianaIE appends every logged command to a history file (logFileName()), whether it came from the
GUI, the command console or this bridge. Only the bridge knows when it is running code, so it tags each new part of
that file: "user" when it appeared between bridge requests, "ai" when it appeared while serving one, together with
DianaIE's undo/redo counts at that moment. The MCP server reads that journal to tell the user's edits from its own.

The MCP server starts it through a generated loader (``DianaIE.exe --keepOpen start_bridge.py``) or the optional
autostart hook; inside a DianaIE you opened yourself, run this in the command console:
    import runpy; runpy.run_path(r"<path>/diana_bridge.py", init_globals={"DIANA_NAMESPACE": globals()}, run_name="diana_mcp_bridge")
Running through runpy keeps the bridge's own names out of DianaIE's scripting namespace.
Keep this file compatible with Python 3.7 (no walrus, no builtin generics, no str.removeprefix).
Avoid DIANA commands that DianaIE writes to its history (projectName, diaVersion, ...) in anything that runs
periodically: they would clutter the user's history and show up as new changes.
"""
import ast
import atexit
import builtins
import contextlib
import hmac
import io
import json
import os
import re
import select
import socket
import sys
import time
import traceback
import uuid

BRIDGE_VERSION = "0.9.0"
MAX_TEXT = 200000
SOLVER_ENTRYPOINTS = ("runSolver", "runStabor", "runPipe", "runQuake", "runRCBeam", "runSealec", "runSlab")
DEFAULT_TIMER_MS = 50
TITLE_PREFIX = "Diana Interactive Environment"


def default_session_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "diana-mcp", "sessions")


def _truncate(text):
    if text is None or len(text) <= MAX_TEXT:
        return text
    return text[:MAX_TEXT] + "\n... [truncated %d characters]" % (len(text) - MAX_TEXT)


def log_key(log_path):
    """Stable name of a DianaIE history file: IEhistory_<yymmdd>_<hhmmss>, kept when DianaIE renames it on exit."""
    name = os.path.basename(log_path)
    match = re.match(r"(IEhistory_\d{6}_\d{6})", name)
    return match.group(1) if match else os.path.splitext(name)[0]


def undo_counts():
    """DianaIE's (undo, redo) stack sizes, or (None, None) outside DianaIE. Not logged by DianaIE."""
    try:
        from diana.utils import undoredo
        stack = undoredo.GetGlobalTracker().undoStack()
        return int(stack.undoCount()), int(stack.redoCount())
    except Exception:
        return None, None


def main_window_title(pid=None):
    """Title of this process's main window, e.g. 'Diana Interactive Environment — beam.dpf*' (Windows only)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32")
        pid = pid or os.getpid()
        titles = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def collect(hwnd, _):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
                length = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                titles.append(buf.value)
            return True

        user32.EnumWindows(enum_proc(collect), 0)
        ours = [t for t in titles if t.startswith(TITLE_PREFIX)]
        return (ours or titles or [None])[0]
    except Exception:
        return None


def parse_title(title):
    """(project name without .dpf, has unsaved changes) from a DianaIE window title."""
    if not title or not title.startswith(TITLE_PREFIX):
        return None, None
    rest = title[len(TITLE_PREFIX):].strip(" —-")
    if not rest:
        return "", False
    modified = rest.endswith("*")
    name = rest.rstrip("*").strip()
    if name.lower().endswith(".dpf"):
        name = name[:-4]
    return name, modified


class Journal(object):
    """Tags each new part of DianaIE's command log with who wrote it; one JSON record per line in a .jsonl file."""

    def __init__(self, log_path, journal_path, counts=undo_counts):
        self.log_path = log_path
        self.journal_path = journal_path
        self.counts = counts
        self.offset = self._size() or 0
        folder = os.path.dirname(journal_path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        undo, redo = self.counts()
        self._write({"event": "start", "pid": os.getpid(), "log": log_path, "offset": self.offset,
                     "t": round(time.time(), 3), "undo": undo, "redo": redo, "bridge_version": BRIDGE_VERSION})

    def _size(self):
        try:
            return os.path.getsize(self.log_path)
        except OSError:
            return None

    def flush(self, source):
        """Record the complete lines appended since the last flush as written by ``source``; returns their size."""
        size = self._size()
        if size is None or size == self.offset:
            return 0
        if size < self.offset:  # the history file was replaced
            self._write({"event": "reset", "offset": 0, "t": round(time.time(), 3)})
            self.offset = 0
        with open(self.log_path, "rb") as f:
            f.seek(self.offset)
            data = f.read(size - self.offset)
        cut = data.rfind(b"\n")
        if cut < 0:
            return 0  # a line is still being written
        start, end = self.offset, self.offset + cut + 1
        undo, redo = self.counts()
        self._write({"start": start, "end": end, "src": source, "t": round(time.time(), 3), "undo": undo, "redo": redo})
        self.offset = end
        return end - start

    def _write(self, record):
        try:
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass


class BridgeServer(object):
    """Socket server whose ``poll`` method is driven from the DianaIE main thread."""

    def __init__(self, namespace, host="127.0.0.1", port=0, token=None, log_path=None, journal=None):
        self.namespace = namespace
        self.token = token or uuid.uuid4().hex
        self.log_path = log_path
        self.journal = journal
        self.diana_version = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind((host, port))
        self.sock.listen(8)
        self.sock.setblocking(False)
        self.host, self.port = self.sock.getsockname()[:2]
        self.busy = False
        self.closed = False
        self.requests = 0
        self.started = time.time()
        self.on_close = []

    def log(self, message):
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
        except Exception:
            pass

    def journal_flush(self, source):
        if self.journal is not None:
            try:
                self.journal.flush(source)
            except Exception:
                self.log("journal failed:\n" + traceback.format_exc())

    def poll(self):
        """Serve every pending connection. Re-entrant calls (e.g. from a modal dialog) are ignored."""
        if self.busy or self.closed:
            return
        self.busy = True
        try:
            # Whatever reached the command log since the last tick came from the user (GUI or console).
            self.journal_flush("user")
            while not self.closed:
                ready = select.select([self.sock], [], [], 0)[0]
                if not ready:
                    break
                conn = self.sock.accept()[0]
                self._serve_connection(conn)
                self.journal_flush("ai")
        except Exception:
            self.log("poll failed:\n" + traceback.format_exc())
        finally:
            self.busy = False

    def _serve_connection(self, conn):
        try:
            conn.setblocking(True)
            conn.settimeout(3)  # clients send immediately; never stall the GUI thread for long
            raw = b""
            while not raw.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                raw += chunk
            response = self.dispatch(raw)
            conn.settimeout(None)
            conn.sendall(json.dumps(response).encode("utf-8") + b"\n")
        except Exception:
            self.log("connection failed:\n" + traceback.format_exc())
        finally:
            conn.close()

    def dispatch(self, raw):
        try:
            request = json.loads(raw.decode("utf-8"))
        except Exception:
            return {"id": None, "ok": False, "error": "malformed request"}
        request_id = request.get("id")
        if not hmac.compare_digest(str(request.get("token", "")).encode("utf-8"), self.token.encode("utf-8")):
            return {"id": request_id, "ok": False, "error": "invalid token"}
        self.requests += 1
        op = request.get("op")
        deadline = request.get("deadline")
        if deadline is not None and time.time() > float(deadline):
            # The request waited in the socket backlog while DianaIE was busy and the client gave up.
            response = {"ok": False, "expired": True, "error": "request expired before DianaIE could run it"}
        elif op == "ping":
            response = {"ok": True, "info": self.info()}
        elif op == "exec":
            response = self.execute(request.get("code", ""))
        elif op == "stop":
            self.close()
            response = {"ok": True}
        else:
            response = {"ok": False, "error": "unknown op: %r" % (op,)}
        response["id"] = request_id
        return response

    def info(self):
        """Session facts for ping. Uses no logged DIANA command: ping runs every time the server looks for sessions."""
        info = {
            "bridge_version": BRIDGE_VERSION,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "cwd": os.getcwd(),
            "requests": self.requests,
            "uptime_s": round(time.time() - self.started, 1),
        }
        if self.diana_version:
            info["diana_version"] = self.diana_version
        title = main_window_title()
        project, modified = parse_title(title)
        if project is not None:
            info["project"] = project
            info["unsaved_changes"] = modified
        if self.journal is not None:
            info["log_file"] = self.journal.log_path
            info["journal"] = self.journal.journal_path
        return info

    def execute(self, code):
        """Run ``code`` in the shared namespace; if it ends with an expression, return its repr."""
        stdout, stderr = io.StringIO(), io.StringIO()
        value, error = None, None
        started = time.time()
        protected = {}

        def gated_solver(*args, **kwargs):
            raise RuntimeError("direct DianaIE solver calls are blocked; use the reviewed MCP solve workflow")

        # The MCP-side AST check gives an early, friendly error.  This namespace guard is the enforcement boundary:
        # aliases and globals()['runSolver'] cannot bypass it.  GUI actions and the user's own command console are
        # outside bridge requests and remain unaffected.
        for name in SOLVER_ENTRYPOINTS:
            protected[name] = (name in self.namespace, self.namespace.get(name))
            self.namespace[name] = gated_solver
        try:
            tree = ast.parse(code, "<mcp>", "exec")
            last_expr = None
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last_expr = ast.Expression(tree.body.pop().value)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exec(compile(tree, "<mcp>", "exec"), self.namespace)
                if last_expr is not None:
                    result = eval(compile(last_expr, "<mcp>", "eval"), self.namespace)
                    if result is not None:
                        value = repr(result)
        except BaseException:  # SystemExit from user code must not take DianaIE down
            error = traceback.format_exc()
        finally:
            for name, (existed, original) in protected.items():
                if existed:
                    self.namespace[name] = original
                else:
                    self.namespace.pop(name, None)
        return {
            "ok": error is None,
            "stdout": _truncate(stdout.getvalue()),
            "stderr": _truncate(stderr.getvalue()),
            "value": _truncate(value),
            "error": _truncate(error),
            "seconds": round(time.time() - started, 3),
        }

    def close(self):
        if self.closed:
            return
        self.closed = True
        # A restart from inside a request (start() closes the running bridge) is the bridge's own doing.
        self.journal_flush("ai" if self.busy else "user")
        self.journal = None
        for callback in self.on_close:
            try:
                callback()
            except Exception:
                self.log("close callback failed:\n" + traceback.format_exc())
        try:
            self.sock.close()
        except Exception:
            pass
        self.log("bridge closed")


def install_win32_timer(server, interval_ms=DEFAULT_TIMER_MS):
    """Call ``server.poll`` every ``interval_ms`` from the calling (GUI) thread's message loop."""
    import ctypes
    import ctypes.wintypes as wt

    timerproc_type = ctypes.WINFUNCTYPE(None, wt.HWND, wt.UINT, ctypes.c_size_t, wt.DWORD)
    user32 = ctypes.windll.user32
    user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, timerproc_type]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]

    def on_tick(hwnd, message, event_id, when):
        server.poll()

    callback = timerproc_type(on_tick)
    timer_id = user32.SetTimer(None, 0, interval_ms, callback)
    if not timer_id:
        raise OSError("SetTimer failed")
    server.timer_refs = (callback, timer_id)  # the ctypes callback must outlive this function
    server.on_close.append(lambda: user32.KillTimer(None, timer_id))
    return timer_id


def write_session_file(server, session_dir, launch_id=None):
    if not os.path.isdir(session_dir):
        os.makedirs(session_dir)
    path = os.path.join(session_dir, "%d.json" % os.getpid())
    data = {
        "pid": os.getpid(),
        "host": server.host,
        "port": server.port,
        "token": server.token,
        "launch_id": launch_id,
        "bridge_version": BRIDGE_VERSION,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "executable": sys.executable,
        "diana_version": server.diana_version,
    }
    if server.journal is not None:
        data["log_file"] = server.journal.log_path
        data["journal"] = server.journal.journal_path
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)

    def remove():
        try:
            os.remove(path)
        except OSError:
            pass

    server.on_close.append(remove)
    return path


def _diana_namespace(own_globals):
    """DianaIE's scripting namespace: injected by the loader, our own globals, or ``__main__``."""
    if own_globals.get("DIANA_NAMESPACE") is not None:
        return own_globals["DIANA_NAMESPACE"]
    if "diaVersion" in own_globals:
        return own_globals
    main = sys.modules.get("__main__")
    if main is not None and "diaVersion" in vars(main):
        return vars(main)
    return None


def remember_log_dir(state_dir, log_dir):
    """Keep a list of DianaIE history folders so the server finds them without a bridge (the folder is a setting)."""
    path = os.path.join(state_dir, "log_dirs.json")
    known = []
    try:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                known = json.load(f)
    except Exception:
        known = []
    if log_dir not in known:
        known.append(log_dir)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(known, f)
        except Exception:
            pass


def open_journal(namespace, state_dir, log):
    """Journal on DianaIE's command log (logFileName() is not itself logged); None if unavailable."""
    log_file_name = namespace.get("logFileName")
    if not callable(log_file_name):
        return None
    try:
        log_path = os.path.normpath(str(log_file_name()))
        if not os.path.isfile(log_path):
            return None
        remember_log_dir(state_dir, os.path.dirname(log_path))
        return Journal(log_path, os.path.join(state_dir, "journals", log_key(log_path) + ".jsonl"))
    except Exception:
        log("journal unavailable:\n" + traceback.format_exc())
        return None


def start(namespace=None, port=None, token=None, session_dir=None, interval_ms=DEFAULT_TIMER_MS):
    """Start (or restart) the bridge inside the current DianaIE session."""
    previous = getattr(builtins, "_diana_mcp_bridge", None)
    if previous is not None:
        previous.close()
    if namespace is None:
        namespace = _diana_namespace(globals())
    if namespace is None:
        raise RuntimeError("DIANA scripting namespace not found; run this inside DianaIE")
    if port is None:
        port = int(os.environ.get("DIANA_MCP_PORT", "0") or 0)
    session_dir = session_dir or os.environ.get("DIANA_MCP_SESSION_DIR") or default_session_dir()
    if not os.path.isdir(session_dir):
        os.makedirs(session_dir)
    log_path = os.path.join(session_dir, "%d.log" % os.getpid())
    server = BridgeServer(namespace, port=port, token=token or os.environ.get("DIANA_MCP_TOKEN") or None,
                          log_path=log_path)
    server.journal = open_journal(namespace, os.path.dirname(os.path.abspath(session_dir)), server.log)
    server.journal_flush("user")
    version = namespace.get("diaVersion")
    if callable(version):  # logged once by DianaIE; tagged as the bridge's own call below
        try:
            server.diana_version = str(version())
        except Exception as exc:
            server.log("diaVersion failed: %s" % exc)
    server.journal_flush("ai")
    workdir = os.environ.get("DIANA_MCP_WORKDIR")
    if workdir and os.path.isdir(workdir):
        os.chdir(workdir)  # DianaIE ignores the launch cwd and defaults to the user's Documents folder
    install_win32_timer(server, interval_ms)
    write_session_file(server, session_dir, os.environ.get("DIANA_MCP_LAUNCH_ID"))
    atexit.register(server.close)
    builtins._diana_mcp_bridge = server
    server.log("bridge %s listening on %s:%d" % (BRIDGE_VERSION, server.host, server.port))
    print("DIANA MCP bridge %s listening on %s:%d" % (BRIDGE_VERSION, server.host, server.port))
    return server


def run_standalone(session_dir, port=0, token=None):
    """Test mode without DianaIE: serve from a plain polling loop in the main thread.

    With DIANA_MCP_TEST_LOG set, that file plays DianaIE's command log, so the change journal can be tested.
    """
    namespace = {"__name__": "__diana_mcp_standalone__"}
    server = BridgeServer(namespace, port=port, token=token)
    test_log = os.environ.get("DIANA_MCP_TEST_LOG")
    if test_log:
        namespace["logFileName"] = lambda: test_log
        server.journal = open_journal(namespace, os.path.dirname(os.path.abspath(session_dir)), server.log)
    write_session_file(server, session_dir, os.environ.get("DIANA_MCP_LAUNCH_ID"))
    while not server.closed:
        server.poll()
        time.sleep(0.01)


def _inside_diana():
    return _diana_namespace(globals()) is not None


if __name__ == "__main__" and "--standalone" in sys.argv:
    _args = sys.argv[sys.argv.index("--standalone") + 1:]
    run_standalone(_args[0], int(_args[1]) if len(_args) > 1 else 0, _args[2] if len(_args) > 2 else None)
elif _inside_diana():
    start()
