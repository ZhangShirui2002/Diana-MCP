"""Optional, Git-backed community knowledge and privacy-preserving contribution staging.

The private :mod:`diana_mcp.experience` repository is never used as a Git remote and is never modified by this
module.  Contributions are copied into a separate staging area, redacted, and require the literal confirmation
``UPLOAD <batch-id>`` before a publisher may touch a remote repository.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config
from .experience import Entry, ExperienceError, ExperienceStore, render_body

DEFAULT_REPOSITORY = "https://github.com/ZhangShirui2002/Diana-MCP-Knowledge.git"
COMMUNITY_STATUSES = {
    "candidate", "community-confirmed", "reproducibly-verified", "disputed", "obsolete",
}
PUBLIC_EXTENSIONS = "dat|dcf|out|tb|dnb|dna|ff|log|dpf|sys"


class CommunityError(RuntimeError):
    pass


def _run_git(cwd: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    command = ["git"]
    # Unlike changing global Git config, this is scoped to community operations. An explicitly empty value disables
    # a stale global proxy; an unset value preserves the user's normal Git networking configuration.
    if "DIANA_MCP_COMMUNITY_GIT_PROXY" in os.environ:
        command += ["-c", f"http.https://github.com.proxy={os.environ['DIANA_MCP_COMMUNITY_GIT_PROXY']}"]
    command += (["-C", str(cwd)] if cwd is not None else []) + list(args)
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def _git_error(done: subprocess.CompletedProcess[str], action: str) -> None:
    if done.returncode:
        raise CommunityError(f"{action}失败：{(done.stderr or done.stdout).strip()}")


@dataclass(frozen=True)
class CommunitySettings:
    url: str = DEFAULT_REPOSITORY
    branch: str = "main"
    push_url: str | None = None

    @classmethod
    def from_environment(cls) -> "CommunitySettings":
        return cls(
            os.environ.get("DIANA_MCP_COMMUNITY_URL", DEFAULT_REPOSITORY).strip() or DEFAULT_REPOSITORY,
            os.environ.get("DIANA_MCP_COMMUNITY_BRANCH", "main").strip() or "main",
            os.environ.get("DIANA_MCP_COMMUNITY_PUSH_URL", "").strip() or None,
        )


class CommunityStore:
    """Read-only Adapter over a separately cloned community Git repository."""

    def __init__(self, root: Path | None = None, *, url: str | None = None, branch: str | None = None,
                 push_url: str | None = None):
        settings = CommunitySettings.from_environment()
        self.root = Path(root) if root else config.community_dir() / "repository"
        self.url = url or settings.url
        self.branch = branch or settings.branch
        self.push_url = push_url or settings.push_url or self.url

    @property
    def entries_dir(self) -> Path:
        return self.root / "experiences"

    @property
    def ready(self) -> bool:
        return (self.root / ".git").is_dir() and self.entries_dir.is_dir()

    def entries(self) -> list[Entry]:
        entries: list[Entry] = []
        if not self.entries_dir.is_dir():
            return entries
        generated: dict[str, Any] = {}
        index_path = self.root / "index.json"
        if index_path.is_file():
            if index_path.is_symlink() or index_path.stat().st_size > 10 * 1024 * 1024:
                raise CommunityError("社区 index.json 是符号链接或超过 10 MiB")
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
                if index.get("format") != 1 or not isinstance(index.get("entries"), dict):
                    raise ValueError("expected format 1 with an entries mapping")
                generated = index["entries"]
            except (OSError, ValueError, TypeError) as exc:
                raise CommunityError(f"社区 index.json 无效：{exc}") from exc
        for path in sorted(self.entries_dir.glob("*/*.md")):
            if path.is_symlink() or path.stat().st_size > 128 * 1024:
                raise CommunityError(f"社区条目是符号链接或超过 128 KiB：{path.relative_to(self.root)}")
            try:
                entry = Entry.from_text(path.read_text(encoding="utf-8"))
            except (ExperienceError, ValueError) as exc:
                raise CommunityError(f"社区条目格式错误 {path.relative_to(self.root)}：{exc}") from exc
            entry.path = path
            status = str(entry.extra.get("community_status", path.parent.name))
            if status not in COMMUNITY_STATUSES:
                raise CommunityError(f"{path.name}: community_status {status!r} 无效")
            entry.extra["community_status"] = status
            try:
                entry.extra["confidence"] = max(0.0, min(1.0, float(entry.extra.get("confidence", 0))))
            except (TypeError, ValueError) as exc:
                raise CommunityError(f"{path.name}: confidence 必须是 0–1 的数值") from exc
            computed = generated.get(entry.id, {})
            if computed:
                effective = str(computed.get("effective_status", status))
                if effective not in COMMUNITY_STATUSES:
                    raise CommunityError(f"index.json: {entry.id} 的 effective_status {effective!r} 无效")
                entry.extra["community_status"] = effective
                try:
                    entry.extra["confidence"] = max(0.0, min(1.0, float(computed.get(
                        "confidence", entry.extra["confidence"]))))
                except (TypeError, ValueError) as exc:
                    raise CommunityError(f"index.json: {entry.id} 的 confidence 无效") from exc
                for key in ("successful_confirmations", "failed_confirmations", "last_confirmed"):
                    if key in computed:
                        entry.extra[key] = computed[key]
            entries.append(entry)
        return entries

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.entries_dir.is_dir():
            return problems
        for path in self.entries_dir.rglob("*"):
            if path.is_symlink():
                problems.append(f"不允许符号链接：{path.relative_to(self.root)}")
            elif path.is_file() and path.suffix.lower() == ".md" and path.stat().st_size > 128 * 1024:
                problems.append(f"条目超过 128 KiB：{path.relative_to(self.root)}")
        try:
            self.entries()
        except CommunityError as exc:
            problems.append(str(exc))
        return problems

    def _commit(self) -> str | None:
        if not (self.root / ".git").is_dir():
            return None
        done = _run_git(self.root, "rev-parse", "HEAD")
        return done.stdout.strip() if done.returncode == 0 else None

    def status(self) -> dict[str, Any]:
        dirty = False
        if (self.root / ".git").is_dir():
            dirty = bool(_run_git(self.root, "status", "--porcelain").stdout.strip())
        entries: list[Entry] = []
        problems: list[str] = []
        if self.entries_dir.is_dir():
            try:
                entries = self.entries()
            except CommunityError as exc:
                problems.append(str(exc))
        counts = Counter(str(e.extra.get("community_status")) for e in entries)
        return {
            "enabled": os.environ.get("DIANA_MCP_DISABLE_COMMUNITY", "").lower() not in ("1", "true", "yes"),
            "synced": self.ready,
            "repository": self.url,
            "contribution_repository": self.push_url,
            "branch": self.branch,
            "cache": str(self.root),
            "commit": self._commit(),
            "dirty": dirty,
            "entries": len(entries),
            "counts": dict(sorted(counts.items())),
            "problems": problems,
            "automatic_network_access": False,
        }

    def sync(self) -> dict[str, Any]:
        """Explicitly clone/fetch the configured repository; community files are consumed only as data."""
        if os.environ.get("DIANA_MCP_DISABLE_COMMUNITY", "").lower() in ("1", "true", "yes"):
            raise CommunityError("社区知识已由 DIANA_MCP_DISABLE_COMMUNITY 禁用")
        self.root.parent.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").is_dir():
            if self.root.exists() and any(self.root.iterdir()):
                raise CommunityError(f"社区缓存目录非空且不是 Git 仓库：{self.root}")
            local_source = Path(self.url)
            if local_source.exists():  # also makes tests and air-gapped mirrors independent of Git transports
                if self.root.exists():
                    self.root.rmdir()
                shutil.copytree(local_source, self.root)
                selected = _run_git(self.root, "checkout", self.branch)
                _git_error(selected, "切换本地社区知识分支")
            else:
                done = _run_git(None, "clone", "--depth", "1", "--branch", self.branch, "--", self.url, str(self.root))
                _git_error(done, "克隆社区知识库")
        else:
            dirty = _run_git(self.root, "status", "--porcelain").stdout.strip()
            if dirty:
                raise CommunityError("社区缓存有本地修改，拒绝覆盖；请删除修改或改用新的缓存目录")
            current = _run_git(self.root, "remote", "get-url", "origin")
            _git_error(current, "读取社区知识库远程地址")
            if current.stdout.strip().rstrip("/") != self.url.rstrip("/"):
                raise CommunityError(f"缓存 origin 是 {current.stdout.strip()}，与配置的 {self.url} 不同")
            fetched = _run_git(self.root, "fetch", "--depth", "1", "origin", self.branch)
            _git_error(fetched, "获取社区知识库")
            checked = _run_git(self.root, "checkout", "--detach", "FETCH_HEAD")
            _git_error(checked, "切换社区知识版本")
        problems = self.validate()
        if problems:
            raise CommunityError("同步内容未通过检查：" + "；".join(problems[:10]))
        return {**self.status(), "synced": True}


_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("email", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "<EMAIL>"),
    ("ssh", re.compile(r"(?i)\b(?:ssh://)?[\w.-]+@[\w.-]+(?::\d+)?\b"), "<SSH_ENDPOINT>"),
    ("unc_path", re.compile(r"(?i)(?<!\w)\\\\[^\\\s]+\\[^\s<>\"|]+"), "<LOCAL_PATH>"),
    ("windows_path", re.compile(r"(?i)(?<!\w)[A-Z]:[\\/][^\s<>\"|]+"), "<LOCAL_PATH>"),
    ("unix_path", re.compile(r"(?<!\w)/(?:home|users|mnt|work|tmp|var/tmp)/[^\s<>\"']+", re.I), "<LOCAL_PATH>"),
    ("ipv4", re.compile(r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])"), "<IP_ADDRESS>"),
    ("diana_file", re.compile(rf"(?i)\b[^\s<>\"'\\/]+\.(?:{PUBLIC_EXTENSIONS})\b"), "<DIANA_FILE>"),
]


def redact_text(text: str) -> tuple[str, Counter[str]]:
    """Deterministically redact obvious identifiers and return counts, never the matched values."""
    redacted, findings = str(text or ""), Counter()
    for kind, pattern, replacement in _PATTERNS:
        redacted, count = pattern.subn(replacement, redacted)
        findings[kind] += count
    for kind, value in (("user_name", os.environ.get("USERNAME")),
                        ("host_name", os.environ.get("COMPUTERNAME"))):
        if value and len(value.strip()) >= 3:
            redacted, count = re.subn(re.escape(value.strip()), f"<{kind.upper()}>", redacted, flags=re.I)
            findings[kind] += count
    return redacted, +findings


def _public_entry(entry: Entry) -> tuple[Entry, Counter[str]]:
    findings: Counter[str] = Counter()

    def clean(text: str) -> str:
        value, seen = redact_text(text)
        findings.update(seen)
        return value

    sections = {name: clean(text) for name, text in entry.sections.items() if name != "验证脚本"}
    if "验证脚本" in entry.sections:
        findings["verification_script_removed"] += 1
    digest_source = "\n".join([clean(entry.title), clean(entry.body), entry.type, entry.diana_version or ""])
    public_id = "candidate-" + hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:16]
    title = clean(entry.title)
    public = Entry(
        id=public_id,
        title=title,
        type=entry.type,
        status="draft",
        diana_version=clean(entry.diana_version or "") or None,
        triggers=[clean(value) for value in entry.triggers],
        tags=[clean(value) for value in entry.tags],
        manual=[clean(value) for value in entry.manual],
        evidence=[],
        related=[],
        source=entry.source,
        created=entry.created,
        updated=entry.updated,
        body=render_body(sections),
        extra={
            "community_status": "candidate",
            "confidence": "0.10",
            "license": "Apache-2.0",
            "public_origin": "local-redacted:" + hashlib.sha256(entry.id.encode("utf-8")).hexdigest()[:12],
            "requires_human_review": "true",
        },
    )
    return public, findings


class ContributionManager:
    """Application service for prepare/preview/contribute; remote publication is always separately gated."""

    def __init__(self, private: ExperienceStore, staging_root: Path | None = None,
                 community: CommunityStore | None = None):
        self.private = private
        self.staging_root = Path(staging_root) if staging_root else config.community_dir() / "staging"
        self.community = community or CommunityStore()

    def eligible(self) -> list[dict[str, Any]]:
        """Low-risk first-pass candidates. This is a filter, not a privacy or correctness guarantee."""
        candidates = []
        for entry in self.private.entries():
            if entry.status == "obsolete" or entry.source == "history-log" or entry.script:
                continue
            _, obvious = redact_text(entry.to_text())
            candidates.append({
                "id": entry.id,
                "title": entry.title,
                "status": entry.status,
                "obvious_sensitive_matches": sum(obvious.values()),
            })
        return candidates

    def prepare(self, entry_ids: Iterable[str]) -> dict[str, Any]:
        ids = list(dict.fromkeys(str(i).strip() for i in entry_ids if str(i).strip()))
        if not ids:
            raise CommunityError("至少指定一条本地经验")
        batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        folder = self.staging_root / batch_id
        candidates = folder / "experiences" / "candidate"
        candidates.mkdir(parents=True, exist_ok=False)
        items = []
        totals: Counter[str] = Counter()
        for entry_id in ids:
            try:
                entry = self.private.get(entry_id)
            except KeyError as exc:
                raise CommunityError(f"没有本地经验 {entry_id!r}") from exc
            public, findings = _public_entry(entry)
            totals.update(findings)
            path = candidates / f"{public.id}.md"
            path.write_text(public.to_text(), encoding="utf-8")
            items.append({
                "source_ref": hashlib.sha256(entry.id.encode("utf-8")).hexdigest()[:12],
                "public_id": public.id,
                "file": str(path.relative_to(folder)).replace("\\", "/"),
                "findings": dict(sorted(findings.items())),
                "requires_human_review": True,
            })
        manifest = {
            "format": 1,
            "batch_id": batch_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "entries": len(items),
            "items": items,
            "finding_counts": dict(sorted(totals.items())),
            "requires_human_review": True,
            "semantic_risks": [
                "项目名称、唯一几何、材料、荷载和试验编号需要人工判断",
                "自动脱敏不能证明内容可公开，也不能证明技术结论正确",
            ],
            "upload_performed": False,
        }
        (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    def _manifest(self, batch_id: str) -> tuple[Path, dict[str, Any]]:
        if not re.fullmatch(r"[0-9TZ-]{16,24}[0-9a-f]{8}", batch_id):
            raise CommunityError("batch_id 格式无效")
        folder = self.staging_root / batch_id
        path = folder / "manifest.json"
        if not path.is_file():
            raise CommunityError(f"没有脱敏批次 {batch_id!r}")
        return folder, json.loads(path.read_text(encoding="utf-8"))

    def preview(self, batch_id: str, *, include_text: bool = False) -> dict[str, Any]:
        folder, manifest = self._manifest(batch_id)
        items = []
        for item in manifest["items"]:
            path = folder / item["file"]
            record = {**item, "file": str(path)}
            if include_text:
                record["text"] = path.read_text(encoding="utf-8")
            items.append(record)
        return {**manifest, "folder": str(folder), "items": items,
                "required_confirmation": f"UPLOAD {batch_id}"}

    def contribute(self, batch_id: str, confirmation: str) -> dict[str, Any]:
        """Publish a prepared batch. Exact confirmation is required immediately before any network write."""
        expected = f"UPLOAD {batch_id}"
        if confirmation.strip() != expected:
            raise CommunityError(f"上传是公开外部写入；请在看完预览后明确传入 {expected!r}")
        folder, manifest = self._manifest(batch_id)
        if (folder / "UPLOADED.json").exists():
            raise CommunityError("这个批次已经上传过，拒绝重复发布")
        if not self.community.ready:
            self.community.sync()
        branch = "contribution/" + batch_id.lower()
        with tempfile.TemporaryDirectory(prefix="diana-community-") as temp:
            checkout = Path(temp) / "repository"
            source = Path(self.community.url).resolve().as_uri() if Path(self.community.url).exists() else self.community.url
            cloned = _run_git(None, "clone", "--depth", "1", "--branch", self.community.branch,
                              "--", source, str(checkout))
            _git_error(cloned, "建立贡献工作副本")
            public_name = os.environ.get("DIANA_MCP_COMMUNITY_GIT_NAME") or _run_git(None, "config", "--global",
                                                                                     "user.name").stdout.strip()
            public_email = os.environ.get("DIANA_MCP_COMMUNITY_GIT_EMAIL") or _run_git(None, "config", "--global",
                                                                                        "user.email").stdout.strip()
            if not public_name or not public_email or not public_email.lower().endswith("@users.noreply.github.com"):
                raise CommunityError("为避免在 Signed-off-by 中泄露邮箱，请先设置公开的 "
                                     "DIANA_MCP_COMMUNITY_GIT_NAME 和 GitHub noreply 邮箱 "
                                     "DIANA_MCP_COMMUNITY_GIT_EMAIL")
            _git_error(_run_git(checkout, "config", "user.name", public_name), "配置公开贡献者名称")
            _git_error(_run_git(checkout, "config", "user.email", public_email), "配置公开贡献者邮箱")
            _git_error(_run_git(checkout, "checkout", "-b", branch), "建立贡献分支")
            remote = "origin"
            if self.community.push_url.rstrip("/") != self.community.url.rstrip("/"):
                _git_error(_run_git(checkout, "remote", "add", "contribution", self.community.push_url),
                           "配置贡献 fork")
                remote = "contribution"
            added = []
            for item in manifest["items"]:
                source = folder / item["file"]
                target = checkout / item["file"]
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise CommunityError(f"社区中已存在 {item['public_id']}，请重新同步并检查重复项")
                shutil.copy2(source, target)
                added.append(item["file"])
            _git_error(_run_git(checkout, "add", "--", *added), "暂存社区候选")
            _git_error(_run_git(checkout, "commit", "-s", "-m", f"knowledge: add {len(added)} redacted candidates"),
                       "提交社区候选")
            pushed = _run_git(checkout, "push", "-u", remote, branch)
            _git_error(pushed, "推送社区贡献分支")
            commit = _run_git(checkout, "rev-parse", "HEAD").stdout.strip()
        base = self.community.url.removesuffix(".git").rstrip("/")
        upstream_slug = _github_slug(self.community.url)
        push_slug = _github_slug(self.community.push_url)
        head = f"{push_slug.split('/', 1)[0]}:{branch}" if upstream_slug and push_slug else branch
        result = {
            "uploaded": True,
            "batch_id": batch_id,
            "branch": branch,
            "commit": commit,
            "create_pull_request": f"{base}/compare/{self.community.branch}...{head}?expand=1",
            "note": "候选仍是 candidate；社区自动检查通过前不会进入发布索引。",
        }
        (folder / "UPLOADED.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result


def _github_slug(url: str) -> str | None:
    match = re.search(r"github\.com[/:]([^/\s:]+/[^/\s]+?)(?:\.git)?$", url.rstrip("/"), re.I)
    return match.group(1) if match else None
