"""Background DIANA solver jobs: diana.exe on a .dat/.dcf pair, independent of the DianaIE GUI."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config
from .review_gate import GateError, GuardStore
from .out_parser import parse_out
from .session import kill_processes_in, kill_tree, pid_alive, process_image

OUTPUT_SUFFIXES = (".out", ".tb", ".dnb", ".dna", ".sys", ".ff")
# Control commands must precede the first application command (manual Appendix H.1); ':' in column 1
# starts a comment line (Appendix D).
_LEADING = re.compile(rb"^(:|\s*$|\s*(LOG|NOLOG|NUMTHR|ERRORS)\b)", re.IGNORECASE)
# Input files a run can reference: user subroutine DLLs (*FORTRAN USE, Appendix J), additional model files
# (*INPUT READ FILE=, Appendix H.3) and imported load/time tables in the .dat.
_REFERENCES = (
    re.compile(rb'\bUSE\s+"([^"]+)"', re.IGNORECASE),
    re.compile(rb'\bREAD\s+FILE\s*=\s*"?([^\s"]+)"?', re.IGNORECASE),
    re.compile(rb'\bIMPORT\s+"([^"]+)"', re.IGNORECASE),
)
# The solver must not start or share the per-user Communicator (manual Appendix K.7); this also keeps a
# cancelled job's process tree away from the Communicator used by DianaIE.
JOB_ENVIRONMENT = {"COMMUNICATOR_CONNECTION": "server=NONE"}


class JobError(RuntimeError):
    pass


# Tables of a .dat that hold the mesh itself; the rest (materials, geometry, loads, supports, groups, units)
# is the model definition that job-to-job comparisons are about.
_DAT_TABLE = re.compile(r"^'([A-Z]+)'")
_DAT_SUBTABLE = re.compile(r"^[A-Z][A-Z0-9_]*\b")
DEFINITION_TABLE_LINES = 400


def dat_definition(path: Path) -> list[str]:
    """The model definition of a .dat: every table except node coordinates and element connectivity, whose
    sizes are kept as one summary line each; long tables are cut at DEFINITION_TABLE_LINES lines."""
    out: list[str] = []
    table = subtable = ""
    skipped = kept = 0

    def flush() -> None:
        nonlocal skipped
        if skipped:
            out.append(f"  ({skipped} lines of {table if table == 'COORDINATES' else 'CONNECT'} not compared)")
            skipped = 0

    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip()
            head = _DAT_TABLE.match(line)
            if head:
                flush()
                table, subtable, kept = head.group(1), "", 0
                out.append(line)
                continue
            sub = _DAT_SUBTABLE.match(line)
            if sub and table == "ELEMENTS":
                flush()
                subtable = sub.group(0)
            if table == "COORDINATES" or (table == "ELEMENTS" and subtable == "CONNECT" and not sub):
                skipped += 1
                continue
            kept += 1
            if kept <= DEFINITION_TABLE_LINES:
                out.append(line)
            elif kept == DEFINITION_TABLE_LINES + 1:
                out.append(f"  (table '{table}' continues; not compared further)")
    flush()
    return out


def classify(out: Path) -> tuple[str, dict[str, Any] | None]:
    """("success", None) for a normal end without errors or non-converged steps, else ("problem", details)."""
    if not out.is_file():
        return "problem", {"end": "no .out file (the solver did not start)"}
    summary = parse_out(out)
    info = summary.summary()
    not_converged = [s.number for s in summary.steps if s.converged is False]
    if summary.completed and not not_converged:
        return "success", None
    problem = {"end": info.get("end"), "errors": [
        {"severity": m.severity, "code": m.code, "text": m.text[:300]} for m in summary.errors[:5]]}
    if not_converged:
        problem["not_converged_steps"] = not_converged[:20]
    if info.get("last_converged_step"):
        problem["last_converged_step"] = info["last_converged_step"]
    if info.get("progress"):
        problem["progress"] = info["progress"]
    return "problem", problem


def standalone_dcf(raw: bytes) -> bytes | None:
    """DianaIE's saveAnalysisCommands writes only the analysis blocks. A standalone diana.exe run also needs
    ``*FILOS``/``INITIA`` (fresh database; only honoured at the start of the command file) and ``*INPUT`` (read the
    .dat) before the first analysis block, after any comments and control commands, plus a final ``*END``.
    Returns the patched bytes, or None if nothing had to change. Works on bytes so the file's encoding (UTF-8
    labels written by a Chinese DianaIE, or GBK) is preserved."""
    body = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
    newline = b"\r\n" if b"\r\n" in body else b"\n"
    lines = body.splitlines(keepends=True)
    first = 0
    while first < len(lines) and _LEADING.match(lines[first]):
        first += 1
    head, rest = b"".join(lines[:first]), b"".join(lines[first:])
    patched = body
    if not re.match(rb"\s*\*FILOS\b", rest, re.IGNORECASE):
        insert = b"*FILOS" + newline + b"INITIA" + newline
        if not re.match(rb"\s*\*INPUT\b", rest, re.IGNORECASE):
            insert += b"*INPUT" + newline
        if head and not head.endswith((b"\n", b"\r")):
            head += newline
        patched = head + insert + rest
    if not re.search(rb"^\s*\*END\b", patched, re.IGNORECASE | re.MULTILINE):
        patched = patched.rstrip() + newline + b"*END" + newline
    return None if patched == body else patched


def referenced_files(raw: bytes) -> list[str]:
    """File names a .dcf or .dat refers to as input."""
    found: list[str] = []
    for pattern in _REFERENCES:
        for match in pattern.finditer(raw):
            name = match.group(1).decode("utf-8", errors="replace")
            if name not in found:
                found.append(name)
    return found


_BEGIN_OUTPUT = re.compile(rb"^\s*BEGIN\s+OUTPUT\b(.*)$", re.IGNORECASE)
_END_OUTPUT = re.compile(rb"^\s*END\s+OUTPUT\b", re.IGNORECASE)
_TABULA = re.compile(rb"^\s*TABULA\b", re.IGNORECASE)
_TEXT = re.compile(rb"^\s*TEXT\b", re.IGNORECASE)
_BEGIN_LAYOUT = re.compile(rb"^\s*BEGIN\s+LAYOUT\b", re.IGNORECASE)
_END_LAYOUT = re.compile(rb"^\s*END\s+LAYOUT\b", re.IGNORECASE)
_INLINE_LAYOUT = re.compile(rb"^\s*LAYOUT\b(.*)$", re.IGNORECASE)  # 'LAYOUT' or 'LAYOUT LINPAG 0 ...'
_LINPAG = re.compile(rb"^\s*(LINPAG|COLLIN)\b", re.IGNORECASE)
_DIGITS_RESULT = re.compile(rb"^\s*DIGITS\s+RESULT\b", re.IGNORECASE)
_APPLICATION = re.compile(rb"^\*(\w+)", re.IGNORECASE)
_WIDE_LINES = b"COLLIN 1000"  # rows wider than COLLIN (130 characters by default) are wrapped


def _layout_options(inline: bytes, no_paging: bool, digits: int | None) -> list[bytes]:
    """Option lines of an inline 'LAYOUT ...' line, minus the ones being replaced."""
    tokens = inline.split()
    groups: list[list[bytes]] = []
    i = 0
    while i < len(tokens):
        keyword = tokens[i].upper()
        if keyword == b"DIGITS":
            j = i + 1
            while j + 1 < len(tokens) and tokens[j].upper() in (b"RESULT", b"COORD", b"AXES"):
                if not (digits and tokens[j].upper() == b"RESULT"):
                    groups.append([b"DIGITS", tokens[j], tokens[j + 1]])
                j += 2
            i = j
            continue
        j = i + 1
        while j < len(tokens) and re.match(rb"^[-+]?\d", tokens[j]):
            j += 1
        if not (no_paging and keyword in (b"LINPAG", b"COLLIN")):
            groups.append(tokens[i:j])
        i = j
    return [b" ".join(group) for group in groups]


def tabula_layout(raw: bytes, no_paging: bool = True, digits: int | None = None) -> bytes | None:
    """Set the layout of every TABULA output block (manual Appendix I.1.1.2, 'Options for Tabular Output').

    ``no_paging`` writes ``LINPAG 0`` (no page header or column header repeated every 66 lines) and
    ``COLLIN 1000`` (no rows wrapped over several lines when a table is wider than 130 characters);
    ``DIGITS RESULT n`` (1-10) sets the significant digits of result values, 4 by default. Output blocks of
    *DESIGN are left alone: their layout cannot be specified (Appendix I.2). Returns the patched bytes, or None
    if nothing had to change.
    """
    wanted = ([b"LINPAG 0", _WIDE_LINES] if no_paging else []) + ([b"DIGITS RESULT %d" % digits] if digits else [])
    if not wanted:
        return None
    lines = raw.splitlines(keepends=True)
    newline = b"\r\n" if b"\r\n" in raw else b"\n"
    out: list[bytes] = []
    application = b""
    i = 0
    while i < len(lines):
        command = _APPLICATION.match(lines[i])
        if command:
            application = command.group(1).upper()
        begin = _BEGIN_OUTPUT.match(lines[i].rstrip(b"\r\n"))
        if not begin:
            out.append(lines[i])
            i += 1
            continue
        end = next((j for j in range(i + 1, len(lines)) if _END_OUTPUT.match(lines[j])), len(lines) - 1)
        block = lines[i:end + 1]
        i = end + 1
        tabula_at = 0 if re.search(rb"\bTABULA\b", begin.group(1), re.IGNORECASE) else next(
            (k for k, line in enumerate(block) if _TABULA.match(line)), None)
        if tabula_at is None or application == b"DESIGN":
            out.extend(block)
            continue
        indent = re.match(rb"^\s*", block[min(tabula_at + 1, len(block) - 1)]).group(0) or b"    "
        layout_lines = [indent + b"  " + w + newline for w in wanted]
        start = next((k for k, line in enumerate(block) if _BEGIN_LAYOUT.match(line)), None)
        if start is not None:
            stop = next((k for k in range(start + 1, len(block)) if _END_LAYOUT.match(block[k])), start)
            inner = [line for line in block[start + 1:stop]
                     if not (no_paging and _LINPAG.match(line)) and not (digits and _DIGITS_RESULT.match(line))]
            block = block[:start + 1] + layout_lines + inner + block[stop:]
        else:
            inline = next((k for k, line in enumerate(block) if _INLINE_LAYOUT.match(line.rstrip(b"\r\n"))), None)
            kept = []
            if inline is not None:
                options = _INLINE_LAYOUT.match(block[inline].rstrip(b"\r\n")).group(1)
                kept = [indent + b"  " + o + newline for o in _layout_options(options, no_paging, digits)]
            new_layout = [indent + b"BEGIN LAYOUT" + newline] + layout_lines + kept + [indent + b"END LAYOUT" + newline]
            if inline is not None:
                block = block[:inline] + new_layout + block[inline + 1:]
            else:
                at = tabula_at + 1
                if tabula_at == 0 and at < len(block) and _TEXT.match(block[at]):
                    at += 1  # TEXT directly follows 'BEGIN OUTPUT TABULA'
                block = block[:at] + new_layout + block[at:]
        out.extend(block)
    patched = b"".join(out)
    return None if patched == raw else patched


@dataclass
class Job:
    id: str
    name: str
    directory: str
    dat: str
    dcf: str
    command: list[str]
    pid: int
    started: float
    state: str = "running"  # running | finished | failed | cancelled
    returncode: int | None = None
    finished: float | None = None
    keep_ff: bool = False
    analysis: str | None = None
    project: str | None = None
    ff: str | None = None  # the FILOS database used by the run
    restart: bool = False  # continued on an existing FILOS database instead of initializing a new one
    # Learning from runs: jobs of the same case (project + analysis, or folder + name) are compared when a
    # problem run is followed by a successful one.
    case: str | None = None
    outcome: str | None = None  # "success" | "problem" once finished; None while running or when cancelled
    problem: dict[str, Any] | None = None
    recommended: list[str] = field(default_factory=list)  # experience ids shown for the problem
    resolves: str | None = None  # the problem job this successful run followed
    resolved_by: str | None = None
    draft: str | None = None  # experience draft written for that problem -> success episode
    permit_id: str | None = None
    authorization_type: str | None = None  # review | baseline | variant
    authorization_id: str | None = None
    quality_recorded: bool = False

    def path(self, suffix: str) -> Path:
        return Path(self.directory) / f"{self.name}{suffix}"

    @property
    def ff_path(self) -> Path:
        return Path(self.ff) if self.ff else self.path(".ff")

    @property
    def log(self) -> Path:
        return self.path(".solver.log")

    def output_files(self) -> dict[str, int]:
        """Solver outputs written since the job started, including names chosen with FILE= in the .dcf."""
        files: dict[str, int] = {}
        for path in Path(self.directory).iterdir():
            if path.suffix.lower() in OUTPUT_SUFFIXES and path.is_file():
                stat = path.stat()
                if stat.st_mtime >= self.started - 2:
                    files[path.name] = stat.st_size
        if self.ff_path.is_file() and self.ff_path.parent != Path(self.directory):
            files[str(self.ff_path)] = self.ff_path.stat().st_size
        return files

    def describe(self) -> dict[str, Any]:
        info = asdict(self)
        end = self.finished or time.time()
        info["elapsed_s"] = round(end - self.started, 1)
        info["files"] = self.output_files()
        return info


class JobManager:
    def __init__(self, registry_dir: Path | None = None, guard: GuardStore | None = None):
        self.registry_dir = registry_dir
        self.guard = guard or GuardStore(Path(registry_dir).parent / "review-gate" if registry_dir else None)
        self._processes: dict[str, subprocess.Popen] = {}
        # Called with (job, manager) once a job has finished or failed, e.g. to learn from problem -> success.
        self.on_finished: list[Callable[[Job, JobManager], None]] = []

    @property
    def _registry(self) -> Path:
        return self.registry_dir or config.state_dir() / "jobs"

    def inputs_dir(self, job: Job) -> Path:
        """Copies of a job's inputs (run .dcf and the .dat model definition), kept for later comparisons."""
        return self._registry / "inputs" / job.id

    def _keep_inputs(self, job: Job, run_dcf: Path, dat: Path) -> None:
        folder = self.inputs_dir(job)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(run_dcf, folder / "run.dcf")
            (folder / "dat_definition.txt").write_text("\n".join(dat_definition(dat)), encoding="utf-8")
        except OSError:
            pass  # comparisons are a convenience; never fail a submission over them

    def save(self, job: Job) -> None:
        self._save(job)

    def _save(self, job: Job) -> None:
        self._registry.mkdir(parents=True, exist_ok=True)
        (self._registry / f"{job.id}.json").write_text(json.dumps(asdict(job), indent=1), encoding="utf-8")

    def _load(self, path: Path) -> Job | None:
        try:
            return Job(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def get(self, job_id: str) -> Job:
        job = self._load(self._registry / f"{job_id}.json")
        if job is None:
            raise JobError(f"unknown job id {job_id!r}; call diana_job_status without an id to list jobs")
        return self.refresh(job)

    def list(self, limit: int = 20) -> list[Job]:
        paths = sorted(self._registry.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        jobs = [job for job in (self._load(p) for p in paths[:limit]) if job is not None]
        return [self.refresh(job) for job in jobs]

    def submit(
        self,
        dat: Path,
        dcf: Path,
        *,
        name: str | None = None,
        threads: int | None = None,
        overwrite: bool = False,
        keep_ff: bool = False,
        tb_no_paging: bool = True,
        tb_digits: int | None = None,
        ff_path: Path | None = None,
        search_dirs: list[Path] | None = None,
        solver: list[str] | None = None,
        analysis: str | None = None,
        project: str | None = None,
        permit_id: str | None = None,
    ) -> Job:
        """Start diana.exe in the folder of ``dat``; outputs are named after ``name`` (default: the .dat stem).

        ``ff_path`` continues on an existing FILOS database (restart, RESTOR, a stand-alone *REDUCT/*ELIQUE/*ECREEP
        after the analysis that created it): the .dcf is then used as written and the database is kept.
        Relative input files the .dcf/.dat refer to (user DLLs, *INPUT READ FILE=, IMPORT) are copied into the
        job folder from the folders of the .dcf/.dat or ``search_dirs``; missing ones stop the submission.
        ``tb_no_paging`` / ``tb_digits`` set the layout of TABULA outputs in the run copy of the .dcf.
        ``solver`` is the command prefix, by default the diana.exe of the default installation. ``permit_id`` is a
        one-use authorization issued after GUI review or an approved parameter-study self-check; every production
        submission requires one and the common process-launch layer consumes it immediately before ``Popen``.
        """
        if tb_digits is not None and not 1 <= tb_digits <= 10:
            raise JobError("tb_digits must be between 1 and 10 (DIANA formats at most ten digits)")
        dat, dcf = dat.resolve(), dcf.resolve()
        for path in (dat, dcf):
            if not path.is_file():
                raise JobError(f"file not found: {path}")
        try:
            self.guard.validate_permit(permit_id, dat, dcf)
        except GateError as exc:
            raise JobError(str(exc)) from exc
        directory = dat.parent
        name = name or dat.stem
        restart = ff_path is not None
        ff = ff_path.resolve() if ff_path is not None else directory / f"{name}.ff"
        if restart and not ff.is_file():
            raise JobError(f"FILOS database to continue on not found: {ff}")
        for other in self.list(limit=200):
            if other.state == "running" and (
                (Path(other.directory) == directory and other.name == name) or other.ff_path == ff
            ):
                raise JobError(f"job {other.id} is still running with the same outputs or FILOS database")
        existing = [directory / f"{name}{s}" for s in OUTPUT_SUFFIXES
                    if (directory / f"{name}{s}").exists() and not (restart and directory / f"{name}{s}" == ff)]
        if existing and not overwrite:
            raise JobError(
                "outputs of an earlier run exist: " + ", ".join(p.name for p in existing)
                + ". Pass overwrite=True to delete them (DIANA would otherwise append to the old .dnb)."
            )
        for path in existing:
            path.unlink()

        original = dcf.read_bytes()
        patched = original if restart else (standalone_dcf(original) or original)
        patched = tabula_layout(patched, no_paging=tb_no_paging, digits=tb_digits) or patched
        self._stage_references(original, dat, directory, [dcf.parent, dat.parent, *(search_dirs or [])])
        run_dcf = dcf
        if patched != original:
            run_dcf = directory / f"{name}.run.dcf"
            run_dcf.write_bytes(patched)

        solver_argv = list(solver) if solver else [str(config.default_install().solver)]
        command = config.launch_argv(solver_argv[0], *solver_argv[1:])
        if threads:
            command += ["-n", str(int(threads))]
        ff_argument = os.path.relpath(ff, directory) if ff.drive.lower() == directory.drive.lower() else str(ff)
        command += [name, os.path.relpath(dat, directory), os.path.relpath(run_dcf, directory), ff_argument]

        try:
            permit = self.guard.consume_permit(permit_id, dat, dcf)
        except GateError as exc:
            raise JobError(str(exc)) from exc
        with open(directory / f"{name}.solver.log", "wb") as log:
            process = subprocess.Popen(
                command, cwd=directory, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=config.launch_env(**JOB_ENVIRONMENT), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        job = Job(
            id=time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
            name=name, directory=str(directory), dat=str(dat), dcf=str(run_dcf), command=command,
            pid=process.pid, started=time.time(), keep_ff=keep_ff or restart, analysis=analysis,
            project=project, ff=str(ff), restart=restart,
            case=f"{project}|{analysis}" if project and analysis else f"{directory}|{name}",
            permit_id=permit["id"], authorization_type=permit.get("source_type"),
            authorization_id=permit.get("source_id"),
        )
        self._processes[job.id] = process
        self._keep_inputs(job, run_dcf, dat)
        self._save(job)
        self.guard.record_submission(permit, job.id)
        return job

    @staticmethod
    def _stage_references(dcf_bytes: bytes, dat: Path, directory: Path, search_dirs: list[Path]) -> None:
        sources = [dcf_bytes]
        if dat.stat().st_size < 200 * 1024 * 1024:  # IMPORT appears in the .dat; skip scanning huge meshes
            sources.append(dat.read_bytes())
        missing = []
        for name in dict.fromkeys(n for raw in sources for n in referenced_files(raw)):
            reference = Path(name)
            if reference.is_absolute():
                if not reference.exists():
                    missing.append(name)
                continue
            target = directory / reference
            if target.exists():
                continue
            source = next((d / reference for d in search_dirs if (d / reference).is_file()), None)
            if source is None:
                missing.append(name)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if missing:
            raise JobError(
                f"input files referenced by the .dcf/.dat were not found: {missing}; looked in {directory} and "
                + ", ".join(str(d) for d in search_dirs)
            )

    def _running(self, job: Job) -> bool:
        process = self._processes.get(job.id)
        if process is not None:
            job.returncode = process.poll()
            return job.returncode is None
        # Started by an earlier server process: trust the pid only while it still is diana.exe.
        image = process_image(job.pid) if pid_alive(job.pid) else None
        return bool(image) and Path(image).name.lower() == "diana.exe"

    def refresh(self, job: Job) -> Job:
        if job.state != "running" or self._running(job):
            return job
        job.finished = time.time()
        out = job.path(".out")
        parsed = parse_out(out) if out.is_file() else None
        completed = parsed.completed if parsed is not None else False
        job.state = "finished" if completed and job.returncode in (0, None) else "failed"
        job.outcome, job.problem = classify(out)
        self._cleanup(job)
        if not job.quality_recorded:
            summary = parsed.summary() if parsed is not None else {}
            checks = [
                "DIANA 正常结束" if completed else "DIANA 未正常结束",
                "求解器无错误" if not summary.get("errors") else f"求解器错误：{summary['errors']}",
                "全部增量步收敛" if not summary.get("not_converged_steps") else
                f"未收敛步：{summary['not_converged_steps']}",
            ]
            quality_passed = bool(completed and not summary.get("errors") and not summary.get("not_converged_steps"))
            # Do not silently lose this evidence: if persistence fails the job remains recorded as running and the
            # next status poll retries the transition instead of trusting an unaudited baseline.
            self.guard.record_quality(job.id, quality_passed, checks, job.permit_id)
            job.quality_recorded = True
        self._save(job)
        for callback in list(self.on_finished):
            try:
                callback(job, self)
            except Exception:  # learning must never break job handling
                pass
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.state != "running":
            raise JobError(f"job {job_id} is not running (state: {job.state})")
        kill_tree(job.pid)
        kill_processes_in(Path(job.directory))  # the solver segment survives a kill of the launcher under Wine
        job.state, job.finished = "cancelled", time.time()
        self._cleanup(job)
        if not job.quality_recorded:
            self.guard.record_quality(job.id, False, ["作业被取消，未完成质量检查"], job.permit_id)
            job.quality_recorded = True
        self._save(job)
        return job

    def _cleanup(self, job: Job) -> None:
        self._processes.pop(job.id, None)
        if not job.keep_ff and not job.restart:
            try:
                job.ff_path.unlink(missing_ok=True)  # the FILOS scratch database, often hundreds of MB
            except OSError:
                pass  # still locked by a dying segment process; harmless
