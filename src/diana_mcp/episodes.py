"""Learning from solver runs: a problem run followed by a successful run of the same case becomes a draft.

For every finished job the solver log is classified (success / problem). A problem run gets the experience entries
that match its messages (shown to the AI right away, and remembered). When a later run of the same case (same
project + analysis, or same folder + job name) succeeds, the two runs' inputs are compared, the .dcf commands
and the .dat model definition without the mesh, and the difference is drafted as "problem -> change -> result".
The AI then explains the cause and asks the user to approve it; drafts that were recommended for the problem
are proposed for approval too, since the same problem has now been solved.
"""
from __future__ import annotations

import difflib
import time
from pathlib import Path
from typing import Any

from . import config, usage
from .experience import STATUSES, ExperienceError, ExperienceStore
from .jobs import Job, JobManager

MAX_DIFF_LINES = 80


def _lines(path: Path) -> list[str] | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def diff(old: list[str], new: list[str], limit: int = MAX_DIFF_LINES) -> list[str]:
    lines = [l for l in difflib.unified_diff(old, new, lineterm="", n=1) if not l.startswith(("---", "+++"))]
    return lines[:limit] + ([f"... ({len(lines) - limit} more lines)"] if len(lines) > limit else [])


def case_label(job: Job) -> str:
    if job.project and job.analysis:
        return f"{Path(job.project).stem} / {job.analysis}"
    return job.name


def problem_texts(problem: dict[str, Any] | None) -> list[str]:
    if not problem:
        return []
    texts = [f"{e.get('severity', '')} {e.get('code', '')} {e.get('text', '')}" for e in problem.get("errors", [])]
    texts.append(str(problem.get("end", "")))
    if problem.get("not_converged_steps"):
        texts.append("NO CONVERGENCE")
    return texts


def describe_problem(problem: dict[str, Any] | None) -> str:
    if not problem:
        return "（无记录）"
    parts = [f"结束状态：{problem.get('end')}"]
    for error in problem.get("errors", [])[:5]:
        parts.append(f"- {error.get('severity')} {error.get('code') or ''}：{error.get('text', '')[:200]}")
    if problem.get("not_converged_steps"):
        parts.append(f"未收敛的步：{problem['not_converged_steps']}")
    if problem.get("last_converged_step"):
        parts.append(f"最后收敛的步：{problem['last_converged_step']}")
    if problem.get("progress"):
        parts.append(f"进度：{problem['progress']}")
    return "\n".join(parts)


def _date(epoch: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)) if epoch else "?"


def _installed_version() -> str | None:
    try:
        return config.default_install().version
    except FileNotFoundError:
        return None


