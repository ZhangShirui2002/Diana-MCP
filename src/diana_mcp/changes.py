"""What happened in DianaIE: its command history, who did each step, what each call means, what an undo reverted.

DianaIE writes every command it runs, from the GUI, its command console or the bridge, as a line of Python to a
history file (~/.DianaIE/IEhistory_<yymmdd>_<hhmmss>.<random>.running.py while it runs). The bridge adds a journal
that tags each part of that file with its source ("user" or "ai") and DianaIE's undo/redo counts; without a bridge the
history file alone still gives the steps, with an unknown source.
"""
from __future__ import annotations

import ast
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config

LOG_NAME = re.compile(r"^(IEhistory_(\d{6})_(\d{6}))(?:\.(\w+))?(\.running)?\.py$")
DEFAULT_CATEGORIES = ("model", "analysis", "project", "undo", "run")
ALL_CATEGORIES = DEFAULT_CATEGORIES + ("results", "view", "report", "query", "other")


# --- history files ----------------------------------------------------------------------------------------------

def log_key(path: str | os.PathLike) -> str:
    """Must match log_key in bridge/diana_bridge.py."""
    name = Path(path).name
    match = re.match(r"(IEhistory_\d{6}_\d{6})", name)
    return match.group(1) if match else Path(name).stem


@dataclass(frozen=True)
class HistoryFile:
    path: Path
    key: str
    started: datetime
    running: bool

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def describe(self) -> dict[str, Any]:
        return {"file": str(self.path), "started": self.started.isoformat(sep=" "), "running": self.running,
                "size": self.size}


