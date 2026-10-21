"""The experience handbook: one Markdown file per lesson learned while modeling with DIANA, in a git repository.

Each entry has a YAML front matter block (see frontmatter.py) and a body with the sections 现象 / 原因 / 解决 / 验证.
Everything the AI writes starts as a draft ("未验证"); only the user can make an entry verified ("已验证"), and
the approval, with the user's words, is recorded in the file and in the git history.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import config, frontmatter
from .textindex import BM25

TYPES = {
    "error-fix": "错误修复", "modeling-pitfall": "建模陷阱", "api-usage": "API 用法", "convergence": "收敛策略",
    "parameter": "参数取值", "result-interpretation": "结果判读", "workflow": "工作流程",
}
STATUSES = {"draft": "未验证", "verified": "已验证", "disputed": "存疑", "obsolete": "过时"}
SOURCES = {"migrated": "开发中实测后迁入", "ai": "AI 起草", "user": "用户提供", "history-log": "历史日志"}
ALIASES = {
    "草稿": "draft", "未验证": "draft", "已验证": "verified", "验证": "verified", "存疑": "disputed", "过时": "obsolete",
    "错误修复": "error-fix", "建模陷阱": "modeling-pitfall", "api用法": "api-usage", "api 用法": "api-usage",
    "收敛策略": "convergence", "参数取值": "parameter", "结果判读": "result-interpretation", "工作流程": "workflow",
    "迁入": "migrated", "ai 起草": "ai", "用户提供": "user", "历史日志": "history-log",
}
SECTIONS = ("现象", "原因", "解决", "验证", "备注")
LIST_FIELDS = ("triggers", "tags", "manual", "evidence", "related")
FIELD_ORDER = ("id", "title", "type", "status", "diana_version", "triggers", "tags", "manual", "evidence", "related",
               "source", "created", "updated", "approval", "dispute", "checks")
MAPPING_FIELDS = ("approval", "dispute", "checks")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")

README = """\
# DIANA 经验手册

diana-mcp 的经验库：日常用 DIANA 建模时发现的问题、原因和解决办法，一条经验一个 Markdown 文件（`experiences/`）。
AI 通过 `diana_knowledge_search` 与手册一起检索，也用 `diana_experience_*` 工具新增和维护。

## 条目格式

文件开头是 YAML 头（两行 `---` 之间），然后是正文的四个小节：`## 现象`、`## 原因`、`## 解决`、`## 验证`
（可选 `## 备注`）。YAML 头的字段：

| 字段 | 含义 |
|---|---|
| `id` | 与文件名相同，小写字母、数字和连字符 |
| `title` | 一句话概括 |
| `type` | error-fix 错误修复 / modeling-pitfall 建模陷阱 / api-usage API 用法 / convergence 收敛策略 / parameter 参数取值 / result-interpretation 结果判读 / workflow 工作流程 |
| `status` | draft 未验证 / verified 已验证 / disputed 存疑 / obsolete 过时（也可以直接写中文） |
| `diana_version` | 适用或验证过的 DIANA 版本 |
| `triggers` | 遇到时应想起这条经验的错误码、报错文字或命令名；`re:` 开头表示正则表达式 |
| `tags` | 标签，如 材料、界面单元、非线性 |
| `manual` | 相关的手册页，如 `Theory/Theoryse738.html` |
| `evidence` | 证据：算例、作业、测试、日期 |
| `related` | 相关经验的 id |
| `source` | migrated 开发中实测后迁入 / ai AI 起草 / user 用户提供 / history-log 历史日志 |
| `created` / `updated` | 日期 |
| `approval` | 批准记录：日期、批准人、用户原话（只在用户确认后由工具写入） |
| `dispute` | 标记存疑或过时的日期和原因 |

## 经验从哪里来

- **开发实测**（`source: migrated`）：diana-mcp 开发过程中在真实 DIANA 上验证过的做法和坑。
- **历史日志**（`source: history-log`，文件名以 `msg-` 开头）：从旧的 .out 文件按 DIANA 消息（错误码）归类生成，
  含出现次数、示例文件、DIANA 原文中的说明与建议；数字与模型命名已脱敏。命令：
  `python -m diana_mcp knowledge mine-outs <文件夹>`，或让 AI 调用 `diana_experience_mine_logs`。
