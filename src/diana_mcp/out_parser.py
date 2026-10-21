"""Summary of a DIANA solver log (.out): analysis blocks, load steps, convergence, messages, termination.

Verified DIANA 10.5 patterns (own runs, the user's runs, manual Appendix C and the Test suite):
     4:*NONLIN LABEL="Structural nonlinear"                          <- echoed .dcf commands
     8:      STEPS EXPLIC SIZES 0.25(12)                             <- planned steps
 /DIANA/AP/NL41    16:52:17      1.44-CPU     0.72-IO      120-FA   BEGIN   <- segment line ('99.-FA' occurs)
 STEP      7 INITIATED:
 STEP      7 TERMINATED, CONVERGENCE AFTER   4 ITERATIONS           <- also '1 ITERATION'
 STEP      9 TERMINATED, NO CONVERGENCE AFTER  20 ITERATIONS        <- the analysis may continue or stop
 STEP      9 TERMINATED                                             <- nothing more: divergence, abort follows
  TOTAL LOAD FACTOR:  LOADING( 1) *   1.750E+00                     <- printed after the step ends
  END TIME:   3.000E-02                                              <- instead, in time-stepped analyses
 SEVERITY  : FATAL | ABORT | ERROR | WARNING
 ERROR CODE: /DIANA/AP/LB40/0053                                    <- also for warnings
 ERRORMSG.F: Divergence occurred, iteration method failed.          <- may continue on the next lines
 Unexpected input CAUCHY / Last line was ... / SCAN ERROR: ...      <- .dcf syntax errors (no SEVERITY block)
 /DIANA/DC/END     16:34:03      4.81-CPU     3.03-IO      420-FA   STOP     <- normal end
 DIANA-JOB ABORTED                                                  <- abnormal end
          4 EIGENVALUES FOUND AFTER    1 ITERATIONS                 <- eigenvalue analyses, then
     EIGEN-FREQUENCIES:          (or 'EIGEN-FREQUENCIES (HZ) :', 'EIGEN-VALUES:' for buckling)
     0.37964E+02(  1)    0.11422E+03(  2)                           <- value(mode), D exponents occur
     MODE      FREQUENCY      EFF.MASS TY     PERCENTAGE      CUM.PERCENT.
        1     0.37964E+02     0.58914E+03     0.76711E+02     0.76711E+02
 FINAL FACTOR OF SAFETY: 1.16250                                    <- strength reduction (*REDUCT)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[EeDd][-+]?\d+)?"
_PHASE = re.compile(r"^\s*/DIANA/(\w+)/(\w+)\s+(\d\d:\d\d:\d\d)\s+(\S+)-CPU\s+(\S+)-IO\s+(\S+)-FA\s+(\S+)")
_END_MARK = re.compile(r"^\s*/DIANA/DC/END\b.*\bSTOP\b")
_ECHO = re.compile(r"^\s*\d+:\s?(\*\w+.*)$")
_ECHO_SIZES = re.compile(r"^\s*\d+:\s*STEPS\s+EXPLIC\s+SIZES\s+(.*)$", re.IGNORECASE)
_ELAPSED = re.compile(r"Elapsed time:\s*(.*?)\s*$", re.IGNORECASE)
_DURATION = re.compile(r"([\d.]+)\s*(hour|minute|second)s?", re.IGNORECASE)
_STOPPED = re.compile(r"EXECUTION IS STOPPED", re.IGNORECASE)
_STEP_START = re.compile(r"^\s*STEP\s+(\d+)\s+INITIATED", re.IGNORECASE)
_STEP_END = re.compile(r"^\s*STEP\s+(\d+)\s+TERMINATED\b,?\s*(.*)$", re.IGNORECASE)
_ITERATIONS = re.compile(r"AFTER\s+(\d+)\s+ITERATION", re.IGNORECASE)
_LOAD_FACTOR = re.compile(r"TOTAL\s+LOAD\s+FACTOR\s*:(.*)$", re.IGNORECASE)
_END_TIME = re.compile(rf"^\s*END\s+TIME\s*:\s*({_NUMBER})", re.IGNORECASE)
_FLOAT = re.compile(r"[-+]?\d*\.\d+(?:[EeDd][-+]?\d+)?|[-+]?\d+[EeDd][-+]?\d+")
_SEVERITY = re.compile(r"^\s*SEVERITY\s*:\s*(\w+)", re.IGNORECASE)
_CODE = re.compile(r"^\s*ERROR CODE\s*:\s*(\S+)", re.IGNORECASE)
_MESSAGE = re.compile(r"^\s*ERRORMSG\.\w\s*:\s*(.*)$", re.IGNORECASE)
_SYNTAX = re.compile(r"^\s*(Unexpected input\b.*|Last line was\b.*|SCAN ERROR\s*:.*)$", re.IGNORECASE)
_ABORTED = re.compile(r"DIANA-JOB ABORTED", re.IGNORECASE)
_EIGEN_FOUND = re.compile(r"^\s*(\d+)\s+EIGENVALUES FOUND AFTER\s+(\d+)\s+ITERATION", re.IGNORECASE)
_EIGEN_LIST = re.compile(r"^\s*EIGEN-(FREQUENCIES|VALUES)\b[^:]*:\s*$", re.IGNORECASE)
_EIGEN_ITEM = re.compile(rf"({_NUMBER})\(\s*(\d+)\)")
_MODE_TABLE = re.compile(r"^\s*MODE\s{2,}FREQUENCY\b", re.IGNORECASE)
_MODE_ROW = re.compile(r"^\s*(\d+)((?:\s+[-+]?\d*\.\d+(?:[EeDd][-+]?\d+)?)+)\s*$")
_SAFETY = re.compile(rf"FINAL FACTOR OF SAFETY\s*:\s*({_NUMBER})", re.IGNORECASE)
_BAD_SEVERITIES = {"FATAL", "ABORT", "ERROR", "SYNTAX"}


@dataclass
class Step:
    number: int
    analysis: str | None = None
    block: int = 0  # increases when step numbering restarts (phases, stages, following analyses)
    load_factor: float | None = None
    time: float | None = None  # END TIME of time-stepped analyses
    iterations: int | None = None
    converged: bool | None = None  # None while the step is still running
    status: str = ""  # text after TERMINATED, e.g. 'CONVERGENCE AFTER 4 ITERATIONS'
    attempts: int = 1  # >1 when the step was re-initiated, e.g. by automatic step-size reduction


@dataclass
class Message:
    severity: str
    code: str = ""
    text: str = ""


@dataclass
class OutSummary:
    path: str
    commands: list[str] = field(default_factory=list)
    planned_steps: int | None = None
    phases: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    terminated: bool = False  # normal end marker '/DIANA/DC/END ... STOP'
    aborted: bool = False  # 'DIANA-JOB ABORTED'
    stopped: str | None = None  # 'EXECUTION IS STOPPED, BUT CAN BE CONTINUED': the reason printed before it
    elapsed_s: float | None = None
    cpu_s: float | None = None
    last_line: str = ""
    eigenvalues_found: int | None = None
    eigen_lists: list[dict] = field(default_factory=list)  # {"kind": "FREQUENCIES"|"VALUES", "values": {mode: v}}
    mode_tables: list[dict] = field(default_factory=list)  # {"columns": [...], "rows": [[mode, freq, ...], ...]}
    factor_of_safety: float | None = None

    @property
    def modes(self) -> list[dict]:
        """Mode number and frequency (or eigenvalue) of every eigenmode reported in the log."""
        seen: dict[int, dict] = {}
        for table in self.mode_tables:
            for row in table["rows"]:
                seen.setdefault(int(row[0]), {"mode": int(row[0]), "frequency": row[1]})
        for listing in self.eigen_lists:
            key = "frequency" if listing["kind"].upper().startswith("FREQ") else "eigenvalue"
            for mode, value in listing["values"].items():
                seen.setdefault(mode, {"mode": mode}).setdefault(key, value)
        return [seen[m] for m in sorted(seen)]

    @property
    def errors(self) -> list[Message]:
        return [m for m in self.messages if m.severity.upper() in _BAD_SEVERITIES]

    @property
    def warnings(self) -> list[Message]:
        return [m for m in self.messages if m.severity.upper() not in _BAD_SEVERITIES]

    @property
    def completed(self) -> bool:
        return self.terminated and not self.aborted and not self.errors

    def summary(self) -> dict:
        not_converged = [s.number for s in self.steps if s.converged is False]
        last = self.steps[-1] if self.steps else None
        last_converged = next((s for s in reversed(self.steps) if s.converged), None)
        warning_codes: dict[str, int] = {}
        for message in self.warnings:
            key = message.code or message.severity
            warning_codes[key] = warning_codes.get(key, 0) + 1
        if self.terminated:
            end = "normal"
        elif self.aborted:
            end = "aborted"
        elif self.stopped:
            end = f"stopped by DIANA: {self.stopped} (can be continued from the .ff)"
        else:
            end = "unfinished (still running, or stopped from outside)"
        progress = None
        if self.planned_steps and self.steps:
            done = sum(1 for s in self.steps if s.converged is not None)
            progress = f"{done}/{self.planned_steps} steps"
        blocks = len({s.block for s in self.steps})
        info = {
            "completed": self.completed,
            "end": end,
            "commands": self.commands,
            "steps": len(self.steps),
            "planned_steps": self.planned_steps,
            "progress": progress,
            "step_blocks": blocks if blocks > 1 else None,
            "retried_steps": sum(1 for s in self.steps if s.attempts > 1) or None,
            "last_step": {k: v for k, v in last.__dict__.items() if v not in (None, "") and k != "attempts"}
            if last else None,
            "last_converged_step": {k: v for k, v in (("number", last_converged.number),
                                                        ("load_factor", last_converged.load_factor),
                                                        ("time", last_converged.time)) if v is not None}
            if last_converged else None,
            "not_converged_steps": not_converged[:50] + (["..."] if len(not_converged) > 50 else []),
            "eigenvalues_found": self.eigenvalues_found,
            "modes": self.modes[:50],
            "mode_tables": self.mode_tables if sum(len(t["rows"]) for t in self.mode_tables) <= 200 else
            "too large to inline; see the .out file",
            "factor_of_safety": self.factor_of_safety,
            "errors": [m.__dict__ for m in self.errors[:10]],
            "warnings": len(self.warnings),
            "warning_codes": warning_codes,
            "elapsed_s": self.elapsed_s,
            "cpu_s": self.cpu_s,
            "last_phase": self.phases[-1] if self.phases else None,
            "last_line": self.last_line,
        }
        return {k: v for k, v in info.items() if v not in (None, [], "", {})}


def _float(text: str) -> float | None:
    try:
        return float(text.replace("D", "E").replace("d", "e"))
    except ValueError:
        return None


def planned_step_count(sizes: str) -> int | None:
    """Number of steps in 'STEPS EXPLIC SIZES 0.01(10) 0.05(4) 0.1' -> 15."""
    count = 0
    for token in sizes.split():
        repeat = re.fullmatch(rf"{_NUMBER}\((\d+)\)", token)
        if repeat:
            count += int(repeat.group(1))
        elif re.fullmatch(_NUMBER, token):
            count += 1
        else:
            break
    return count or None


def parse_out(path: Path) -> OutSummary:
    summary = OutSummary(path=str(path))
    current: Step | None = None
    last_ended: Step | None = None
    analysis: str | None = None
    message: Message | None = None
    message_open = False  # ERRORMSG text may continue on the following lines
    table: dict | None = None
    eigen: dict | None = None
    block = 0
    planned = 0
    previous_text = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.replace("\x00", "").rstrip()  # killed runs can leave NUL padding at the end
            if not line.strip():
                message, message_open, table, eigen = None, False, None, None
                continue
            previous_text, summary.last_line = summary.last_line, line.strip()

            if eigen is not None:
                items = _EIGEN_ITEM.findall(line)
                if items:
                    for value, mode in items:
                        eigen["values"][int(mode)] = _float(value)
                    continue
                eigen = None
            listing = _EIGEN_LIST.match(line)
            if listing:
                eigen = {"kind": listing.group(1).upper(), "values": {}}
                summary.eigen_lists.append(eigen)
                continue
            if _MODE_TABLE.match(line):
                table = {"columns": re.split(r"\s{2,}", line.strip()), "rows": []}
                summary.mode_tables.append(table)
                continue
            if table is not None:
                row = _MODE_ROW.match(line)
                if row:
                    table["rows"].append([int(row.group(1))] + [_float(v) for v in row.group(2).split()])
                    continue
                table = None

            severity = _SEVERITY.match(line)
            if severity:
                # Text may follow directly, without ERROR CODE / ERRORMSG, e.g. '50 WARNINGS SEEN'.
                message, message_open = Message(severity.group(1).upper()), True
                summary.messages.append(message)
                continue
            if message is not None:
                code = _CODE.match(line)
                if code:
                    message.code = code.group(1)
                    continue
                text = _MESSAGE.match(line)
                if text:
                    message.text = (message.text + " " + text.group(1).strip()).strip()
                    message_open = True
                    continue
                if message_open and not _PHASE.match(line) and not _ABORTED.search(line):
                    message.text = (message.text + " " + line.strip()).strip()
                    continue
                message, message_open = None, False
            syntax = _SYNTAX.match(line)
            if syntax:
                summary.messages.append(Message("SYNTAX", text=syntax.group(1).strip()))
                continue

            found = _EIGEN_FOUND.match(line)
            if found:
                summary.eigenvalues_found = int(found.group(1))
                continue
            safety = _SAFETY.search(line)
            if safety:
                summary.factor_of_safety = _float(safety.group(1))
                continue
            sizes = _ECHO_SIZES.match(line)
            if sizes:
                planned += planned_step_count(sizes.group(1)) or 0
                summary.planned_steps = planned or None
                continue
            echo = _ECHO.match(line)
            if echo:
                command = echo.group(1).strip()
                if command.split()[0].upper() not in ("*END", "*FILOS", "*INPUT"):
                    summary.commands.append(command)
                    analysis = command.split()[0].lstrip("*").upper()
                continue
            phase = _PHASE.match(line)
            if phase:
                summary.phases.append(f"{phase.group(1)}/{phase.group(2)} {phase.group(7)}")
                cpu = _float(phase.group(4).rstrip("."))
                if cpu is not None:
                    summary.cpu_s = cpu
                if _END_MARK.match(line):
                    summary.terminated = True
                continue
            if _END_MARK.match(line):
                summary.terminated = True
                continue
            if _ABORTED.search(line):
                summary.aborted = True
                continue
            if _STOPPED.search(line):
                reason = previous_text.rstrip(".") if previous_text and not _STOPPED.search(previous_text) else ""
                summary.stopped = reason or line.strip()
                continue
            elapsed = _ELAPSED.search(line)
            if elapsed:
                parts = _DURATION.findall(elapsed.group(1))
                seconds = sum(float(v) * {"hour": 3600, "minute": 60, "second": 1}[u.lower()] for v, u in parts)
                summary.elapsed_s = seconds if parts else summary.elapsed_s
                continue
            start = _STEP_START.match(line)
            if start:
                number = int(start.group(1))
                previous = summary.steps[-1] if summary.steps else None
                if previous is not None and previous.number == number and previous.block == block:
                    previous.attempts += 1  # re-initiated, e.g. by automatic step-size reduction
                    previous.converged, previous.iterations, previous.status = None, None, ""
                    current = previous
                    continue
                if previous is not None and number < previous.number:
                    block += 1  # numbering restarted: next phase, stage or analysis block
                current = Step(number, analysis=analysis, block=block)
                summary.steps.append(current)
                continue
            end = _STEP_END.match(line)
            if end:
                number, status = int(end.group(1)), end.group(2).strip().upper()
                if current is None or current.number != number:
                    current = Step(number, analysis=analysis, block=block)
                    summary.steps.append(current)
                current.status = status
                current.converged = status.startswith("CONVERGENCE")
                iterations = _ITERATIONS.search(status)
                if iterations:
                    current.iterations = int(iterations.group(1))
                last_ended, current = current, None
                continue
            factor = _LOAD_FACTOR.search(line)
            if factor:
                values = _FLOAT.findall(factor.group(1))
                target = last_ended or current
                if values and target is not None:
                    target.load_factor = _float(values[-1])
                continue
            end_time = _END_TIME.match(line)
            if end_time and last_ended is not None:
                last_ended.time = _float(end_time.group(1))
    return summary


def tail(path: Path, lines: int = 20) -> list[str]:
    """Last non-empty lines of a (possibly huge, possibly still growing) text file."""
    lines = max(1, min(int(lines), 2000))
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        chunk = 4096 * lines
        while True:
            f.seek(max(0, size - chunk))
            text = f.read().replace(b"\x00", b"").decode("utf-8", errors="replace")
            found = [l.rstrip() for l in text.splitlines() if l.strip()]
            if len(found) > lines or chunk >= size:  # one extra line: the first may be cut mid-line
                return found[-lines:]
            chunk *= 4
