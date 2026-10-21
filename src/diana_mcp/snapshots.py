"""Model snapshots on disk and the differences between two of them (old -> new values)."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import config
from .changes import Entry, param_title

SNAPSHOT_SCRIPT = Path(__file__).resolve().parent / "bridge" / "diana_snapshot.py"
KEEP_PER_PROJECT = 60

SECTION_ZH = {
    "project": "项目", "units": "单位", "definitions": "模型定义", "directions": "方向", "shapes": "几何体",
    "materials": "材料", "geometries": "几何属性", "element_data": "单元数据", "loads": "荷载", "supports": "支座",
    "tyings": "约束", "boundary_conditions": "边界条件", "fixed_potentials": "固定势", "initial_fields": "初始场",
    "load_cases": "荷载工况", "support_sets": "支座集", "load_combinations": "荷载组合", "node_groups": "节点组",
    "node_group_items": "节点组项", "element_sets": "单元集", "reinforcement_sets": "钢筋集", "functions": "函数",
    "mesh": "网格", "analyses": "分析",
}
ITEM_SECTIONS = (
    "shapes", "materials", "geometries", "element_data", "loads", "supports", "tyings", "boundary_conditions",
    "fixed_potentials", "initial_fields", "element_sets", "reinforcement_sets", "directions", "load_combinations",
    "analyses",
)
NAME_SECTIONS = ("load_cases", "support_sets", "node_groups", "node_group_items", "functions")
VALUE_SECTIONS = ("project", "units", "definitions", "mesh")
FIELD_ZH = {
    "set": "所属集", "element_class": "单元类别", "material": "材料", "geometry": "几何属性", "data": "单元数据",
    "seeding_method": "网格划分方式", "element_size": "网格尺寸", "division": "等分数", "body": "类型",
    "bbox": "包围盒", "size": "尺寸(长/面积/体积)", "vertices": "顶点数", "edges": "边数", "faces": "面数",
    "class": "类别", "model": "模型", "load_case": "荷载工况", "support_set": "支座集", "commands": "命令",
    "phases": "阶段", "nodes": "节点数", "elements": "单元数", "name": "名称", "path": "文件",
    "dimension": "维度", "aspects": "分析类型", "kind": "种类",
}
UNIT_ZH = {"LENGTH": "长度", "MASS": "质量", "FORCE": "力", "TIME": "时间", "TEMPER": "温度", "ANGLE": "角度"}


# --- store ------------------------------------------------------------------------------------------------------

def project_key(path: str | None, name: str | None) -> str:
    """Folder name for a project's snapshots: <name>-<hash of the absolute path>."""
    if path:
        stem = Path(path).stem
        digest = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:8]
        return f"{stem}-{digest}"
    return f"{name or 'untitled'}-unsaved"


@dataclass
class SnapshotInfo:
    id: str
    project_key: str
    file: Path
    label: str | None
    taken: float
    project: str | None
    project_path: str | None
    origin: str  # "live" (the open DianaIE project) or "file" (a saved .dpf read by a batch DianaIE)

    def describe(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "taken": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.taken)),
                "project": self.project, "project_path": self.project_path, "origin": self.origin}


class SnapshotStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or config.state_dir() / "snapshots"

    def save(self, snapshot: dict[str, Any], label: str | None = None, origin: str = "live") -> SnapshotInfo:
        project = snapshot.get("project") or {}
        key = project_key(project.get("path"), project.get("name"))
        taken = float(snapshot.get("taken") or time.time())
        snap_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(taken)) + "-" + uuid.uuid4().hex[:4]
        folder = self.directory / key
        folder.mkdir(parents=True, exist_ok=True)
        info = SnapshotInfo(snap_id, key, folder / f"{snap_id}.json", label, taken, project.get("name"),
                            project.get("path"), origin)
        meta = {k: v for k, v in info.describe().items()} | {"taken_epoch": taken, "project_key": key}
        tmp = info.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"meta": meta, "snapshot": snapshot}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, info.file)
        self._prune(folder)
        return info

    def _prune(self, folder: Path) -> None:
        unlabelled = [i for i in self._infos(folder) if not i.label]
        for info in unlabelled[KEEP_PER_PROJECT:]:
            info.file.unlink(missing_ok=True)

    def _infos(self, folder: Path) -> list[SnapshotInfo]:
        infos = []
        for path in folder.glob("*.json"):
            try:
                with open(path, encoding="utf-8") as f:
                    meta = json.load(f)["meta"]
                infos.append(SnapshotInfo(meta["id"], meta["project_key"], path, meta.get("label"),
                                          float(meta["taken_epoch"]), meta.get("project"), meta.get("project_path"),
                                          meta.get("origin", "live")))
            except (OSError, ValueError, KeyError):
                continue
        return sorted(infos, key=lambda i: i.taken, reverse=True)

    def list(self, key: str | None = None) -> list[SnapshotInfo]:
        folders = [self.directory / key] if key else [p for p in self.directory.glob("*") if p.is_dir()]
        infos = [i for folder in folders if folder.is_dir() for i in self._infos(folder)]
        return sorted(infos, key=lambda i: i.taken, reverse=True)

    def latest(self, key: str) -> SnapshotInfo | None:
        infos = self.list(key)
        return infos[0] if infos else None

    def find(self, ref: str, key: str | None = None) -> SnapshotInfo | None:
        """A snapshot by id, or the newest one with this label (in the given project first)."""
        candidates = self.list(key) + (self.list() if key else [])
        for info in candidates:
            if info.id == ref:
                return info
        for info in candidates:
            if info.label == ref:
                return info
        return None

    @staticmethod
    def load(info: SnapshotInfo) -> dict[str, Any]:
        with open(info.file, encoding="utf-8") as f:
            return json.load(f)["snapshot"]