- **自动起草**（`source: ai`，标签“自动起草”）：
  - 同一算例的求解先失败、后成功时，比较两次的 .dcf 与 .dat（不含节点坐标和单元连接），把差异写成
    “问题 → 改动 → 结果”；
  - 脚本命令报错后换了参数调用成功时，记下错误写法与正确写法。
- **AI 或用户手动记录**：`diana_experience_add`。

## 规则

- AI 写入的经验一律是**未验证**的草稿；只有用户确认后，才由工具改为**已验证**，并记下用户原话。
- 求解或脚本出问题时，工具结果里会自动附上相关经验（按错误码和报错文字匹配）；问题解决后，
  帮上忙的草稿会被列为“待批准”，由 AI 带着新证据提请用户审批。
- 历史日志（旧的 .out 文件）生成的条目同样是草稿；建模时再遇到同类问题，AI 会带着新证据提请批准。
- 发现经验不对时标为**存疑**，不再适用时标为**过时**，不直接删除，便于追溯。
- 每次通过工具修改都会自动提交一次 git；手工编辑后可运行 `python -m diana_mcp knowledge commit -m "说明"`，
  或用 `python -m diana_mcp knowledge validate` 先检查格式。
"""

GITIGNORE = "*.tmp\n.obsidian/\n.vscode/\nexport/\n"


class ExperienceError(ValueError):
    pass


def today() -> str:
    return dt.date.today().isoformat()


def normalize(value: Any, allowed: dict[str, str], field_name: str) -> str:
    text = str(value or "").strip()
    key = ALIASES.get(text.lower(), ALIASES.get(text, text.lower()))
    if key not in allowed:
        raise ExperienceError(f"{field_name} must be one of {', '.join(allowed)} (or {', '.join(allowed.values())}), got {text!r}")
    return key


def parse_sections(body: str) -> dict[str, str]:
    """{heading: text} of the '## ' sections of a body."""
    sections: dict[str, str] = {}
    current, lines = None, []
    for line in body.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current, lines = match.group(1), []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


def render_body(sections: dict[str, str]) -> str:
    parts = []
    for name in SECTIONS:
        text = (sections.get(name) or "").strip()
        if text or name in SECTIONS[:4]:
            parts.append(f"## {name}\n\n{text or '（待补充）'}")
    for name, text in sections.items():
        if name not in SECTIONS and text.strip():
            parts.append(f"## {name}\n\n{text.strip()}")
    return "\n\n".join(parts)


@dataclass
class Entry:
    id: str
    title: str
    type: str
    status: str = "draft"
    diana_version: str | None = None
    triggers: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    source: str = "ai"
    created: str | None = None
    updated: str | None = None
    approval: dict[str, Any] | None = None
    dispute: dict[str, Any] | None = None
    checks: dict[str, Any] | None = None  # last automatic verification: date, diana_version, result, detail
    body: str = ""
    path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_text(cls, text: str, path: Path | None = None) -> Entry:
        data, body = frontmatter.read(text)
        missing = [k for k in ("id", "title", "type") if not data.get(k)]
        if missing:
            raise ExperienceError(f"missing {', '.join(missing)}")
        values: dict[str, Any] = {}
        for key in FIELD_ORDER:
            if key in data:
                values[key] = data.pop(key)
        for key in LIST_FIELDS:
            value = values.get(key)
            values[key] = [] if value is None else [str(v) for v in (value if isinstance(value, list) else [value])]
        values["type"] = normalize(values["type"], TYPES, "type")
        values["status"] = normalize(values.get("status") or "draft", STATUSES, "status")
        values["source"] = normalize(values.get("source") or "ai", SOURCES, "source")
        for key in ("id", "title", "diana_version", "created", "updated"):
            if values.get(key) is not None:
                values[key] = str(values[key])
        for key in MAPPING_FIELDS:
            if values.get(key) is not None and not isinstance(values[key], dict):
                values[key] = {"note": str(values[key])}
        entry = cls(**values, body=body, path=path, extra=data)
        if not _ID.match(entry.id):
            raise ExperienceError(f"id {entry.id!r} must be 3-80 lowercase letters, digits or hyphens")
        if path is not None and path.stem != entry.id:
            raise ExperienceError(f"id {entry.id!r} does not match the file name {path.name}")
        return entry

    def front_matter(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for key in FIELD_ORDER:
            value = getattr(self, key)
            if value is None and key in MAPPING_FIELDS + ("diana_version",):
                continue
            data[key] = value
        data.update(self.extra)
        return data

    def to_text(self) -> str:
        return frontmatter.write(self.front_matter(), self.body)

    @property
    def sections(self) -> dict[str, str]:
        return parse_sections(self.body)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "type": TYPES[self.type], "status": STATUSES[self.status],
            "tags": self.tags, "triggers": self.triggers, "updated": self.updated, "source": SOURCES[self.source],
        }

    def view(self) -> dict[str, Any]:
        out = self.summary() | {
            "diana_version": self.diana_version, "manual": self.manual, "evidence": self.evidence,
            "related": self.related, "created": self.created, "file": str(self.path) if self.path else None,
            "sections": self.sections,
        }
        for key in MAPPING_FIELDS:
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out

    @property
    def script(self) -> str | None:
        """The Python of the '## 验证脚本' section (run inside DianaIE by diana_experience_verify)."""
        section = self.sections.get("验证脚本") or ""
        match = re.search(r"```(?:python|py)?\s*\n(.*?)```", section, re.DOTALL)
        return match.group(1).strip() if match else None

    def search_fields(self) -> list[tuple[str, float]]:
        return [(self.title, 3.0), (" ".join(self.triggers), 3.0), (" ".join(self.tags), 2.0), (self.body, 1.0),
                (self.id.replace("-", " "), 1.0)]

    def matched_triggers(self, text: str) -> list[str]:
        """Triggers found in ``text`` (case-insensitive substrings; 're:' prefix for a regular expression)."""
        found = []
        lowered = text.lower()
        for trigger in self.triggers:
            if trigger.startswith("re:"):
                try:
                    if re.search(trigger[3:], text, re.IGNORECASE):
                        found.append(trigger)
                except re.error:
                    continue
            elif trigger and trigger.lower() in lowered:
                found.append(trigger)
        return found


def slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:6])


class ExperienceStore:
    def __init__(self, root: Path | None = None, use_git: bool = True):
        self.root = Path(root) if root else config.knowledge_dir()
        self.use_git = use_git
        self._cache: dict[Path, tuple[tuple[int, int], Entry | ExperienceError]] = {}
        self._batch: list[Path] | None = None
        self.last_batch_commit: dict[str, Any] | None = None

    @contextmanager
    def batch(self, message: str):
        """Group many changes into one commit (e.g. seeding from old logs) instead of one commit each."""
        outer = self._batch is not None
        if not outer:
            self._batch = []
        try:
            yield
        finally:
            if not outer:
                paths, self._batch = self._batch, None
                self.last_batch_commit = self.commit(paths, message) if paths else {"committed": False,
                                                                                     "reason": "没有变化"}

    @property
    def entries_dir(self) -> Path:
        return self.root / "experiences"

    # --- repository -----------------------------------------------------------------------------------------

    def ensure(self) -> None:
        """Create the folder layout, README and git repository if they are missing."""
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        created = []
        for name, text in (("README.md", README), (".gitignore", GITIGNORE)):
            path = self.root / name
            if not path.exists():
                path.write_text(text, encoding="utf-8")
                created.append(path)
        if self.git_available() and not (self.root / ".git").exists():
            self._git("init", "-q")
            if created:
                batch, self._batch = self._batch, None  # the repository's first commit stands on its own
                try:
                    self.commit(created, "knowledge: 初始化经验手册仓库")
                finally:
                    self._batch = batch

    def git_available(self) -> bool:
        return self.use_git and shutil.which("git") is not None

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=False)

    def commit(self, paths: Iterable[Path], message: str) -> dict[str, Any]:
        """Commit exactly these files (other uncommitted edits stay as they are); deferred inside batch()."""
        if self._batch is not None:
            self._batch.extend(p for p in paths if p not in self._batch)
            return {"committed": False, "reason": "批量模式：结束时统一提交"}
        if not self.git_available():
            return {"committed": False, "reason": "git 不可用，文件已保存但未提交"}
        if not (self.root / ".git").exists():
            self._git("init", "-q")
        relative = [str(Path(p).resolve().relative_to(self.root.resolve())).replace("\\", "/") for p in paths]
        add = self._git("add", "--", *relative)
        if add.returncode != 0:
            return {"committed": False, "reason": add.stderr.strip()}
        if not self._git("diff", "--cached", "--quiet", "--", *relative).returncode:
            return {"committed": False, "reason": "没有变化"}
        done = self._git("commit", "-q", "-m", message, "--only", "--", *relative)
        if done.returncode != 0:
            return {"committed": False, "reason": (done.stderr or done.stdout).strip()}
        head = self._git("rev-parse", "--short", "HEAD").stdout.strip()
        return {"committed": True, "commit": head}

    def uncommitted(self) -> list[str]:
        if not self.git_available() or not (self.root / ".git").exists():
            return []
        status = self._git("status", "--porcelain")
        return [line[3:] for line in status.stdout.splitlines() if line.strip()]

    # --- reading --------------------------------------------------------------------------------------------

    def _read(self, path: Path) -> Entry | ExperienceError:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        try:
            value: Entry | ExperienceError = Entry.from_text(path.read_text(encoding="utf-8"), path)
        except (ExperienceError, frontmatter.FrontMatterError, ValueError) as exc:
            value = ExperienceError(f"{path.name}: {exc}")
        self._cache[path] = (key, value)
        return value

    def load(self) -> tuple[list[Entry], list[str]]:
        """All valid entries (newest first) and the problems of the invalid files."""
        entries, problems = [], []
        if self.entries_dir.is_dir():
            for path in sorted(self.entries_dir.glob("*.md")):
                value = self._read(path)
                if isinstance(value, Entry):
                    entries.append(value)
                else:
                    problems.append(str(value))
        entries.sort(key=lambda e: (e.updated or "", e.id), reverse=True)
        return entries, problems

    def entries(self) -> list[Entry]:
        return self.load()[0]

    def get(self, entry_id: str) -> Entry:
        path = self.entries_dir / f"{entry_id}.md"
        if not path.is_file():
            raise KeyError(entry_id)
        value = self._read(path)
        if isinstance(value, ExperienceError):
            raise value
        return value

    def validate(self) -> list[str]:
        entries, problems = self.load()
        ids = {e.id for e in entries}
        for entry in entries:
            for other in entry.related:
                if other not in ids:
                    problems.append(f"{entry.id}.md: related entry {other!r} does not exist")
            if entry.status == "verified" and not entry.approval:
                problems.append(f"{entry.id}.md: verified without an approval record")
        return problems

    def index(self, entries: list[Entry] | None = None) -> tuple[BM25, dict[str, Entry]]:
        entries = entries if entries is not None else self.entries()
        by_id = {e.id: e for e in entries}
        return BM25({e.id: e.search_fields() for e in entries}), by_id

    def likely_duplicates(self, title: str, triggers: Iterable[str] = (), exclude: str | None = None) -> list[Entry]:
        """Entries with a similar title (character-bigram overlap >= 0.5) or a shared specific trigger."""
        def grams(text: str) -> set[str]:
            text = re.sub(r"\s+", "", text.lower())
            return {text[i:i + 2] for i in range(len(text) - 1)}

        mine = grams(title)
        wanted = {t.lower() for t in triggers if len(t) >= 4}
        found = []
        for entry in self.entries():
            if entry.id == exclude:
                continue
            theirs = grams(entry.title)
            overlap = len(mine & theirs) / max(1, len(mine | theirs))
            if overlap >= 0.5 or wanted & {t.lower() for t in entry.triggers}:
                found.append(entry)
        return found

    def recommend(self, texts: Iterable[str], limit: int = 3,
                  boost: dict[str, float] | None = None) -> list[dict[str, Any]]:
        """Entries for what a tool just reported (error codes, messages, tracebacks): first those whose triggers
        occur in the texts, specific ones (codes, paths) before plain words; then close matches by content.
        ``boost`` multiplies scores per entry id (usage statistics)."""
        haystack = "\n".join(t for t in texts if t)
        if not haystack.strip():
            return []
        entries = [e for e in self.entries() if e.status != "obsolete"]
        scored: dict[str, tuple[float, list[str]]] = {}
        for entry in entries:
            matched = entry.matched_triggers(haystack)
            if matched:
                specific = sum(1 for t in matched if re.search(r"[\d/]", t))
                scored[entry.id] = (10.0 + 10 * specific + len(matched), matched)
        if len(scored) < limit:
            bm25, _ = self.index(entries)
            hits = bm25.search(haystack[:4000], limit * 2)
            top = hits[0][1] if hits else 0.0
            for entry_id, score in hits:
                if entry_id not in scored and score >= 0.6 * top:
                    scored[entry_id] = (score / (top or 1.0), [])
        by_id = {e.id: e for e in entries}
        if boost:
            scored = {i: (s * boost.get(i, 1.0), m) for i, (s, m) in scored.items()}
        ranked = sorted(scored.items(), key=lambda kv: (-kv[1][0], by_id[kv[0]].status != "verified"))[:limit]
        return [{"id": i, "title": by_id[i].title, "status": STATUSES[by_id[i].status], "type": TYPES[by_id[i].type],
                 **({"matched": m} if m else {})} for i, (_, m) in ranked]

    def similar(self, text: str, limit: int = 3, exclude: str | None = None) -> list[tuple[Entry, float]]:
        bm25, by_id = self.index()
        hits = [(by_id[i], s) for i, s in bm25.search(text, limit + 1) if i != exclude]
        if not hits:
            return []
        top = hits[0][1]
        return [(e, round(s, 2)) for e, s in hits[:limit] if s >= 0.35 * top]

    # --- writing --------------------------------------------------------------------------------------------

    def new_id(self, title: str, kind: str) -> str:
        base = slug(title)
        if len(base.split("-")) < 2:
            base = f"{kind}-{dt.date.today():%Y%m%d}"
        candidate, n = base, 1
        while (self.entries_dir / f"{candidate}.md").exists():
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    def _save(self, entry: Entry) -> Path:
        self.ensure()
        path = self.entries_dir / f"{entry.id}.md"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(entry.to_text(), encoding="utf-8")
        Entry.from_text(tmp.read_text(encoding="utf-8"), path)  # never write a file that would not load again
        os.replace(tmp, path)
        entry.path = path
        return path

    def add(self, *, title: str, kind: str, symptom: str, cause: str = "", fix: str = "", verification: str = "",
            notes: str = "", triggers: Iterable[str] = (), tags: Iterable[str] = (), manual: Iterable[str] = (),
            evidence: Iterable[str] = (), related: Iterable[str] = (), diana_version: str | None = None,
            source: str = "ai", entry_id: str | None = None) -> tuple[Entry, dict[str, Any]]:
        """A new draft entry, committed to git."""
        kind = normalize(kind, TYPES, "type")
        source = normalize(source, SOURCES, "source")
        if not title.strip() or not symptom.strip():
            raise ExperienceError("title and symptom are required")
        self.ensure()
        entry_id = entry_id or self.new_id(title, kind)
        if not _ID.match(entry_id):
            raise ExperienceError(f"id {entry_id!r} must be 3-80 lowercase letters, digits or hyphens")
        if (self.entries_dir / f"{entry_id}.md").exists():
            raise ExperienceError(f"an entry with id {entry_id!r} already exists; update it instead")
        entry = Entry(
            id=entry_id, title=title.strip(), type=kind, status="draft", diana_version=diana_version,
            triggers=list(triggers), tags=list(tags), manual=list(manual), evidence=list(evidence),
            related=list(related), source=source, created=today(), updated=today(),
            body=render_body({"现象": symptom, "原因": cause, "解决": fix, "验证": verification, "备注": notes}),
        )
        path = self._save(entry)
        commit = self.commit([path], f"knowledge: 新增草稿 {entry.id}\n\n{entry.title}\n来源：{SOURCES[source]}")
        return entry, commit

    def update(self, entry_id: str, *, title: str | None = None, kind: str | None = None,
               sections: dict[str, str] | None = None, triggers: Iterable[str] | None = None,
               tags: Iterable[str] | None = None, manual: Iterable[str] | None = None,
               related: Iterable[str] | None = None, add_evidence: Iterable[str] = (),
               evidence: Iterable[str] | None = None, diana_version: str | None = None,
               note: str | None = None) -> tuple[Entry, dict[str, Any]]:
        """Edit an entry's content (its status is changed with approve() / set_status()); ``evidence`` replaces the
        list, ``add_evidence`` appends to it."""
        entry = self.get(entry_id)
        changed = []
        if evidence is not None and list(evidence) != entry.evidence:
            entry.evidence = list(evidence)
            changed.append("evidence")
        if title:
            entry.title = title.strip()
            changed.append("title")
        if kind:
            entry.type = normalize(kind, TYPES, "type")
            changed.append("type")
        for name, value in (("triggers", triggers), ("tags", tags), ("manual", manual), ("related", related)):
            if value is not None:
                setattr(entry, name, list(value))
                changed.append(name)
        new_evidence = [e for e in add_evidence if e and e not in entry.evidence]
        if new_evidence:
            entry.evidence += new_evidence
            changed.append("evidence")
        if diana_version:
            entry.diana_version = diana_version
            changed.append("diana_version")
        if sections:
            current = entry.sections
            current.update({k: v for k, v in sections.items() if v is not None})
            entry.body = render_body(current)
            changed.append("sections")
        if not changed:
            raise ExperienceError("nothing to change")
        entry.updated = today()
        path = self._save(entry)
        message = f"knowledge: 更新 {entry.id}（{', '.join(changed)}）\n\n{entry.title}" + (f"\n{note}" if note else "")
        return entry, self.commit([path], message)

    def record_check(self, entry_id: str, check: dict[str, Any]) -> tuple[Entry, dict[str, Any]]:
        """Store the result of an automatic verification run (see verify.py)."""
        entry = self.get(entry_id)
        entry.checks = check
        entry.updated = today()
        path = self._save(entry)
        outcome = "通过" if check.get("result") == "pass" else "失败"
        return entry, self.commit([path], f"knowledge: 自动验证{outcome} {entry.id}（DIANA {check.get('diana_version')}）")

    def approve(self, entry_id: str, approval: str, by: str = "用户") -> tuple[Entry, dict[str, Any]]:
        """Mark an entry verified; ``approval`` must quote the user's confirmation."""
        if not approval or not approval.strip():
            raise ExperienceError("approval must quote the user's confirmation")
        entry = self.get(entry_id)
        entry.status = "verified"
        entry.approval = {"date": today(), "by": by, "note": approval.strip()}
        entry.dispute = None
        entry.updated = today()
        path = self._save(entry)
        message = f"knowledge: 批准为已验证 {entry.id}\n\n{entry.title}\n批准人：{by}\n原话：{approval.strip()}"
        return entry, self.commit([path], message)

    def set_status(self, entry_id: str, status: str, reason: str) -> tuple[Entry, dict[str, Any]]:
        """Mark an entry disputed, obsolete, or back to draft (verified goes through approve())."""
        status = normalize(status, STATUSES, "status")
        if status == "verified":
            raise ExperienceError("use approve() with the user's confirmation to verify an entry")
        if not reason or not reason.strip():
            raise ExperienceError("a reason is required")
        entry = self.get(entry_id)
        entry.status = status
        entry.dispute = {"date": today(), "note": reason.strip()} if status in ("disputed", "obsolete") else None
        if status == "draft":
            entry.approval = None
        entry.updated = today()
        path = self._save(entry)
        message = f"knowledge: 标记为{STATUSES[status]} {entry.id}\n\n{entry.title}\n原因：{reason.strip()}"
        return entry, self.commit([path], message)