def learn(job: Job, jobs: JobManager, store: ExperienceStore, limit: int = 3) -> None:
    """Hook for JobManager.on_finished."""
    if job.outcome == "problem":
        boost = {i: u.factor for i, u in usage.experience_usage().items()}
        job.recommended = [r["id"] for r in store.recommend(problem_texts(job.problem), limit, boost=boost)]
        usage.log_recommendation(f"job:{job.id}", job.recommended)
        jobs.save(job)
        return
    if job.outcome != "success" or job.resolves or not job.case:
        return
    earlier = sorted((j for j in jobs.list(limit=500)
                      if j.case == job.case and j.id != job.id and j.started < job.started and j.outcome),
                     key=lambda j: j.started)
    if not earlier or earlier[-1].outcome != "problem" or earlier[-1].resolved_by:
        return
    failed = earlier[-1]
    old_dir, new_dir = jobs.inputs_dir(failed), jobs.inputs_dir(job)
    old_dcf, new_dcf = _lines(old_dir / "run.dcf"), _lines(new_dir / "run.dcf")
    old_dat, new_dat = _lines(old_dir / "dat_definition.txt"), _lines(new_dir / "dat_definition.txt")
    dcf_changes = diff(old_dcf, new_dcf) if old_dcf is not None and new_dcf is not None else []
    dat_changes = diff(old_dat, new_dat) if old_dat is not None and new_dat is not None else []
    failed.resolved_by, job.resolves = job.id, failed.id
    if dcf_changes or dat_changes:
        codes = [e["code"] for e in (failed.problem or {}).get("errors", []) if e.get("code")]
        first = (failed.problem or {}).get("errors") or [{}]
        headline = first[0].get("code") or ("不收敛" if (failed.problem or {}).get("not_converged_steps")
                                              else str((failed.problem or {}).get("end", "失败"))[:30])
        changes = []
        if dcf_changes:
            changes.append("命令文件（.dcf）的改动：\n\n```diff\n" + "\n".join(dcf_changes) + "\n```")
        if dat_changes:
            changes.append("模型定义（.dat，不含节点坐标和单元连接）的改动：\n\n```diff\n" + "\n".join(dat_changes) + "\n```")
        not_converged = bool((failed.problem or {}).get("not_converged_steps"))
        kind = "convergence" if not_converged or any("/NL/" in c or "/SO/" in c for c in codes) else "error-fix"
        try:
            entry, _ = store.add(
                title=f"{case_label(job)}：{headline} → 修改输入后求解成功",
                kind=kind,
                symptom=f"作业 {failed.id}（{_date(failed.started)}）没有成功：\n\n{describe_problem(failed.problem)}",
                cause="（待 AI 分析补充：结合下面的改动说明问题的原因。）",
                fix="\n\n".join(changes),
                verification=f"修改后的作业 {job.id}（{_date(job.started)}）正常结束，所有荷载步收敛。",
                triggers=codes, tags=["自动起草", "作业对比"],
                evidence=[f"失败作业 {failed.id}：{failed.directory}", f"成功作业 {job.id}：{job.directory}"],
                related=[r for r in failed.recommended if (store.entries_dir / f"{r}.md").exists()],
                source="ai", diana_version=_installed_version(),
            )
            job.draft = entry.id
        except ExperienceError:
            pass
    jobs.save(failed)
    jobs.save(job)


def job_advice(job: Job, jobs: JobManager, store: ExperienceStore) -> dict[str, Any]:
    """What a job status should show the AI: matching experience for a problem, the draft and the approval
    candidates after a problem was solved."""
    out: dict[str, Any] = {}
    if job.outcome == "problem":
        ids = job.recommended or [r["id"] for r in store.recommend(problem_texts(job.problem), 3)]
        entries = []
        for entry_id in ids:
            try:
                entry = store.get(entry_id)
            except (KeyError, ExperienceError):
                continue
            entries.append({"id": entry.id, "title": entry.title, "status": STATUSES[entry.status]})
        if entries:
            out["related_experience"] = entries
            out["experience_note"] = ("先看这些经验（diana_experience_list(id=...)）。未验证的只是线索；"
                                      "若它帮助解决了问题，补充证据后请用户批准。")
    elif job.resolves:
        try:
            failed = jobs.get(job.resolves)
        except Exception:
            failed = None
        learned: dict[str, Any] = {"after_problem_job": job.resolves}
        if job.draft:
            learned["draft"] = job.draft
            learned["next"] = ("已根据失败与成功两次运行的差异起草经验：用 diana_experience_update 补充原因和说明，"
                               "再询问用户是否批准。")
        candidates = []
        for entry_id in (failed.recommended if failed else []):
            try:
                entry = store.get(entry_id)
            except (KeyError, ExperienceError):
                continue
            if entry.status == "draft":
                candidates.append({"id": entry.id, "title": entry.title})
        if candidates:
            learned["approval_candidates"] = candidates
            learned["approval_note"] = ("这些未验证经验在出问题时被推荐过，问题现已解决：若确实是按它们解决的，"
                                        "补充证据（add_evidence）后请用户批准。")
        if job.draft or candidates:
            out["learned"] = learned
    return out