# --- taking snapshots -------------------------------------------------------------------------------------------

def live_snapshot_code(out_path: Path) -> str:
    """Code for diana_execute / the bridge: snapshot the open project into ``out_path``."""
    return (
        "import runpy as _runpy\n"
        f"_runpy.run_path({config.to_windows(SNAPSHOT_SCRIPT)!r}, run_name='diana_mcp_snapshot')"
        f"['take'](globals(), {config.to_windows(out_path)!r})\n"
        "del _runpy\n"
    )


def snapshot_saved_project(install: config.DianaInstall, dpf: Path, timeout: float = 600) -> dict[str, Any]:
    """Snapshot a saved .dpf with a short-lived batch DianaIE, without touching the live session."""
    from .verify import run_batch_dianaie

    if not dpf.is_file():
        raise FileNotFoundError(f"project not found: {dpf}")
    with tempfile.TemporaryDirectory(prefix="diana-mcp-", ignore_cleanup_errors=True) as tmp:
        out = Path(tmp) / "snapshot.json"
        script = Path(tmp) / "snapshot_project.py"
        script.write_text(
            "import runpy\n"
            f"if not openProject({config.to_windows(dpf)!r}):\n"
            f"    raise RuntimeError('openProject failed')\n"
            + live_snapshot_code(out)
            + "closeProject()\n",
            encoding="utf-8",
        )
        run_batch_dianaie(install, script, timeout)
        if not out.is_file():
            raise RuntimeError(f"DianaIE could not snapshot {dpf} (is the file valid and a license available?)")
        snapshot = json.loads(out.read_text(encoding="utf-8"))
    snapshot.setdefault("project", {})["path"] = dpf.resolve().as_posix()
    return snapshot


# --- diff -------------------------------------------------------------------------------------------------------

@dataclass
class Change:
    section: str
    item: str | None
    kind: str  # added | removed | modified
    key: str | None = None
    old: Any = None
    new: Any = None
    lines: list[str] = field(default_factory=list)
    summary: str = ""
    by: str | None = None

    def text(self) -> str:
        where = SECTION_ZH.get(self.section, self.section)
        item = f" “{self.item}”" if self.item is not None else ""
        if self.kind == "added":
            return f"新增{where}{item}" + (f"：{self.summary}" if self.summary else "")
        if self.kind == "removed":
            return f"删除{where}{item}"
        if self.lines:
            return f"{where}{item} 的命令有改动：\n" + "\n".join("    " + line for line in self.lines)
        title = key_title(self.section, self.key)
        key = f"{self.key}" + (f"（{title}）" if title else "") if self.key else ""
        return f"{where}{item}：{key} {show(self.old)} → {show(self.new)}"

    def view(self) -> dict[str, Any]:
        out = {"text": self.text(), "section": self.section, "item": self.item, "kind": self.kind}
        if self.key:
            out.update(key=self.key, old=self.old, new=self.new)
        if self.by:
            out["by"] = self.by
        return out


def key_title(section: str, key: str | None) -> str:
    if not key:
        return ""
    if section == "units":
        return UNIT_ZH.get(key, "")
    if key in FIELD_ZH:
        return FIELD_ZH[key]
    return param_title(key)


def show(value: Any) -> str:
    if value is None:
        return "（无）"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, list):
        text = "[" + ", ".join(show(v) for v in value) + "]"
        return text if len(text) < 120 else text[:117] + "…]"
    return str(value)


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b))
    return a == b


