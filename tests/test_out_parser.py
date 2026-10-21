from pathlib import Path

from diana_mcp.out_parser import parse_out, tail

DATA = Path(__file__).parent / "data"


def test_linear_run_completes():
    summary = parse_out(DATA / "linsta_beam.out")
    assert summary.completed and summary.terminated and not summary.aborted
    assert summary.commands == ['*LINSTA LABEL="Structural linear static"']
    assert summary.steps == [] and summary.messages == []
    assert summary.elapsed_s == 10.0 and summary.cpu_s == 4.81


def test_nonlinear_run_that_diverged():
    summary = parse_out(DATA / "nonlin_beam_aborted.out")
    assert not summary.completed and summary.aborted and not summary.terminated
    assert [(s.number, s.load_factor, s.iterations, s.converged) for s in summary.steps] == [
        (1, 0.25, 0, True), (2, 0.5, 0, True), (3, 0.75, 0, True), (4, 1.0, 0, True), (5, 1.25, 0, True),
        (6, 1.5, 1, True), (7, 1.75, 4, True), (8, 2.0, 9, True), (9, 2.25, None, False),
    ]
    assert [(e.severity, e.code) for e in summary.errors] == [
        ("FATAL", "/DIANA/AP/LB40/0053"), ("ABORT", "/DIANA/LB/ER50/0098"),
    ]
    assert summary.errors[0].text.startswith("Divergence occurred, iteration method failed. Use smaller load steps")
    assert summary.errors[0].text.endswith("Restart from a saved FILOS file or rerun the complete job.")
    assert summary.planned_steps == 12 and summary.summary()["progress"] == "9/12 steps"
    brief = summary.summary()
    assert brief["last_converged_step"] == {"number": 8, "load_factor": 2.0}
    assert brief["not_converged_steps"] == [9]


def test_non_converged_step_warnings_and_normal_end(tmp_path):
    log = tmp_path / "run.out"
    log.write_text(
        "     4:*NONLIN\n"
        " STEP      1 INITIATED:\n"
        " STEP      1 TERMINATED, CONVERGENCE AFTER   3 ITERATIONS\n"
        "  TOTAL LOAD FACTOR:  LOADING( 1) *   1.000E-01\n"
        " STEP      2 INITIATED:\n"
        " STEP      2 TERMINATED, NO CONVERGENCE AFTER  25 ITERATIONS\n"
        "  TOTAL LOAD FACTOR:  LOADING( 1) *   2.000E-01\n"
        "\n"
        "SEVERITY  : WARNING\n"
        "ERROR CODE: /DIANA/LB/DS30/1235\n"
        "ERRORMSG.W: Some element is distorted.\n"
        "\n"
        " /DIANA/DC/END     16:34:03      4.81-CPU     3.03-IO      420-FA   STOP  \n"
        " Elapsed time: 12 seconds\n",
        encoding="utf-8",
    )
    summary = parse_out(log)
    assert summary.completed  # warnings and non-converged steps do not make the run fail
    assert [s.converged for s in summary.steps] == [True, False]
    assert summary.steps[1].iterations == 25 and summary.steps[1].load_factor == 0.2
    assert [(w.severity, w.code, w.text) for w in summary.warnings] == [
        ("WARNING", "/DIANA/LB/DS30/1235", "Some element is distorted.")
    ]
    assert summary.summary()["warning_codes"] == {"/DIANA/LB/DS30/1235": 1}


def test_eigenvalue_run_reports_modes_and_tables():
    summary = parse_out(DATA / "eigen_beam.out")
    assert summary.completed and summary.commands == ['*EIGEN LABEL="Structural eigenvalue"']
    assert summary.eigenvalues_found == 4
    assert summary.modes == [{"mode": 1, "frequency": 37.964}, {"mode": 2, "frequency": 114.22},
                             {"mode": 3, "frequency": 174.42}, {"mode": 4, "frequency": 310.44}]
    effective_y = next(t for t in summary.mode_tables if t["columns"][2] == "EFF.MASS TY")
    assert effective_y["columns"] == ["MODE", "FREQUENCY", "EFF.MASS TY", "PERCENTAGE", "CUM.PERCENT."]
    assert effective_y["rows"][0] == [1, 37.964, 589.14, 76.711, 76.711]
    assert len(summary.mode_tables) == 7 and summary.summary()["modes"][0]["frequency"] == 37.964


def test_time_stepped_analysis_reports_end_time(tmp_path):
    log = tmp_path / "time.out"
    log.write_text(
        "     4:*NONLIN\n"
        "   TIME INCREMENT:    3.000E-02\n"
        "  STEP      1 TERMINATED, CONVERGENCE AFTER   8 ITERATIONS\n"
        "   END TIME:   3.000E-02\n",
        encoding="utf-8",
    )
    summary = parse_out(log)
    assert (summary.steps[0].time, summary.steps[0].iterations) == (0.03, 8)
    brief = summary.summary()
    assert brief["last_converged_step"] == {"number": 1, "time": 0.03}
    assert brief["end"].startswith("unfinished")


def test_syntax_errors_fail_the_run(tmp_path):
    log = tmp_path / "syntax.out"
    log.write_text(
        "     4:*LINSTA\n     5:  BEGIN OUTPUT\n     6:    FORCE CAUCHY\n"
        " Unexpected input CAUCHY\n Last line was\n FORCE ?\n"
        " /DIANA/DC/END     16:34:03      0.10-CPU     0.10-IO      7-FA   STOP  \n",
        encoding="utf-8",
    )
    summary = parse_out(log)
    assert summary.terminated and not summary.completed
    assert [e.severity for e in summary.errors] == ["SYNTAX", "SYNTAX"]
    assert summary.errors[0].text == "Unexpected input CAUCHY"


