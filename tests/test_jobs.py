import sys
import time
from pathlib import Path

import pytest

from diana_mcp.jobs import JobError, JobManager, standalone_dcf, tabula_layout

FAKE = [sys.executable, str(Path(__file__).parent / "fake_diana.py")]


def wait(manager, job, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        job = manager.get(job.id)
        if job.state != "running":
            return job
        time.sleep(0.2)
    raise TimeoutError(job.id)


def case(tmp_path, dcf_text="*LINSTA\n  SOLVE PARDIS\n*END\n"):
    dat = tmp_path / "beam.dat"
    dcf = tmp_path / "beam.dcf"
    dat.write_text("'COORDINATES'\n'END'\n", encoding="utf-8")
    dcf.write_text(dcf_text, encoding="utf-8")
    return dat, dcf


def submit(manager, dat, dcf, **kwargs):
    """Unit-test submission through the same one-use review permit required in production."""
    snapshot = {"project": {"name": dat.stem}, "mesh": {"nodes": 2, "elements": 1}}
    item = manager.guard.create_review(
        project=str(dat.with_suffix(".dpf")), analysis="test", snapshot=snapshot, dat=dat, dcf=dcf,
        diana_version="test", units={}, validation_issues=[], changes=[],
    )
    manager.guard.gui_decide(item["id"], "approve_once", "test fixture")
    permit = manager.guard.permit_review(
        item["id"], item["state_fingerprint"], item["dat_sha256"], {"passed": True}
    )
    return manager.submit(dat, dcf, permit_id=permit["id"], **kwargs)


def test_standalone_dcf_adds_header_and_end():
    patched = standalone_dcf("﻿*LINSTA LABEL=\"线性\"\n  SOLVE PARDIS\n".encode("utf-8"))
    assert patched.startswith(b"*FILOS\nINITIA\n*INPUT\n*LINSTA")
    assert patched.rstrip().endswith(b"*END")
    assert "线性".encode("utf-8") in patched
    assert standalone_dcf(patched) is None


def test_standalone_dcf_goes_after_comments_and_control_commands():
    raw = b": test case\r\n: second comment\r\nNUMTHR 4\r\nERRORS .CPF MF=20\r\n*LINSTA\r\n*END\r\n"
    patched = standalone_dcf(raw).decode()
    assert patched.splitlines()[:7] == [": test case", ": second comment", "NUMTHR 4", "ERRORS .CPF MF=20",
                                         "*FILOS", "INITIA", "*INPUT"]
    assert "\r\n" in patched and patched.count("*END") == 1


def test_standalone_dcf_does_not_repeat_an_existing_input_command():
    patched = standalone_dcf(b"*INPUT\nREAD FILE=extra.dat\n*NONLIN\n*END\n")
    assert patched == b"*FILOS\nINITIA\n*INPUT\nREAD FILE=extra.dat\n*NONLIN\n*END\n"


def test_tabula_layout_widens_lines_and_skips_design_blocks():
    raw = (b"*LINSTA\nBEGIN OUTPUT\n  TABULA\n  DISPLA\nEND OUTPUT\n"
           b"*DESIGN\nBEGIN OUTPUT\n  TABULA\n  STRESS\nEND OUTPUT\n*END\n")
    patched = tabula_layout(raw).decode()
    linsta, design = patched.split("*DESIGN")
    assert "LINPAG 0" in linsta and "COLLIN 1000" in linsta
    assert "LAYOUT" not in design


DIANAIE_DCF = """*NONLIN LABEL="Structural nonlinear"
  BEGIN OUTPUT
    TEXT 输出
    BINARY
    SELECT STEPS ALL /
  END OUTPUT
  BEGIN OUTPUT
    TEXT "输出 2"
    TABULA
    LAYOUT
    SELECT STEPS ALL /
    DISPLA TOTAL TRANSL GLOBAL X Y Z
  END OUTPUT
*END
"""


def test_tabula_layout_replaces_bare_layout_and_leaves_binary_blocks():
    patched = tabula_layout(DIANAIE_DCF.encode("utf-8"), no_paging=True, digits=8).decode("utf-8")
    assert ("    TABULA\n    BEGIN LAYOUT\n      LINPAG 0\n      COLLIN 1000\n      DIGITS RESULT 8\n    END LAYOUT\n"
            "    SELECT STEPS") in patched
    assert patched.count("LINPAG") == 1  # the BINARY block is untouched
    assert "TEXT 输出" in patched
    assert tabula_layout(patched.encode("utf-8"), no_paging=True, digits=8) is None  # idempotent


def test_tabula_layout_updates_an_existing_layout_block_and_keeps_crlf():
    raw = (b"*LINSTA\r\nBEGIN OUTPUT TABULA FILE=out\r\n  TEXT \"t\"\r\n  BEGIN LAYOUT\r\n    LINPAG 66\r\n"
           b"    COLLIN 200\r\n    DIGITS RESULT 4\r\n  END LAYOUT\r\n  DISPLA\r\nEND OUTPUT\r\n*END\r\n")
    patched = tabula_layout(raw, no_paging=True, digits=6)
    assert b"LINPAG 66" not in patched and b"DIGITS RESULT 4" not in patched
    assert b"LINPAG 0\r\n" in patched and b"DIGITS RESULT 6\r\n" in patched
    assert b"COLLIN 1000\r\n" in patched and b"COLLIN 200" not in patched
    assert b"\n" not in patched.replace(b"\r\n", b"")


def test_tabula_layout_expands_the_inline_form_dianaie_writes_for_a_single_option():
    raw = b"  BEGIN OUTPUT\n    TABULA\n    LAYOUT LINPAG 0\n    SELECT MODES ALL /\n  END OUTPUT\n"
    patched = tabula_layout(raw, no_paging=True).decode()
    assert patched.count("LINPAG") == 1
    assert "    BEGIN LAYOUT\n      LINPAG 0\n      COLLIN 1000\n    END LAYOUT\n    SELECT MODES" in patched

    raw = b"BEGIN OUTPUT\n  TABULA\n  LAYOUT COLLIN 200 DIGITS RESULT 6 COORD 5 COMBIN\n  DISPLA\nEND OUTPUT\n"
    lines = tabula_layout(raw, no_paging=True, digits=8).decode().splitlines()
    assert lines[2:9] == ["  BEGIN LAYOUT", "    LINPAG 0", "    COLLIN 1000", "    DIGITS RESULT 8",
                          "    DIGITS COORD 5", "    COMBIN", "  END LAYOUT"]


def test_tabula_layout_inserts_after_text_when_the_device_is_on_the_begin_line():
    raw = b"*LINSTA\nBEGIN OUTPUT TABULA\n  TEXT \"t\"\n  DISPLA\nEND OUTPUT\n*END\n"
    patched = tabula_layout(raw).decode()
    assert patched.splitlines()[2:7] == ['  TEXT "t"', "  BEGIN LAYOUT", "    LINPAG 0", "    COLLIN 1000",
                                         "  END LAYOUT"]
    assert tabula_layout(raw, no_paging=False) is None


def test_successful_job_finishes_and_drops_the_ff_file(tmp_path):
    manager = JobManager(tmp_path / "registry")
    dat, dcf = case(tmp_path)
    job = submit(manager, dat, dcf, solver=FAKE, threads=2)
    assert job.command[len(FAKE):] == ["-n", "2", "beam", "beam.dat", "beam.run.dcf", "beam.ff"]
    assert (tmp_path / "beam.run.dcf").read_bytes().startswith(b"*FILOS")
    assert dcf.read_text(encoding="utf-8").startswith("*LINSTA")  # original untouched

    job = wait(manager, job)
    assert job.state == "finished" and job.returncode == 0
    assert not (tmp_path / "beam.ff").exists()
    assert set(job.describe()["files"]) == {"beam.out", "beam.tb", "beam.dnb"}


def test_referenced_user_library_is_copied_or_reported(tmp_path):
    manager = JobManager(tmp_path / "registry")
    run = tmp_path / "run"
    run.mkdir()
    sources = tmp_path / "sources"
    sources.mkdir()
    dat, dcf = case(run, '*FORTRAN\n  USE "usrmat.dll"\n*NONLIN\n*END\n')
    with pytest.raises(JobError, match="usrmat.dll"):
        submit(manager, dat, dcf, solver=FAKE)
    (sources / "usrmat.dll").write_bytes(b"MZ")
    job = wait(manager, submit(manager, dat, dcf, solver=FAKE, search_dirs=[sources]))
    assert (run / "usrmat.dll").read_bytes() == b"MZ" and job.state == "finished"


def test_restart_continues_on_an_existing_database_and_keeps_it(tmp_path):
    manager = JobManager(tmp_path / "registry")
    dat, dcf = case(tmp_path, "*NONLIN\n  BEGIN EXECUT\n  END EXECUT\n*END\n")
    database = tmp_path / "previous.ff"
    database.write_bytes(b"FILOS")
    job = submit(manager, dat, dcf, solver=FAKE, ff_path=database)
    assert job.command[-1] == "previous.ff" and job.dcf == str(dcf)  # used as written: no *FILOS INITIA
    job = wait(manager, job)
    assert job.restart and database.exists()


def test_state_survives_a_new_manager(tmp_path):
    first = JobManager(tmp_path / "registry")
    dat, dcf = case(tmp_path)
    job = wait(first, submit(first, dat, dcf, solver=FAKE))
    again = JobManager(tmp_path / "registry").get(job.id)
    assert again.state == "finished"
    assert [j.id for j in JobManager(tmp_path / "registry").list()] == [job.id]


def test_failed_run(tmp_path):
    manager = JobManager(tmp_path / "registry")
    dat, dcf = case(tmp_path, "*LINSTA FAIL\n*END\n")
    job = wait(manager, submit(manager, dat, dcf, solver=FAKE))
    assert job.state == "failed" and job.returncode == 1
    assert "segment aborted" in job.log.read_text()


def test_existing_outputs_need_overwrite(tmp_path):
    manager = JobManager(tmp_path / "registry")
    dat, dcf = case(tmp_path)
    (tmp_path / "beam.out").write_text("old run")
    with pytest.raises(JobError, match="overwrite=True"):
        submit(manager, dat, dcf, solver=FAKE)
    job = wait(manager, submit(manager, dat, dcf, solver=FAKE, overwrite=True))
    assert job.state == "finished"


def test_cancel_kills_the_solver(tmp_path):
    manager = JobManager(tmp_path / "registry")
    dat, dcf = case(tmp_path, "*LINSTA SLOW\n*END\n")
    job = submit(manager, dat, dcf, solver=FAKE, keep_ff=True)
    with pytest.raises(JobError, match="still running"):
        submit(manager, dat, dcf, solver=FAKE, overwrite=True)
    time.sleep(1)
    job = manager.cancel(job.id)
    assert job.state == "cancelled"
    assert (tmp_path / "beam.ff").exists()  # keep_ff
    with pytest.raises(JobError, match="not running"):
        manager.cancel(job.id)


def test_cancel_also_kills_the_solver_segment(tmp_path, monkeypatch):
    """Under Wine diana.exe starts the segment executable as a separate host process that a kill of the launcher
    does not reach, so cancel() sweeps the job folder as well."""
    from diana_mcp import jobs as jobs_module

    swept = []
    monkeypatch.setattr(jobs_module, "kill_processes_in", lambda directory: swept.append(Path(directory)) or 1)
    manager = JobManager(tmp_path / "registry")
    dat, dcf = case(tmp_path, "*LINSTA SLOW" + chr(10) + "*END" + chr(10))
    job = submit(manager, dat, dcf, solver=FAKE)
    time.sleep(1)
    job = manager.cancel(job.id)

    assert job.state == "cancelled"
    assert swept == [Path(job.directory)]