def _flatten(item: Any, prefix: str = "") -> dict[str, Any]:
    """Leaf values of an item: parameters by their path, other fields by name; readable copies are skipped."""
    out: dict[str, Any] = {}
    if not isinstance(item, dict):
        return {prefix or "value": item}
    for key, value in item.items():
        if key in ("readable", "dcf"):
            continue
        if key == "parameters" and isinstance(value, dict):
            out.update(value)
        elif isinstance(value, dict):
            out.update(_flatten(value, f"{prefix}{key}/"))
        else:
            out[prefix + key] = value
    return out


def _summary(section: str, item: Any) -> str:
    """One line describing a new item."""
    if not isinstance(item, dict):
        return show(item)
    if section in ("loads", "supports") and item.get("readable"):
        parts = [f"{k} {v}" for k, v in item["readable"].items() if not str(k).lower().endswith(" name")]
        extra = item.get("load_case") or item.get("support_set")
        return "，".join(parts) + (f"（{extra}）" if extra else "")
    if section in ("materials", "geometries") and (item.get("class") or item.get("model")):
        return " / ".join(str(v) for v in (item.get("class"), item.get("model")) if v)
    if section == "shapes":
        bits = [str(item[k]) for k in ("body", "element_class") if item.get(k)]
        bits += [f"{FIELD_ZH[k]} {item[k]}" for k in ("material", "geometry") if item.get(k)]
        if item.get("size") is not None:
            bits.append(f"尺寸 {show(item['size'])}")
        return "，".join(bits)
    if section == "analyses":
        return "命令 " + "、".join(item.get("commands") or [])
    fields = _flatten(item)
    shown = [f"{k} = {show(v)}" for k, v in list(fields.items())[:6]]
    return "，".join(shown)


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[Change]:
    """What changed from one snapshot to the next, section by section."""
    changes: list[Change] = []
    for section in VALUE_SECTIONS:
        old, new = before.get(section) or {}, after.get(section) or {}
        for key in list(old) + [k for k in new if k not in old]:
            if key == "stored_path":
                continue
            if not _equal(old.get(key), new.get(key)):
                changes.append(Change(section, None, "modified", key, old.get(key), new.get(key)))
    for section in ITEM_SECTIONS:
        old, new = before.get(section) or {}, after.get(section) or {}
        for name in new:
            if name not in old:
                changes.append(Change(section, name, "added", summary=_summary(section, new[name])))
        for name in old:
            if name not in new:
                changes.append(Change(section, name, "removed"))
        for name in old:
            if name not in new:
                continue
            a, b = _flatten(old[name]), _flatten(new[name])
            for key in list(a) + [k for k in b if k not in a]:
                if not _equal(a.get(key), b.get(key)):
                    changes.append(Change(section, name, "modified", key, a.get(key), b.get(key)))
            if section == "analyses":
                lines = [l for l in difflib.unified_diff(old[name].get("dcf") or [], new[name].get("dcf") or [],
                                                         lineterm="", n=0)
                         if l[:1] in "+-" and not l.startswith(("+++", "---"))]
                if lines:
                    changes.append(Change(section, name, "modified", lines=lines[:60]))
    for section in NAME_SECTIONS:
        old, new = list(before.get(section) or []), list(after.get(section) or [])
        changes += [Change(section, str(n), "added") for n in new if n not in old]
        changes += [Change(section, str(n), "removed") for n in old if n not in new]
    return changes


def _mentions(value: Any, name: str) -> bool:
    if isinstance(value, str):
        return value == name
    if isinstance(value, (list, tuple)):
        return any(_mentions(v, name) for v in value)
    if isinstance(value, dict):
        return any(_mentions(v, name) for v in value.values())
    return False


SECTION_COMMANDS = {
    "mesh": ("generateMesh", "clearMesh"),
    "units": ("setUnit", "setUnitSet", "resetUnits"),
    "project": ("setModelDimension", "setModelAnalysisAspects", "saveProjectAs", "newProject", "openProject"),
    "definitions": ("setModelDefinition",),
}


def attribute(changes: list[Change], entries: Iterable[Entry]) -> None:
    """Guess who made each change from the calls in the same period: a call naming the item, or a call that can
    only have caused it (generateMesh for the mesh, setUnit for units)."""
    entries = [e for e in entries if e.name and not e.undone and e.source in ("user", "ai")]
    for change in changes:
        if change.section in SECTION_COMMANDS:
            related = [e for e in entries if e.name in SECTION_COMMANDS[change.section]]
        elif change.item is not None:
            related = [e for e in entries if e.modifying and (_mentions(e.args, change.item) or _mentions(e.kwargs, change.item))]
        else:
            related = []
        sources = {e.source for e in related}
        if sources:
            change.by = "+".join(s for s in ("user", "ai") if s in sources)
