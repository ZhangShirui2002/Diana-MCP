import json

import pytest

from diana_mcp import server
from diana_mcp.api_lessons import ApiLessons, calls, error_type, failing_line

TRACEBACK = '''Traceback (most recent call last):
  File "<mcp>", line 2, in <module>
  File "C:/Program Files/Diana 10.5/modules/diana/mesh/model.py", line 10, in setParameter
RuntimeError: Parameter 'LINEAR/ELASTI/YOUNGS' not found for MATERIAL 'C30'
'''
CODE = 'x = 1\nsetParameter("MATERIAL", "C30", "LINEAR/ELASTI/YOUNGS", 3e10)\n'


def test_parsing_helpers():
    assert error_type(TRACEBACK) == "RuntimeError" and failing_line(TRACEBACK) == 2
    assert calls(CODE, 2) == [("setParameter", 'setParameter("MATERIAL", "C30", "LINEAR/ELASTI/YOUNGS", 3e10)')]
    assert calls("print(len(x))") == [("print", "print(len(x))"), ("len", "len(x)")]


def test_failure_then_success_is_a_lesson(tmp_path):
    lessons = ApiLessons(tmp_path / "pending.json")
    assert lessons.record_failure(CODE, TRACEBACK)
    assert lessons.resolve('fitAll()') == []  # unrelated success
    fixed = 'setParameter("MATERIAL", "C30", "LINEAR/ELASTI/YOUNG", 3e10)'
    (lesson,) = lessons.resolve(fixed)
    assert lesson["command"] == "setParameter" and lesson["worked"] == fixed
    assert lesson["error"].startswith("RuntimeError: Parameter")
    assert lessons.resolve(fixed) == []  # consumed


def test_python_slips_and_builtins_are_ignored(tmp_path):
    lessons = ApiLessons(tmp_path / "pending.json")
    assert not lessons.record_failure("undefined_name", 'File "<mcp>", line 1\nNameError: name x is not defined')
    assert not lessons.record_failure("len(5)", 'File "<mcp>", line 1\nTypeError: object of type int has no len()')
    known = ApiLessons(tmp_path / "k.json", known={"setParameter"})
    assert not known.record_failure("fitAll(1)", 'File "<mcp>", line 1\nRuntimeError: x')  # not a known command


def test_execute_drafts_the_lesson_and_recommends(standalone_bridge, isolated_state):
    sessions_dir, _, _ = standalone_bridge
    define = ("def setThing(value):\n"
              "    if value < 0:\n"
              "        raise RuntimeError('value ' + str(value) + ' must be positive')\n"
              "    return value\n")
    call = lambda tool, **kw: server.mcp.call_tool(tool, kw)
    import asyncio
    run = lambda tool, **kw: asyncio.run(call(tool, **kw))
    run("diana_execute", code=define)
    with pytest.raises(Exception, match="must be positive"):
        run("diana_execute", code="setThing(-1)")
    result = json.loads(run("diana_execute", code="setThing(2)").content[0].text)
    (draft,) = result["auto_drafts"]
    entry = server.experiences.get(draft["id"])
    assert entry.status == "draft" and entry.triggers[0] == "setThing" and "setThing(2)" in entry.sections["解决"]
    with pytest.raises(Exception, match="相关经验") as failure:
        run("diana_execute", code="setThing(-5)")
    assert draft["id"] in str(failure.value)
    again = json.loads(run("diana_execute", code="setThing(3)").content[0].text)
    assert "auto_drafts" not in again  # the same lesson is not drafted twice


@pytest.fixture
def isolated_state(standalone_bridge, tmp_path, monkeypatch):
    from diana_mcp.experience import ExperienceStore
    from diana_mcp.session import SessionManager

    sessions_dir, _, _ = standalone_bridge
    monkeypatch.setenv("DIANA_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(server, "sessions", SessionManager([sessions_dir]))
    monkeypatch.setattr(server, "experiences", ExperienceStore(tmp_path / "knowledge", use_git=False))
    monkeypatch.setattr(server, "_known_commands", lambda: None)
    return tmp_path
