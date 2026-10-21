import pytest

from diana_mcp import episodes
from diana_mcp.experience import ExperienceStore
from diana_mcp.jobs import JobManager, classify, dat_definition

from test_jobs import FAKE, submit, wait

DAT = """\
: Diana Datafile written by Diana 10.5
'MODEL'
DIMENS "2D"
'COORDINATES'
     1     0.000000E+00     0.000000E+00
     2     1.000000E+00     0.000000E+00
'MATERI'
   1 NAME   "Concrete"
     YOUNG    {young}
'ELEMENTS'
SET  "beam"
CONNECT
     1 CQ16M      1     2
     2 CQ16M      2     1
MATERIAL 1
'END'
"""


def write_case(folder, dcf_text, young="3.00000000E+10"):
    dat, dcf = folder / "beam.dat", folder / "beam.dcf"
    dat.write_text(DAT.format(young=young), encoding="utf-8")
    dcf.write_text(dcf_text, encoding="utf-8")
    return dat, dcf


@pytest.fixture
def setup(tmp_path):
    store = ExperienceStore(tmp_path / "knowledge", use_git=False)
    manager = JobManager(tmp_path / "registry")
    manager.on_finished.append(lambda job, m: episodes.learn(job, m, store))
    folder = tmp_path / "case"
    folder.mkdir()
    return store, manager, folder


def test_dat_definition_drops_the_mesh_but_keeps_assignments(tmp_path):
    path, _ = write_case(tmp_path, "")
    lines = dat_definition(path)
    assert "  (2 lines of COORDINATES not compared)" in lines and "  (2 lines of CONNECT not compared)" in lines
    assert "     YOUNG    3.00000000E+10" in lines and "MATERIAL 1" in lines
    assert not any("CQ16M" in line for line in lines)


def test_classify_success_and_problem(tmp_path):
    from pathlib import Path
    data = Path(__file__).parent / "data"
    assert classify(data / "linsta_beam.out") == ("success", None)
    outcome, problem = classify(data / "nonlin_beam_aborted.out")
    assert outcome == "problem" and problem["end"] == "aborted"
    assert problem["errors"][0]["code"] == "/DIANA/AP/LB40/0053" and problem["not_converged_steps"]
    assert classify(tmp_path / "missing.out")[0] == "problem"


def test_problem_then_success_drafts_the_input_difference(setup):
    store, manager, folder = setup
    store.add(title="发散 LB40/0053 时减小荷载步", kind="convergence", symptom="发散", triggers=["LB40/0053"],
              entry_id="smaller-steps-on-divergence")
    dat, dcf = write_case(folder, "*NONLIN\n  EXECUT\n  DIVERGE\n*END\n")
    failed = wait(manager, submit(manager, dat, dcf, solver=FAKE))
    assert failed.outcome == "problem" and failed.recommended == ["smaller-steps-on-divergence"]
    advice = episodes.job_advice(failed, manager, store)
    assert advice["related_experience"][0]["id"] == "smaller-steps-on-divergence"

    dat, dcf = write_case(folder, "*NONLIN\n  EXECUT\n  ARCLEN\n*END\n", young="3.20000000E+10")
    solved = wait(manager, submit(manager, dat, dcf, solver=FAKE, overwrite=True))
    assert solved.outcome == "success" and solved.resolves == failed.id and solved.draft
    draft = store.get(solved.draft)
    assert draft.status == "draft" and draft.triggers[0] == "/DIANA/AP/LB40/0053"
    assert draft.related == ["smaller-steps-on-divergence"]
    fix = draft.sections["解决"]
    assert "-  DIVERGE" in fix and "+  ARCLEN" in fix and "+     YOUNG    3.20000000E+10" in fix
    assert "/DIANA/AP/LB40/0053" in draft.sections["现象"]
    learned = episodes.job_advice(solved, manager, store)["learned"]
    assert learned["draft"] == solved.draft
    assert learned["approval_candidates"] == [{"id": "smaller-steps-on-divergence", "title": "发散 LB40/0053 时减小荷载步"}]
    assert manager.get(failed.id).resolved_by == solved.id


def test_only_the_first_success_after_a_problem_is_drafted(setup):
    store, manager, folder = setup
    dat, dcf = write_case(folder, "*LINSTA\n  FAIL\n*END\n")
    failed = wait(manager, submit(manager, dat, dcf, solver=FAKE))
    assert failed.outcome == "problem" and failed.problem["end"].startswith("unfinished")
    dcf.write_text("*LINSTA\n  OK\n*END\n", encoding="utf-8")
    solved = wait(manager, submit(manager, dat, dcf, solver=FAKE, overwrite=True))
    assert solved.draft and store.get(solved.draft).type == "error-fix"
    third = wait(manager, submit(manager, dat, dcf, solver=FAKE, overwrite=True))
    assert third.resolves is None and third.draft is None  # the run before it already succeeded


def test_identical_inputs_are_not_drafted(setup, monkeypatch):
    store, manager, folder = setup
    dat, dcf = write_case(folder, "*LINSTA\n*END\n")
    first = wait(manager, submit(manager, dat, dcf, solver=FAKE))
    first.outcome, first.problem = "problem", {"end": "unfinished (killed)"}  # e.g. stopped from outside
    manager.save(first)
    second = wait(manager, submit(manager, dat, dcf, solver=FAKE, overwrite=True))
    assert second.resolves == first.id and second.draft is None
    assert store.entries() == []