def test_overflowing_segment_fields_and_eigenvalue_lists(tmp_path):
    log = tmp_path / "buckling.out"
    log.write_text(
        "     4:*EIGEN\n"
        " /DIANA/EI/EV     16:37:02      0.14-CPU     0.15-IO      99.-FA   BEGIN\n"
        "     EIGEN-VALUES:\n"
        "     0.97887E-02(  1)    .57398D+01(  2)\n"
        "\n"
        " /DIANA/DC/END     16:37:03     12345.67-CPU     0.15-IO     100.-FA   STOP\n",
        encoding="utf-8",
    )
    summary = parse_out(log)
    assert summary.completed and summary.cpu_s == 12345.67
    assert summary.modes == [{"mode": 1, "eigenvalue": 0.0097887}, {"mode": 2, "eigenvalue": 5.7398}]


def test_factor_of_safety_retries_and_step_blocks(tmp_path):
    log = tmp_path / "reduct.out"
    log.write_text(
        "     4:*NONLIN\n"
        " STEP      1 INITIATED:\n STEP      1 TERMINATED, CONVERGENCE AFTER   2 ITERATIONS\n"
        " STEP      2 INITIATED:\n STEP      2 TERMINATED, NO CONVERGENCE AFTER  30 ITERATIONS\n"
        " STEP      2 INITIATED:\n STEP      2 TERMINATED, CONVERGENCE AFTER   6 ITERATIONS\n"
        " STEP      1 INITIATED:\n STEP      1 TERMINATED, CONVERGENCE AFTER   3 ITERATIONS\n"
        " FINAL FACTOR OF SAFETY: 1.16250\n",
        encoding="utf-8",
    )
    summary = parse_out(log)
    assert [(s.block, s.number, s.attempts, s.converged) for s in summary.steps] == [
        (0, 1, 1, True), (0, 2, 2, True), (1, 1, 1, True)]
    brief = summary.summary()
    assert brief["retried_steps"] == 1 and brief["step_blocks"] == 2 and "not_converged_steps" not in brief
    assert summary.factor_of_safety == 1.1625


def test_planned_step_count():
    from diana_mcp.out_parser import planned_step_count

    assert planned_step_count("0.01(10) 0.05(4) 0.1") == 15
    assert planned_step_count("0.25(12)") == 12


def test_running_job_has_an_open_step(tmp_path):
    log = tmp_path / "running.out"
    log.write_text("     4:*NONLIN\n STEP      1 INITIATED:\n STEP      1 : DISPLACEMENT NORM =  3.5E-03\n", encoding="utf-8")
    summary = parse_out(log)
    assert not summary.completed and not summary.aborted
    assert summary.steps[0].converged is None


def test_tail_reads_the_end():
    assert tail(DATA / "nonlin_beam_aborted.out", 1) == [" DIANA-JOB ABORTED"]


def test_nul_padding_of_a_killed_run_is_ignored(tmp_path):
    log = tmp_path / "killed.out"
    log.write_bytes(b"     4:*NONLIN\n STEP      1 INITIATED:\n" + b"\x00" * 4096)
    summary = parse_out(log)
    assert summary.last_line == "STEP      1 INITIATED:"
    assert tail(log, 1) == [" STEP      1 INITIATED:"]


def test_stopped_by_diana_after_non_convergence_and_long_elapsed_time(tmp_path):
    log = tmp_path / "stopped.out"
    log.write_text(
        "  STEP      2 TERMINATED, NO CONVERGENCE AFTER  30 ITERATIONS\n"
        "   END TIME:   2.000E-01\n"
        "\n"
        "     CONVERGENCE CRITERION WAS NOT REACHED.\n"
        "     EXECUTION IS STOPPED, BUT CAN BE CONTINUED\n"
        " /DIANA/AP/NL41    12:48:52    284.88-CPU    57.30-IO   284783-FA   STOP  \n"
        " Elapsed time: 1 hour 3 minutes 37 seconds\n",
        encoding="utf-8",
    )
    summary = parse_out(log)
    assert summary.stopped == "CONVERGENCE CRITERION WAS NOT REACHED"
    assert not summary.completed and not summary.aborted
    assert summary.summary()["end"].startswith("stopped by DIANA: CONVERGENCE CRITERION WAS NOT REACHED")
    assert summary.elapsed_s == 3817.0


def test_messages_without_code_keep_their_text(tmp_path):
    log = tmp_path / "suppressed.out"
    log.write_text(
        " SEVERITY  : WARNING\n"
        " ERROR CODE: /DIANA/LB/EL30/0094\n"
        " ERRORMSG.W: One of the angles between edges at node 1 of element 2 is\n"
        " less than 10 degrees or larger than 170 degrees.\n"
        "\n"
        " SEVERITY  : WARNING\n"
        "    50 WARNINGS SEEN\n"
        "  FURTHER WARNINGS WILL BE SUPPRESSED\n"
        "\n",
        encoding="utf-8",
    )
    first, second = parse_out(log).messages
    assert first.code == "/DIANA/LB/EL30/0094" and first.text.endswith("larger than 170 degrees.")
    assert (second.severity, second.code, second.text) == ("WARNING", "", "50 WARNINGS SEEN FURTHER WARNINGS WILL BE SUPPRESSED")
