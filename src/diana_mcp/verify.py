"""Executable experience: entries with a '## 验证脚本' section are checked on the installed DIANA.

The selected scripts run in ONE short-lived batch DianaIE (about 15 s to start), never in the user's live session,
each in a new project of its own under a temporary folder (the variable WORKDIR). A script passes when it runs to
the end without an exception, so it states its checks with assert. The result goes into the entry's `checks`
field. A verified entry that fails is marked disputed: what the user approved no longer holds on this DIANA
version. The whole run is one git commit. Run it again after installing a new DIANA version.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

from . import config
from .experience import ExperienceStore

RUNNER = r'''
import contextlib, io, json, os, time, traceback
_specs = json.load(open(%(spec)r, encoding="utf-8"))
_results = {}
for _spec in _specs:
    _folder = os.path.join(%(work)r, _spec["id"])
    os.makedirs(_folder, exist_ok=True)
    _ns = dict(globals())
    _ns["WORKDIR"] = _folder.replace("\\", "/")
    _out = io.StringIO()
    _t = time.time()
    try:
        with contextlib.redirect_stdout(_out):
            exec(compile(_spec["code"], "<" + _spec["id"] + ">", "exec"), _ns)
        _results[_spec["id"]] = {"ok": True}
    except BaseException:
        _results[_spec["id"]] = {"ok": False, "error": traceback.format_exc()[-3000:]}
    _results[_spec["id"]].update(stdout=_out.getvalue()[-2000:], seconds=round(time.time() - _t, 2))
    try:
        closeProject()
    except Exception:
        pass
    with open(%(result)r, "w", encoding="utf-8") as _f:  # after every script: partial results survive a crash
        json.dump(_results, _f, ensure_ascii=False)
'''


def last_line(text: str) -> str:
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1][:300] if lines else ""


def run_batch_dianaie(install: config.DianaInstall, script: Path, timeout: float) -> None:
    """Run a script in a short-lived DianaIE: no bridge autostart, no Communicator, working directory in the
    state folder (processes DianaIE leaves behind must not hold a temporary folder open)."""
    home = config.state_dir() / "batch"
    home.mkdir(parents=True, exist_ok=True)
    env = config.launch_env(DIANA_MCP_AUTOSTART="0", COMMUNICATOR_CONNECTION="server=NONE")
    try:
        subprocess.run(config.batch_argv(install.dianaie, "--noSplash", "--workingDir",
                                         config.to_windows(home), config.to_windows(script)),
                       cwd=home, env=env, timeout=timeout, check=False,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass


def run_scripts(install: config.DianaInstall, specs: list[dict[str, str]], timeout: float = 900) -> dict[str, Any]:
    """{id: {"ok", "error", "stdout", "seconds"}} for [{"id", "code"}], from one batch DianaIE run."""
    with tempfile.TemporaryDirectory(prefix="diana-mcp-verify-", ignore_cleanup_errors=True) as tmp:
        folder = Path(tmp)
        spec_path, result_path, work = folder / "specs.json", folder / "results.json", folder / "work"
        spec_path.write_text(json.dumps(specs, ensure_ascii=False), encoding="utf-8")
        script = folder / "run_checks.py"
        # The runner executes inside DianaIE, so these are the paths as DIANA sees them.
        script.write_text(RUNNER % {"spec": config.to_windows(spec_path), "work": config.to_windows(work),
                                    "result": config.to_windows(result_path)}, encoding="utf-8")
        run_batch_dianaie(install, script, timeout)
        try:
            results = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            results = {}
    return results


def verify(store: ExperienceStore, install: config.DianaInstall, ids: Iterable[str] | None = None,
           timeout: float = 900) -> dict[str, Any]:
    wanted = set(ids) if ids else None
    entries = [e for e in store.entries() if e.script and e.status != "obsolete"
               and (wanted is None or e.id in wanted)]
    if wanted:
        missing = sorted(wanted - {e.id for e in entries})
    else:
        missing = []
    if not entries:
        return {"diana_version": install.version, "checked": 0, "without_script": missing}
    results = run_scripts(install, [{"id": e.id, "code": e.script} for e in entries], timeout)
    passed, failed, not_run = [], [], []
    today = dt.date.today().isoformat()
    with store.batch(f"knowledge: 自动验证 {len(entries)} 条经验（DIANA {install.version}）"):
        for entry in entries:
            result = results.get(entry.id)
            if result is None:
                not_run.append(entry.id)
                continue
            ok = bool(result.get("ok"))
            detail = "通过" if ok else last_line(result.get("error", ""))
            store.record_check(entry.id, {"date": today, "diana_version": install.version,
                                          "result": "pass" if ok else "fail", "detail": detail})
            if ok:
                passed.append(entry.id)
            else:
                failed.append({"id": entry.id, "error": detail, "stdout": result.get("stdout", "")[-500:]})
                if entry.status == "verified":
                    store.set_status(entry.id, "disputed",
                                     f"自动验证失败（DIANA {install.version}）：{detail}")
    out = {"diana_version": install.version, "checked": len(entries), "passed": passed, "failed": failed,
           "commit": store.last_batch_commit}
    if not_run:
        out["not_run"] = not_run
        out["note"] = "部分脚本没有结果：DianaIE 可能没有启动（许可证？）或超时"
    if missing:
        out["without_script"] = missing
    return out
