"""Seed the experience handbook from old solver logs: one draft per kind of DIANA message.

Every .out file under the given folders is parsed; messages are grouped by error code (or, without a code, by
their text), counted, and turned into draft entries (source "历史日志", status 未验证) that quote DIANA's own
explanation and advice. Numbers in the quoted text are replaced by <n>/<x>, so no model data is copied. Running it
again updates the statistics of existing entries instead of duplicating them. Whether an entry is right is decided
later: when the same message comes up in real work, the AI proposes it for approval with the new evidence.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import re
import shlex
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .experience import ExperienceError, ExperienceStore, render_body, today
from .out_parser import parse_out

MAX_FILE_BYTES = 200 * 1024 * 1024
_ADVICE = re.compile(r"\b(check|use|try|increase|decrease|consider|delete|input a|consult|correct|specify|restart|"
                     r"avoid|reduce|remove|define|change)\b", re.IGNORECASE)
_EXPLANATION = re.compile(r"\b(may be caused|is likely|because|due to|results may be|will be used|is changed to|"
                          r"is skipped|ignored|is overwritten)\b", re.IGNORECASE)
MODULE_TYPES = {"NL": "convergence", "SO": "convergence", "PO": "workflow", "FL": "workflow"}
SEVERE = {"FATAL", "ABORT", "ERROR", "SYNTAX"}


_QUOTED = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"")
_KEYWORD = re.compile(r"^[A-Z][A-Z0-9]{0,7}$")  # DIANA keywords (POISON, TENSTR, ELEMEN); not the user's names


def normalize(text: str, keep_keywords: bool = True) -> str:
    """Message text without model data: quoted names -> <q> (DIANA keywords kept), numbers -> <x>/<n>,
    file names -> <file>, collapsed spaces."""
    def quoted(match: re.Match) -> str:
        value = match.group(1) if match.group(1) is not None else match.group(2)
        return f"'{value}'" if keep_keywords and _KEYWORD.match(value) else "<q>"

    text = _QUOTED.sub(quoted, text)
    text = re.sub(r"\S+\.(?:dat|dcf|ff|out)\b", "<file>", text)
    text = re.sub(r"[-+]?\d*\.\d+(?:[EeDd][-+]?\d+)?|[-+]?\d+[EeDd][-+]?\d+", "<x>", text)
    text = re.sub(r"\b\d+\b", "<n>", text)
    return re.sub(r"\s+", " ", text).strip()


def quoted_keyword(text: str) -> str:
    for match in _QUOTED.finditer(text):
        value = match.group(1) if match.group(1) is not None else match.group(2)
        if _KEYWORD.match(value):
            return value
    return ""


def message_key(severity: str, code: str, text: str) -> tuple[str, str] | None:
    """(group key, detail) of a message; None for companions that add nothing ('Last line was').

    Messages with a code group by code; without one, by their first words with quoted values blanked, so
    "Integer expected, got 'KAPCOH'" and "... got 'ELEMEN'" are one kind with the keywords as details.
    """
    if code:
        return code, quoted_keyword(text)
    if severity == "SYNTAX":
        unexpected = re.match(r"Unexpected input\s+(\S+)", text, re.IGNORECASE)
        if unexpected:
            return "SYNTAX:Unexpected input", unexpected.group(1).upper()
        if text.lower().startswith("last line was"):
            return None
        return "SYNTAX:" + " ".join(normalize(text, keep_keywords=False).split()[:3]), ""
    words = normalize(text, keep_keywords=False).split()
    return (f"{severity}:" + " ".join(words[:4])) if words else None, quoted_keyword(text)


def entry_id(key: str) -> str:
    words = re.findall(r"[a-z0-9]+", re.sub(r"<[nxq]>|<file>", " ", key.lower()))
    return "msg-" + "-".join(words)[:70].strip("-")


def module_of(code: str) -> str:
    parts = code.strip("/").split("/")
    return parts[1] if len(parts) >= 3 else ""


@dataclass
class MessageKind:
    key: str
    code: str = ""
    severities: Counter = field(default_factory=Counter)
    count: int = 0
    files: set[str] = field(default_factory=set)
    texts: Counter = field(default_factory=Counter)
    details: Counter = field(default_factory=Counter)
    first: float | None = None
    last: float | None = None

    def add(self, severity: str, text: str, detail: str, path: str, mtime: float) -> None:
        self.severities[severity] += 1
        self.count += 1
        self.files.add(path)
        if text:
            self.texts[normalize(text)[:600]] += 1
        if detail:
            self.details[detail] += 1
        self.first = mtime if self.first is None else min(self.first, mtime)
        self.last = mtime if self.last is None else max(self.last, mtime)

    @property
    def severity(self) -> str:
        return self.severities.most_common(1)[0][0]

    @property
    def main_text(self) -> str:
        return self.texts.most_common(1)[0][0] if self.texts else ""

    def sentences(self, pattern: re.Pattern) -> list[str]:
        found = []
        for text, _ in self.texts.most_common(3):
            for sentence in re.split(r"(?<=[.?!])\s+", text):
                if pattern.search(sentence) and sentence not in found:
                    found.append(sentence)
        return found

    def stats_line(self) -> str:
        span = ""
        if self.first and self.last:
            first, last = (dt.date.fromtimestamp(t).isoformat() for t in (self.first, self.last))
            span = f"，{first} 至 {last}" if first != last else f"，{first}"
        return f"历史日志：{self.count} 次，{len(self.files)} 个 .out 文件{span}（统计于 {today()}）"


SSH_PREFIX = "ssh://"


def ssh_options() -> list[str]:
    """Options for the ssh calls below; DIANA_MCP_SSH_OPTIONS carries the key file and anything else needed.

    Split with posix=False so a Windows key path keeps its backslashes, then drop the quotes that leaves behind.
    """
    raw = os.environ.get("DIANA_MCP_SSH_OPTIONS", "")
    extra = [token[1:-1] if token[:1] in "\"'" and token[-1:] == token[:1] else token
             for token in shlex.split(raw, posix=False)]
    return [*extra, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


@dataclass(frozen=True)
class RemoteOut:
    """A .out file on another machine. Listed over SSH and read through the tunnel when it is parsed; the copy
    that makes parsing possible is a temporary file deleted right after, so nothing is stored on this machine."""
    target: str  # user@host
    path: str  # absolute path on that machine
    size: int
    mtime: float

    @property
    def label(self) -> str:
        return f"{SSH_PREFIX}{self.target}{self.path}"


def _list_remote(url: str) -> list[RemoteOut]:
    """Every .out file under an ``ssh://user@host/absolute/path`` folder, with its size and mtime."""
    target, _, path = url[len(SSH_PREFIX):].partition("/")
    if not target or not path:
        raise ValueError(f"expected ssh://user@host/absolute/path, got {url}")
    listing = subprocess.run(
        ["ssh", *ssh_options(), target,
         f"find {shlex.quote('/' + path)} -type f -iname '*.out' -printf '%s %T@ %p\\n'"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    if listing.returncode != 0:
        raise OSError(f"cannot list {url}: {listing.stderr.strip() or f'ssh exit {listing.returncode}'}")
    files = []
    for line in listing.stdout.splitlines():
        size, _, rest = line.partition(" ")
        mtime, _, name = rest.partition(" ")
        if name:
            files.append(RemoteOut(target, name, int(size), float(mtime)))
    return files


@contextlib.contextmanager
def _fetched(item: RemoteOut) -> Iterator[Path]:
    """Stream one remote .out into a temporary file and delete it again as soon as it has been parsed."""
    handle = tempfile.NamedTemporaryFile(prefix="diana-mcp-out-", suffix=".out", delete=False)
    try:
        with handle:
            result = subprocess.run(["ssh", *ssh_options(), item.target, f"cat {shlex.quote(item.path)}"],
                                    stdout=handle, stderr=subprocess.PIPE, timeout=900)
        if result.returncode != 0:
            raise OSError(f"cannot read {item.label}: {result.stderr.decode('utf-8', 'replace').strip()}")
        yield Path(handle.name)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def find_out_files(folders: Iterable[str | os.PathLike]) -> list[Path | RemoteOut]:
    """Every .out file under these folders. A folder may be an ``ssh://user@host/path`` URL: those files stay on
    that machine and are streamed when scanned."""
    found: dict[str, Path] = {}
    remote: dict[str, RemoteOut] = {}
    for folder in folders:
        if str(folder).startswith(SSH_PREFIX):
            for item in _list_remote(str(folder)):
                remote.setdefault(item.label, item)
            continue
        root = Path(folder)
        candidates = [root] if root.is_file() else root.rglob("*.out") if root.is_dir() else []
        for path in candidates:
            if path.suffix.lower() == ".out" and "$RECYCLE.BIN" not in path.parts:
                found.setdefault(os.path.normcase(str(path.resolve())), path)
    return sorted(found.values()) + [remote[key] for key in sorted(remote)]


def scan(paths: Iterable[Path | RemoteOut],
         progress: Callable[[int, Path | RemoteOut], None] | None = None) -> tuple[dict[str, MessageKind], dict[str, Any]]:
    kinds: dict[str, MessageKind] = {}
    stats = {"files": 0, "skipped": 0, "endings": Counter()}
    for index, item in enumerate(paths):
        if progress:
            progress(index, item)
        remote = isinstance(item, RemoteOut)
        try:
            size, mtime = (item.size, item.mtime) if remote else (item.stat().st_size, item.stat().st_mtime)
            if size > MAX_FILE_BYTES:
                stats["skipped"] += 1
                continue
            if remote:
                with _fetched(item) as local:
                    summary = parse_out(local)
            else:
                summary = parse_out(item)
        except (OSError, UnicodeError, ValueError):
            stats["skipped"] += 1
            continue
        stats["files"] += 1
        stats["endings"]["normal" if summary.terminated else "aborted" if summary.aborted else
                         "stopped" if summary.stopped else "unfinished"] += 1
        label = item.label if remote else str(item)
        for message in summary.messages:
            key = message_key(message.severity, message.code, message.text)
            if key is None or key[0] is None:
                continue
            kind = kinds.setdefault(key[0], MessageKind(key[0], code=message.code))
            kind.add(message.severity, message.text, key[1], label, mtime)
    stats["endings"] = dict(stats["endings"])
    return kinds, stats


def draft_fields(kind: MessageKind, title: str | None = None) -> dict[str, Any]:
    severity = kind.severity
    module = module_of(kind.code)
    entry_type = MODULE_TYPES.get(module) or ("error-fix" if severity in SEVERE else "modeling-pitfall")
    head = kind.main_text.split(". ")[0][:110] if kind.main_text else kind.key
    quoted = "\n".join(f"> {text}" for text, _ in kind.texts.most_common(2)) or f"> {kind.key}"
    symptom = (f"{kind.stats_line()}，严重程度 {'/'.join(kind.severities)}。\n\n"
               f"DIANA 原文（数字已替换为 <n>/<x>）：\n\n{quoted}")
    if kind.details:
        rows = "\n".join(f"| {d} | {n} |" for d, n in kind.details.most_common(30))
        symptom += f"\n\n出现过的关键字：\n\n| 关键字 | 次数 |\n|---|---|\n{rows}"
    explanation = kind.sentences(_EXPLANATION)
    advice = kind.sentences(_ADVICE)
    cause = ("DIANA 的说明：" + " ".join(explanation) + "\n\n" if explanation else "") + \
        "（待分析：再遇到时由 AI 结合手册和当时的模型补充。）"
    fix = ("DIANA 的建议：" + " ".join(advice) + "\n\n（未验证。）" if advice else "（待补充。）")
    triggers = [kind.code] if kind.code else [kind.key.split(":", 1)[1]]
    return {
        "entry_id": entry_id(kind.key),
        "title": title or (f"{kind.code}：{head}" if kind.code else head),
        "kind": entry_type,
        "symptom": symptom,
        "cause": cause,
        "fix": fix,
        "verification": "未验证：由历史日志统计生成，还没有经过确认的解决过程。",
        "notes": "示例文件：\n\n" + "\n".join(f"- {p}" for p in sorted(kind.files)[:5]),
        "triggers": triggers,
        "tags": ["历史日志", severity] + ([module] if module else []),
        "evidence": [kind.stats_line()],
        "source": "history-log",
    }


def seed(store: ExperienceStore, kinds: dict[str, MessageKind], titles: dict[str, str] | None = None,
         dry_run: bool = False, diana_version: str | None = None,
         message: str = "knowledge: 从历史日志生成草稿") -> dict[str, Any]:
    """Create a draft per new message kind and refresh the statistics of existing ones, in one commit."""
    titles = titles or {}
    created, updated, unchanged = [], [], []
    ordered = sorted(kinds.values(), key=lambda k: (k.severity not in SEVERE, -k.count))
    if dry_run:
        return {"would_create": [entry_id(k.key) for k in ordered
                                 if not (store.entries_dir / f"{entry_id(k.key)}.md").exists()],
                "kinds": len(ordered)}
    with store.batch(message):
        for kind in ordered:
            fields = draft_fields(kind, titles.get(kind.key))
            path = store.entries_dir / f"{fields['entry_id']}.md"
            if path.exists():
                entry = store.get(fields["entry_id"])
                old = [e for e in entry.evidence if e.startswith("历史日志：")]
                if [re.sub(r"（统计于.*）$", "", e) for e in old] == [re.sub(r"（统计于.*）$", "", kind.stats_line())]:
                    unchanged.append(entry.id)
                    continue
                kept = [e for e in entry.evidence if not e.startswith("历史日志：")]
                store.update(entry.id, evidence=kept + [kind.stats_line()], note="更新历史日志统计")
                updated.append(entry.id)
                continue
            try:
                store.add(diana_version=diana_version, **fields)
                created.append(fields["entry_id"])
            except ExperienceError as exc:
                unchanged.append(f"{fields['entry_id']}: {exc}")
    return {"created": created, "updated": updated, "unchanged": unchanged, "commit": store.last_batch_commit}