def log_dirs() -> list[Path]:
    """DianaIE history folders: the default ~/.DianaIE plus folders reported by bridges (it is a DianaIE setting).

    Off Windows "home" is DianaIE's home inside the Wine prefix, and the reported folders are Windows paths.
    """
    home = Path.home() if config.IS_WINDOWS else config.to_host(f"C:/users/{config.wine_user()}")
    dirs = [home / ".DianaIE"]
    known = config.state_dir() / "log_dirs.json"
    try:
        dirs += [config.to_host(d) for d in json.loads(known.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        pass
    unique: dict[str, Path] = {}
    for d in dirs:
        if d.is_dir():
            unique.setdefault(os.path.normcase(str(d.resolve())), d)
    return list(unique.values())


def history_files(dirs: Iterable[Path] | None = None) -> list[HistoryFile]:
    """All DianaIE history files, oldest first."""
    found: dict[str, HistoryFile] = {}
    for directory in dirs if dirs is not None else log_dirs():
        for path in Path(directory).glob("IEhistory_*.py"):
            match = LOG_NAME.match(path.name)
            if not match:
                continue
            try:
                started = datetime.strptime(match.group(2) + match.group(3), "%y%m%d%H%M%S")
            except ValueError:
                continue
            item = HistoryFile(path, match.group(1), started, bool(match.group(5)))
            previous = found.get(item.key)
            if previous is None or (item.running and not previous.running):
                found[item.key] = item
    return sorted(found.values(), key=lambda h: h.started)


def find_history(key_or_path: str, dirs: Iterable[Path] | None = None) -> HistoryFile | None:
    path = Path(key_or_path)
    if path.is_file():
        match = LOG_NAME.match(path.name)
        started = datetime.strptime(match.group(2) + match.group(3), "%y%m%d%H%M%S") if match else datetime.now()
        return HistoryFile(path, log_key(path), started, bool(match and match.group(5)))
    key = log_key(key_or_path)
    for item in history_files(dirs):
        if item.key == key:
            return item
    return None


def dianaie_processes() -> list[tuple[int, datetime]]:
    """Running DianaIE.exe processes with their start times (Windows)."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi")
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    pids = (wintypes.DWORD * 8192)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed)):
        return []
    found = []
    for pid in pids[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                continue
            if Path(buf.value).name.lower() != "dianaie.exe":
                continue
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
                continue
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            created = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
            found.append((int(pid), created.astimezone().replace(tzinfo=None)))
        finally:
            kernel32.CloseHandle(handle)
    return sorted(found, key=lambda p: p[1])


def logs_of_running_dianaie(files: list[HistoryFile] | None = None) -> dict[int, HistoryFile]:
    """History file of each running DianaIE: the first running file started after the process (DianaIE starts
    logging a few seconds to a minute after launch)."""
    files = [f for f in (files if files is not None else history_files()) if f.running]
    taken: set[str] = set()
    out: dict[int, HistoryFile] = {}
    for pid, created in dianaie_processes():
        for item in files:
            if item.key not in taken and item.started >= created - timedelta(seconds=5):
                out[pid] = item
                taken.add(item.key)
                break
    return out


# --- entries ----------------------------------------------------------------------------------------------------

@dataclass
class Entry:
    offset: int
    end: int
    text: str
    name: str | None = None
    args: list[Any] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)
    category: str = "other"
    source: str = "unknown"  # user (GUI / command console), ai (the bridge) or unknown (no journal)
    time: float | None = None
    batch: int | None = None  # journal record: lines DianaIE wrote between two bridge ticks, i.e. one GUI action
    undo: int | None = None
    redo: int | None = None
    undone: bool = False
    reverted: list[str] = field(default_factory=list)
    description: str = ""

    @property
    def modifying(self) -> bool:
        return self.category in ("model", "analysis") or self.name in IMPORTS

    def view(self) -> dict[str, Any]:
        out: dict[str, Any] = {"offset": self.offset, "source": self.source, "category": self.category,
                               "what": self.description, "call": self.text}
        if self.time:
            out["time"] = time.strftime("%H:%M:%S", time.localtime(self.time))
        if self.undone:
            out["undone"] = True
        if self.reverted:
            out["reverted"] = self.reverted
        return out


def _literal(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return ast.unparse(node)


def parse_call(text: str) -> tuple[str, list[Any], dict[str, Any]] | None:
    """('setParameter', [...], {...}) for one logged call, or None if the text is not a single call."""
    try:
        node = ast.parse(text.strip(), mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return None
    return name, [_literal(a) for a in node.args], {k.arg or "**": _literal(k.value) for k in node.keywords}


def read_log(path: Path, start: int = 0, end: int | None = None) -> tuple[list[Entry], int]:
    """Calls in the history file from byte ``start`` (a line start) to ``end``; returns them and the next offset.

    A call that spans several lines (a string or list broken over lines) becomes one entry; an unfinished last
    line is left for the next read.
    """
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read() if end is None else f.read(max(0, end - start))
    except OSError:
        return [], start
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], start
    data = data[: cut + 1]
    entries: list[Entry] = []
    pending: list[tuple[int, bytes]] = []
    position = start
    for raw in data.splitlines(keepends=True):
        line_start, position = position, position + len(raw)
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not pending and (not text.strip() or text.lstrip().startswith("#")):
            continue
        pending.append((line_start, raw))
        joined = b"".join(r for _, r in pending).decode("utf-8", errors="replace").strip()
        parsed = parse_call(joined)
        if parsed is None and len(pending) < 40 and _unbalanced(joined):
            continue
        entry = Entry(pending[0][0], position, joined)
        if parsed:
            entry.name, entry.args, entry.kwargs = parsed
            entry.category = category(entry.name)
        entries.append(entry)
        pending = []
    next_offset = pending[0][0] if pending else start + len(data)
    return entries, next_offset


def _unbalanced(text: str) -> bool:
    depth = 0
    quote = None
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
    return depth > 0 or quote is not None


# --- categories -------------------------------------------------------------------------------------------------

PROJECT = {
    "newProject", "openProject", "saveProject", "saveProjectAs", "closeProject", "exportModel",
    "saveAnalysisCommands", "convertModelFile", "convertCommandFile", "setProjectSetting", "clearProjectSetting",
    "setDocumentProperty", "setDocumentDescription", "removeDocumentProperty",
}
IMPORTS = {
    "importModel", "importGeometry", "importMaterials", "importPoints", "importSheet", "importSurfaces",
    "importBezierSurface", "importGOCAD", "importBaseExcitationLoads",
}
RUN = {"runSolver", "stopAnalysis", "runPipe", "runQuake", "runRCBeam", "runSealec", "runSlab", "runStabor"}
RESULTS = {
    "loadResults", "unloadResults", "clearResults", "setResultCase", "nextResultCase", "previousResultCase",
    "firstResultCase", "lastResultCase", "selectResult", "setResultPlot", "hideResultPlot", "setResultLayer",
    "gotoFrame", "enableAnimation", "saveAnimation", "exportResults", "exportResultsToCSV", "exportNodeResultsToCSV",
    "exportElementResultsToCSV", "exportReinforcementResultsToCSV", "exportResultsChart", "resultsTable",
    "resultsChart", "lineChart", "barChart", "chartToImage", "probeCurveSamplePoints", "probeCurveSampleValues",
}
REPORT_WORDS = ("Report", "Chapter", "Section")
VIEW = {
    "fitAll", "saveImage", "writeToPng", "rotateCamera", "perspectiveProjection", "setRenderMode", "setTransparency",
    "setColor", "highlight", "locate", "clearSelection", "setViewerEnabled", "enableVirtualTransformation",
    "setVirtualTransformation", "setActivePhase", "draw", "addRuler", "setCurrentShapeSet", "saveViewPoint",
    "removeViewPoint", "zoomOut", "zoomIn",
}
VIEW_PREFIXES = ("show", "hide", "select", "deselect", "setViewPoint", "setWorkingPlane")
VIEW_WORDS = ("ViewSetting", "Color", "InWindow")
MODIFYING_PREFIXES = (
    "add", "create", "remove", "rename", "set", "assign", "unassign", "attach", "detach", "move", "copy", "duplicate",
    "array", "mirror", "rotate", "translate", "scale", "extrude", "sweep", "loft", "unite", "subtract", "intersect",
    "imprint", "cut", "trim", "extend", "split", "join", "heal", "sew", "clean", "break", "explode", "fillet",
    "apply", "align", "reverse", "convert", "generate", "clear", "merge", "upgrade", "reset", "revert", "optimize",
    "pointsTo", "wireTo", "sheetTo", "projection", "autoConnection", "swap", "load", "change", "undo", "redo",
)


def category(name: str) -> str:
    if name in ("undo", "redo"):
        return "undo"
    if name in PROJECT or name in IMPORTS:
        return "project"
    if name in RUN:
        return "run"
    if name in RESULTS:
        return "results"
    if name in VIEW or name.startswith(VIEW_PREFIXES) or any(w in name for w in VIEW_WORDS):
        return "view"
    if any(w in name for w in REPORT_WORDS) or name in ("createReport", "generateReport", "saveReport"):
        return "report"
    if not name.startswith(MODIFYING_PREFIXES):
        return "query"
    if "Analysis" in name or "Phase" in name or name in ("setActiveInPhase", "assignMaterialForPhase"):
        return "analysis"
    return "model"


# --- descriptions -----------------------------------------------------------------------------------------------

TYPE_ZH = {
    "MATERIAL": "材料", "GEOMET": "几何属性", "DATA": "单元数据", "GEOMETRYLOAD": "荷载", "GEOMETRYSUPPORT": "支座",
    "GEOMETRYTYING": "约束", "GEOMETRYCONNECTION": "连接", "GEOMETRYBC": "边界条件", "GEOMETRYFIXPOT": "固定势",
    "GEOMETRYINIFIELD": "初始场", "GEOMETRYNODEGROUPITEM": "节点组项", "GEOMETRYNODEGROUP": "节点组",
    "SHAPE": "几何体", "SHAPESET": "几何体集", "GEOMETRYLOADSET": "荷载工况", "GEOMETRYSUPPORTSET": "支座集",
    "GEOMETRYTYINGSET": "约束集", "GEOMETRYBCSET": "边界条件集", "GEOMETRYFIXPOTSET": "固定势集",
    "GEOMETRYWATERLEVELSET": "水位集", "ELEMENTSET": "单元集", "REINFORCEMENTSET": "钢筋集",
    "REINFORCEMENTSHAPE": "钢筋", "PILESHAPE": "桩", "ANCHORSHAPE": "锚杆", "PILESHEETWALLSHAPE": "板桩墙",
    "SHAPEFACE": "面", "SHAPEEDGE": "边", "SHAPEVERTEX": "顶点", "ELEMENT": "单元", "NODE": "节点",
    "SUPPORTSET": "网格支座集", "TYINGSET": "网格约束集", "CONNECTION": "连接", "GEOMETRYPILESET": "桩集",
}
PARAM_ZH = {
    "YOUNG": "弹性模量", "POISON": "泊松比", "DENSIT": "密度", "THICK": "厚度", "TENSTR": "抗拉强度",
    "COMSTR": "抗压强度", "GF1": "受拉断裂能", "GC": "受压断裂能", "TENCRV": "受拉软化曲线", "COMCRV": "受压曲线",
    "TOTCRK": "裂缝模型", "VALUE": "数值", "DIRECT": "方向", "TRANSL": "平动约束", "ROTATI": "转动约束",
    "AXES": "约束方向", "TARGET": "挂载位置", "LODTYP": "荷载类型", "MATMDL": "材料模型", "MCNAME": "材料类别",
    "GEOMDL": "几何模型", "GCNAME": "几何类别", "SIZES": "荷载步", "MAXITE": "最大迭代次数", "NMODES": "模态数",
    "LINPAG": "每页行数", "COLLIN": "行宽", "DEVICE": "输出设备", "SELTYP": "输出选择", "ELEMEN": "单元类型",
    "CRACKB": "裂缝带宽", "SHRCRV": "剪切保留", "BETA": "剪切保留系数", "YLDSTR": "屈服应力", "THERMX": "热膨胀系数",
    "RADIUS": "半径", "DIAMET": "直径", "AREA": "面积", "SPACIN": "间距", "PERIME": "周长", "DSNY": "法向刚度",
    "DSSX": "切向刚度", "DSSY": "切向刚度", "KNY": "法向刚度", "FRICTI": "摩擦", "COHESI": "黏聚力",
    "METHOD": "方法", "CONVER": "收敛准则", "LABEL": "标签", "FACTOR": "系数", "TIMES": "时间点",
}
_UNITS_ZH = {"LENGTH": "长度", "MASS": "质量", "FORCE": "力", "TIME": "时间", "TEMPER": "温度", "ANGLE": "角度"}


def fmt(value: Any, limit: int = 60) -> str:
    """Compact text of a call argument."""
    if isinstance(value, float):
        text = f"{value:.6g}"
    elif isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple)):
        if value and all(isinstance(v, (list, tuple)) for v in value):
            return f"{len(value)} 个点"
        text = "[" + ", ".join(fmt(v, 20) for v in value) + "]"
    elif isinstance(value, dict):
        text = "{" + ", ".join(f"{k}: {fmt(v, 20)}" for k, v in value.items()) + "}"
    else:
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def names(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        if not value:
            return "全部"
        shown = "、".join(f"“{v}”" for v in value[:5])
        return shown + (f" 等 {len(value)} 个" if len(value) > 5 else "")
    return f"“{value}”"


def param_title(path: str, schema: Any = None) -> str:
    parts = [p for p in str(path).strip("/").split("/") if p]
    if not parts:
        return ""
    keyword = re.sub(r"\(\d*\)$", "", parts[-1])
    if keyword in PARAM_ZH:
        return PARAM_ZH[keyword]
    if keyword == "RESULT" and len(parts) > 1 and parts[-2].startswith("DIGITS"):
        return "结果有效位数"
    if schema is not None:
        try:
            entry = schema.describe(keyword, re.sub(r"\(\d*\)$", "", parts[-2]) if len(parts) > 1 else None)
            if entry and entry.title:
                from .glossary import default_glossary  # the Chinese GUI name of the English schema title

                glossary = default_glossary()
                return (glossary.to_chinese(entry.title) if glossary else None) or entry.title
        except Exception:
            pass
    return ""


def _a(args: list[Any], i: int, default: Any = None) -> Any:
    return args[i] if len(args) > i else default


def _type(value: Any) -> str:
    return TYPE_ZH.get(str(value).upper(), str(value))


def _create(kind: str) -> Callable[[list[Any]], str]:
    def describe(a: list[Any]) -> str:
        container = _a(a, 1)
        return f"新建{kind} “{_a(a, 0)}”" + (f"（归入 “{container}”）" if container else "")
    return describe


DESCRIBE: dict[str, Callable[[list[Any]], str]] = {
    "setAnalysisCommandDetail": lambda a: f"分析 “{_a(a, 0)}” / {_a(a, 1)}：{_a(a, 2)} = {fmt(_a(a, 3))}",
    "addAnalysisCommandDetail": lambda a: f"分析 “{_a(a, 0)}” / {_a(a, 1)}：添加 {_a(a, 2)}",
    "removeAnalysisCommandDetail": lambda a: f"分析 “{_a(a, 0)}” / {_a(a, 1)}：删除 {_a(a, 2)}",
    "renameAnalysisCommandDetail": lambda a: f"分析 “{_a(a, 0)}” / {_a(a, 1)}：{_a(a, 2)} 改名为 “{_a(a, 3)}”",
    "copyAnalysisCommandDetail": lambda a: f"分析 “{_a(a, 0)}” / {_a(a, 1)}：复制 {_a(a, 2)}",
    "swapAnalysisCommandDetails": lambda a: f"分析 “{_a(a, 0)}” / {_a(a, 1)}：交换 {_a(a, 2)} 与 {_a(a, 3)}",
    "addAnalysis": lambda a: f"新建分析 “{_a(a, 0)}”",
    "removeAnalysis": lambda a: f"删除分析 “{_a(a, 0)}”",
    "renameAnalysis": lambda a: f"分析 “{_a(a, 0)}” 改名为 “{_a(a, 1)}”",
    "copyAnalysis": lambda a: f"复制分析 “{_a(a, 0)}”",
    "addAnalysisCommand": lambda a: f"分析 “{_a(a, 0)}” 添加命令 “{_a(a, 2)}”（{_a(a, 1)}）",
    "removeAnalysisCommand": lambda a: f"分析 “{_a(a, 0)}” 删除命令 “{_a(a, 1)}”",
    "renameAnalysisCommand": lambda a: f"分析 “{_a(a, 0)}” 的命令 “{_a(a, 1)}” 改名为 “{_a(a, 2)}”",
    "copyAnalysisCommand": lambda a: f"分析 “{_a(a, 0)}” 复制命令 “{_a(a, 1)}”",
    "loadAnalysisCommands": lambda a: f"分析 “{_a(a, 0)}” 从文件载入命令 {_a(a, 1)}",
    "setActiveInPhase": lambda a: (f"分析 “{_a(a, 0)}” 阶段 {names(_a(a, 3))}：{_type(_a(a, 1))} {names(_a(a, 2))} "
                                   + ("激活" if _a(a, 4) else "冻结")),
    "assignMaterialForPhase": lambda a: f"分析 “{_a(a, 0)}” 阶段 {names(_a(a, 3))}：{names(_a(a, 2))} 改用材料 “{_a(a, 4)}”",
    "attach": lambda a: (f"{_type(_a(a, 0))} “{_a(a, 1)}” 挂到 {names(_a(a, 2))}"
                         + (f" 的 {fmt(_a(a, 3))}" if len(a) > 3 else "")),
    "attachTo": lambda a: (f"{_type(_a(a, 0))} “{_a(a, 1)}” 的 {_a(a, 2)} 挂到 {names(_a(a, 3))}"
                           + (f" 的 {fmt(_a(a, 4))}" if len(a) > 4 else "")),
    "detach": lambda a: f"{_type(_a(a, 0))} “{_a(a, 1)}” 取消挂载" + (f"（{names(_a(a, 2))}）" if len(a) > 2 else ""),
    "detachFrom": lambda a: f"{_type(_a(a, 0))} “{_a(a, 1)}” 的 {_a(a, 2)} 取消挂载",
    "addSet": lambda a: f"新建{_type(_a(a, 0))} “{_a(a, 1)}”",
    "remove": lambda a: f"删除{_type(_a(a, 0))} {names(_a(a, 1))}",
    "rename": lambda a: f"{_type(_a(a, 0))} “{_a(a, 1)}” 改名为 “{_a(a, 2)}”",
    "copy": lambda a: f"复制{_type(_a(a, 0))} “{_a(a, 1)}” 为 “{_a(a, 2)}”",
    "createPointLoad": _create("点荷载"), "createLineLoad": _create("线荷载"),
    "createSurfaceLoad": _create("面荷载"), "createBodyLoad": _create("体荷载"), "createModelLoad": _create("模型荷载"),
    "createPointSupport": _create("点支座"), "createLineSupport": _create("线支座"),
    "createSurfaceSupport": _create("面支座"), "createBodySupport": _create("体支座"),
    "createPointTying": _create("点约束"), "createLineTying": _create("线约束"),
    "createSurfaceTying": _create("面约束"), "createBodyTying": _create("体约束"),
    "createPointNodeGroupItem": _create("点节点组项"), "createLineNodeGroupItem": _create("线节点组项"),
    "createSurfaceNodeGroupItem": _create("面节点组项"), "createBodyNodeGroupItem": _create("体节点组项"),
    "createPointBoundaryCondition": _create("点边界条件"), "createLineBoundaryCondition": _create("线边界条件"),
    "createSurfaceBoundaryCondition": _create("面边界条件"), "createBodyBoundaryCondition": _create("体边界条件"),
    "createPointInitialField": _create("点初始场"), "createLineInitialField": _create("线初始场"),
    "createSurfaceInitialField": _create("面初始场"), "createBodyInitialField": _create("体初始场"),
    "createPointFixedPotential": _create("点固定势"), "createLineFixedPotential": _create("线固定势"),
    "createSurfaceFixedPotential": _create("面固定势"), "createBodyFixedPotential": _create("体固定势"),
    "createPointConnection": _create("点连接"), "createLineConnection": _create("线连接"),
    "createSurfaceConnection": _create("面连接"),
    "createConnection": lambda a: f"新建连接 “{_a(a, 0)}”（{_a(a, 1)}）",
    "addMaterial": lambda a: f"新建材料 “{_a(a, 0)}”（{_a(a, 1)} / {_a(a, 2)}）",
    "addGeometry": lambda a: f"新建几何属性 “{_a(a, 0)}”（{_a(a, 1)} / {_a(a, 2)}）",
    "addElementData": lambda a: f"新建单元数据 “{_a(a, 0)}”",
    "removeMaterial": lambda a: f"删除材料 “{_a(a, 0)}”",
    "removeGeometry": lambda a: f"删除几何属性 “{_a(a, 0)}”",
    "renameMaterial": lambda a: f"材料 “{_a(a, 0)}” 改名为 “{_a(a, 1)}”",
    "renameGeometry": lambda a: f"几何属性 “{_a(a, 0)}” 改名为 “{_a(a, 1)}”",
    "copyMaterial": lambda a: f"复制材料 “{_a(a, 0)}”",
    "assignMaterial": lambda a: f"{names(_a(a, 2))} 指定材料 “{_a(a, 0)}”",
    "assignGeometry": lambda a: f"{names(_a(a, 2))} 指定几何属性 “{_a(a, 0)}”",
    "assignElementData": lambda a: f"{names(_a(a, 2))} 指定单元数据 “{_a(a, 0)}”",
    "unassignMaterial": lambda a: f"取消 {names(_a(a, 1))} 的材料",
    "resetGeometry": lambda a: f"取消 {names(_a(a, 1))} 的几何属性",
    "resetElementData": lambda a: f"取消 {names(_a(a, 1))} 的单元数据",
    "resetMaterial": lambda a: f"取消 {names(_a(a, 1))} 的材料",
    "setElementClassType": lambda a: (f"{names(_a(a, 1) if len(a) > 2 else _a(a, 0))} 的单元类别设为 "
                                      f"{_a(a, 2) if len(a) > 2 else _a(a, 1)}"),
    "setUnit": lambda a: f"单位：{_UNITS_ZH.get(str(_a(a, 0)), _a(a, 0))} 改为 {_a(a, 1)}",
    "setUnitSet": lambda a: f"单位制改为 {_a(a, 0)}",
    "setModelDimension": lambda a: f"模型维度设为 {_a(a, 0)}",
    "setModelAnalysisAspects": lambda a: f"分析类型设为 {fmt(_a(a, 0))}",
    "setModelDefinition": lambda a: f"模型定义：{_a(a, 0)} = {fmt(_a(a, 1))}",
    "createSheet": lambda a: f"新建面 “{_a(a, 0)}”（{len(_a(a, 1) or [])} 个顶点）",
    "createLine": lambda a: f"新建线段 “{_a(a, 0)}”：{fmt(_a(a, 1))} → {fmt(_a(a, 2))}",
    "createPolyline": lambda a: f"新建{'闭合' if _a(a, 2) else ''}折线 “{_a(a, 0)}”（{len(_a(a, 1) or [])} 个点）",
    "createBlock": lambda a: f"新建长方体 “{_a(a, 0)}”：起点 {fmt(_a(a, 1))}，尺寸 {fmt(_a(a, 2))}",
    "createPointBody": lambda a: f"新建点 “{_a(a, 0)}” {fmt(_a(a, 1))}",
    "createArc": lambda a: f"新建圆弧 “{_a(a, 0)}”",
    "createCircle": lambda a: f"新建圆 “{_a(a, 0)}”",
    "createSheetCircle": lambda a: f"新建圆面 “{_a(a, 0)}”",
    "createCylinder": lambda a: f"新建圆柱 “{_a(a, 0)}”",
    "createSphere": lambda a: f"新建球 “{_a(a, 0)}”",
    "createCone": lambda a: f"新建圆锥 “{_a(a, 0)}”",
    "createTorus": lambda a: f"新建圆环 “{_a(a, 0)}”",
    "createPrism": lambda a: f"新建棱柱 “{_a(a, 0)}”",
    "createCurve": lambda a: f"新建曲线 “{_a(a, 0)}”",
    "createSheetFromFaces": lambda a: f"由面生成新面 “{_a(a, 0)}”",
    "createSheetFromLines": lambda a: f"由线生成面 “{_a(a, 0)}”",
    "createWireFromLines": lambda a: f"由线生成线框 “{_a(a, 0)}”",
    "generateMesh": lambda a: f"划分网格（{names(_a(a, 0, []))}）",
    "clearMesh": lambda a: "清除网格",
    "setElementSize": lambda a: (f"{names(_a(a, 0))} 的网格尺寸设为 {fmt(_a(a, 1))}" if len(a) < 4 or not isinstance(_a(a, 1), int)
                                 else f"{names(_a(a, 0))} 局部网格尺寸设为 {fmt(_a(a, 3))}"),
    "setMesherType": lambda a: f"{names(_a(a, 0))} 的网格划分器设为 {_a(a, 1)}",
    "clearMesherType": lambda a: f"{names(_a(a, 0))} 的网格划分器恢复默认",
    "setMeshOrder": lambda a: f"{names(_a(a, 0))} 的单元阶次设为 {_a(a, 1)}",
    "clearMeshOrder": lambda a: f"{names(_a(a, 0))} 的单元阶次恢复默认",
    "setDefaultMeshOrder": lambda a: f"默认单元阶次设为 {_a(a, 0)}",
    "setDefaultMesherType": lambda a: f"默认网格划分器设为 {_a(a, 0)}",
    "setEdgeMeshSeed": lambda a: f"{names(_a(a, 0))} 的边网格种子设为 {fmt(a[-1] if a else '')}",
    "setEdgeGrading": lambda a: f"{names(_a(a, 0))} 的边网格渐变",
    "newProject": lambda a: f"新建项目 {_a(a, 0)}",
    "openProject": lambda a: f"打开项目 {_a(a, 0)}",
    "saveProject": lambda a: "保存项目",
    "saveProjectAs": lambda a: f"项目另存为 {_a(a, 0)}",
    "closeProject": lambda a: "关闭项目",
    "importModel": lambda a: f"导入模型 {_a(a, 0)}",
    "importGeometry": lambda a: f"导入几何 {_a(a, 0)}",
    "exportModel": lambda a: f"导出模型 {_a(a, 0)}",
    "saveAnalysisCommands": lambda a: f"导出分析 “{_a(a, 0)}” 的命令到 {_a(a, 1)}",
    "runSolver": lambda a: f"运行求解（{names(_a(a, 0))}）",
    "stopAnalysis": lambda a: "停止分析",
    "arrayCopy": lambda a: f"阵列复制 {names(_a(a, 0))}（{_a(a, 4)} 份，平移 {fmt(_a(a, 1))}）",
    "translate": lambda a: f"平移 {names(_a(a, 0))}：{fmt(_a(a, 1))}",
    "rotate": lambda a: f"旋转 {names(_a(a, 0))}：{fmt(a[-1] if a else '')}",
    "mirror": lambda a: f"镜像{'复制' if _a(a, 3) else ''} {names(_a(a, 0))}",
    "scale": lambda a: f"缩放 {names(_a(a, 0))}",
    "duplicateShape": lambda a: f"复制几何体 “{_a(a, 0)}”" + (f" 为 “{_a(a, 1)}”" if _a(a, 1) else ""),
    "unite": lambda a: f"合并：{names(_a(a, 1))} 并入 “{_a(a, 0)}”",
    "subtract": lambda a: f"布尔减：从 “{_a(a, 0)}” 中减去 {names(_a(a, 1))}",
    "intersect": lambda a: f"布尔交：“{_a(a, 0)}” 与 {names(_a(a, 1))}",
    "cut": lambda a: f"用 {names(_a(a, 1))} 切割 “{_a(a, 0)}”",
    "imprintIntersection": lambda a: f"把 {names(_a(a, 1))} 与 “{_a(a, 0)}” 的交线印刻上去",
    "trim": lambda a: f"修剪 “{_a(a, 0)}”",
    "extend": lambda a: f"延伸 “{_a(a, 0)}”",
    "extrudeProfile": lambda a: f"拉伸 {names(_a(a, 0))}：{fmt(a[-1] if a else '')}",
    "sweep": lambda a: f"扫掠 {names(_a(a, 0))}",
    "loft": lambda a: f"放样生成 “{_a(a, 0)}”",
    "healAndSew": lambda a: f"修复并缝合 {names(_a(a, 0))}",
    "breakShape": lambda a: f"打断 {names(_a(a, 0))}",
    "explodeShape": lambda a: f"分解 {names(_a(a, 0))}",
    "renameShape": lambda a: f"几何体 {names(_a(a, 0))} 改名为 “{_a(a, 1)}”",
    "removeShape": lambda a: f"删除几何体 {names(_a(a, 0))}",
    "moveToShapeSet": lambda a: f"{names(_a(a, 0))} 移入几何体集 “{_a(a, 1)}”",
    "setShapeType": lambda a: f"{names(_a(a, 1))} 的类型改为 {_a(a, 0)}",
    "setReinforcementType": lambda a: f"{names(_a(a, 1))} 的钢筋类型改为 {_a(a, 2)}",
    "addGeometryLoadCombination": lambda a: f"新建荷载组合 “{_a(a, 0)}”",
    "setGeometryLoadCombinationFactor": lambda a: f"荷载组合 “{_a(a, 0)}”：工况 “{_a(a, 1)}” 系数 = {fmt(_a(a, 2))}",
    "setTimeDependentLoadFactors": lambda a: f"{_type(_a(a, 0))} “{_a(a, 1)}” 的时程系数（{len(_a(a, 2) or [])} 个时间点）",
    "projection": lambda a: (f"把 {names(_a(a, 1))} 投影到 “{_a(a, 0)}” 上" if isinstance(_a(a, 1), (list, tuple))
                             else f"把 {names(_a(a, 3))} 投影到 “{_a(a, 1)}” 上"),
    "alignFaceParallel": lambda a: f"使 “{_a(a, 0)}” 的面与 “{_a(a, 2)}” 平行",
    "alignFaceCoplanar": lambda a: f"使 “{_a(a, 0)}” 的面与 “{_a(a, 2)}” 共面",
    "setMidSideNodeLocation": lambda a: f"{names(_a(a, 0))} 的边中节点位置设为 {_a(a, 1)}",
    "clearMidSideNodeLocation": lambda a: f"{names(_a(a, 0))} 的边中节点位置恢复默认",
    "setDefaultMidSideNodeLocation": lambda a: f"默认边中节点位置设为 {_a(a, 0)}",
    "setReinforcementDiscretization": lambda a: f"{names(_a(a, 0))} 的钢筋离散方式设为 {_a(a, 1)}",
    "setMaterialAspects": lambda a: f"材料 “{_a(a, 0)}” 的附加特性设为 {fmt(_a(a, 1))}",
    "setDefaultGeometryLoadCombinations": lambda a: "按默认方式生成荷载组合",
    "setActionType": lambda a: f"荷载工况 “{_a(a, 0)}” 的作用类型设为 {_a(a, 1)}",
    "setTrafficSetFactor": lambda a: f"交通荷载集 “{_a(a, 0)}” 在 “{_a(a, 1)}” 中的系数 = {fmt(_a(a, 2))}",
    "createSlipSurfaceSet": lambda a: f"新建滑动面集 “{_a(a, 0)}”",
    "autoConnection": lambda a: "自动生成连接（界面）",
    "undo": lambda a: f"撤销 {_a(a, 0, 1)} 步",
    "redo": lambda a: f"重做 {_a(a, 0, 1)} 步",
}


def describe(name: str | None, args: list[Any], text: str = "", schema: Any = None) -> str:
    if name is None:
        return text[:120]
    if name == "setParameter":
        kind, item, path, value = (_a(args, i) for i in range(4))
        title = param_title(path, schema)
        return f"{_type(kind)} “{item}”：{path} = {fmt(value)}" + (f"（{title}）" if title else "")
    handler = DESCRIBE.get(name)
    if handler is not None:
        try:
            return handler(args)
        except Exception:
            pass
    shown = ", ".join(fmt(a, 30) for a in args)
    return f"{name}({shown})"


# --- journal ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class JournalRecord:
    start: int
    end: int
    source: str
    time: float
    undo: int | None
    redo: int | None


def read_journal(path: Path | None) -> tuple[list[dict[str, Any]], list[JournalRecord]]:
    """Start events and tagged ranges from a bridge journal (missing or partly written files are fine)."""
    events: list[dict[str, Any]] = []
    records: list[JournalRecord] = []
    if path is None:
        return events, records
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return events, records
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        if "event" in item:
            events.append(item)
        elif {"start", "end", "src"} <= item.keys():
            records.append(JournalRecord(int(item["start"]), int(item["end"]), str(item["src"]),
                                         float(item.get("t") or 0), item.get("undo"), item.get("redo")))
    return events, records


def journal_path(key: str) -> Path:
    return config.state_dir() / "journals" / f"{key}.jsonl"


def annotate(entries: list[Entry], records: list[JournalRecord]) -> None:
    """Copy source, time and undo counts from the journal record that covers each entry (later records win)."""
    if not records:
        return
    ordered = sorted(enumerate(records), key=lambda r: (r[1].start, r[0]))
    for entry in entries:
        for index, record in ordered:
            if record.start <= entry.offset < record.end:
                entry.source, entry.time, entry.batch = record.source, record.time, index
                entry.undo, entry.redo = record.undo, record.redo
            elif record.start > entry.offset:
                break


def tracking_started(events: list[dict[str, Any]]) -> int | None:
    offsets = [int(e["offset"]) for e in events if e.get("event") == "start" and "offset" in e]
    return min(offsets) if offsets else None


# --- undo / redo ------------------------------------------------------------------------------------------------

def _batches(entries: list[Entry]) -> list[list[Entry]]:
    groups: list[list[Entry]] = []
    for entry in entries:
        if groups and entry.batch is not None and groups[-1][0].batch == entry.batch:
            groups[-1].append(entry)
        else:
            groups.append([entry])
    return groups


def resolve_undo(entries: list[Entry], starts: Iterable[tuple[int, int | None]] = ()) -> None:
    """Mark what each undo/redo reverted, replaying DianaIE's undo stack over the whole history.

    With a journal each batch (one GUI action) is one undo step when DianaIE's undo count rose by one; without
    counts every modifying call counts as a step. ``starts`` are (offset, undo count) of bridge starts: the count a
    journal began with. Opening, creating or closing a project clears the stacks.
    """
    undo_stack: list[list[Entry]] = []
    redo_stack: list[list[Entry]] = []
    previous_undo: int | None = None
    pending_starts = sorted((o, u) for o, u in starts if u is not None)
    for batch in _batches(entries):
        while pending_starts and pending_starts[0][0] <= batch[0].offset:
            previous_undo = pending_starts.pop(0)[1]
        for entry in batch:
            if entry.name in ("newProject", "openProject", "closeProject"):
                undo_stack.clear()
                redo_stack.clear()
        undo_entries = [e for e in batch if e.name in ("undo", "redo")]
        if undo_entries:
            for entry in undo_entries:
                try:
                    count = max(1, int(entry.args[0])) if entry.args else 1
                except (TypeError, ValueError):
                    count = 1
                source, target = (undo_stack, redo_stack) if entry.name == "undo" else (redo_stack, undo_stack)
                for _ in range(count):
                    if not source:
                        entry.reverted.append("（更早的操作，记录开始之前）" if entry.name == "undo" else "（无可重做的操作）")
                        break
                    step = source.pop()
                    for undone in step:
                        undone.undone = entry.name == "undo"
                    entry.reverted.append("；".join(e.description for e in step))
                    target.append(step)
        else:
            modifying = [e for e in batch if e.modifying]
            counts = batch[-1].undo
            if counts is not None and previous_undo is not None:
                delta = counts - previous_undo
                if delta >= 1 and modifying:
                    steps = [modifying] if delta == 1 else (
                        [modifying[: len(modifying) - delta + 1]] + [[e] for e in modifying[len(modifying) - delta + 1:]]
                        if delta <= len(modifying) else [[e] for e in modifying])
                elif delta <= 0 and modifying:
                    steps = [modifying]  # the undo stack is full, or a change DianaIE cannot undo
                else:
                    steps = []
            else:
                steps = [[e] for e in modifying]
            if steps:
                undo_stack.extend(steps)
                redo_stack.clear()
        if batch[-1].undo is not None:
            previous_undo = batch[-1].undo


# --- putting it together ----------------------------------------------------------------------------------------

@dataclass
class History:
    log: HistoryFile
    entries: list[Entry]
    end: int
    tracked_from: int | None  # first offset covered by a bridge journal
    journal: Path | None


def load_history(log: HistoryFile, journal: Path | None = None, schema: Any = None) -> History:
    """Every call of one DianaIE session with sources, descriptions and undo annotations."""
    journal = journal if journal is not None else journal_path(log.key)
    entries, end = read_log(log.path)
    events, records = read_journal(journal if journal.is_file() else None)
    annotate(entries, records)
    for entry in entries:
        entry.description = describe(entry.name, entry.args, entry.text, schema)
    resolve_undo(entries, [(int(e["offset"]), e.get("undo")) for e in events
                           if e.get("event") == "start" and "offset" in e])
    return History(log, entries, end, tracking_started(events), journal if journal.is_file() else None)


def select_entries(entries: list[Entry], since: int = 0, categories: Iterable[str] = DEFAULT_CATEGORIES,
                   sources: Iterable[str] = ("user", "unknown")) -> list[Entry]:
    categories, sources = set(categories), set(sources)
    return [e for e in entries if e.offset >= since and e.category in categories and e.source in sources]


def cursor_path(key: str) -> Path:
    return config.state_dir() / "tracking" / f"{key}.json"


def load_cursor(key: str) -> int | None:
    try:
        return int(json.loads(cursor_path(key).read_text(encoding="utf-8"))["cursor"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_cursor(key: str, offset: int) -> None:
    path = cursor_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cursor": offset, "updated": time.time()}), encoding="utf-8")
    os.replace(tmp, path)
