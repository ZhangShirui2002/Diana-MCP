import copy
import runpy

import pytest

from diana_mcp import snapshots as sn
from diana_mcp.changes import Entry, category, parse_call

BASE = {
    "format": 1,
    "taken": 1000.0,
    "project": {"name": "beam", "path": "F:/p/beam.dpf", "dimension": ["2D"], "aspects": ["STRUCT"]},
    "units": {"LENGTH": "M", "FORCE": "N"},
    "shapes": {"beam": {"kind": "regular", "material": "C30", "bbox": [0.0, 4.0, 0.0, 0.4, 0.0, 0.0], "size": 1.6}},
    "materials": {"C30": {"parameters": {"MATCLS/CONCR/LEI/LINEAR/ELASTI/YOUNG": 3e10}, "class": "混凝土", "model": "线弹性"}},
    "loads": {"P": {"parameters": {"FORCE/VALUE": -10000.0, "FORCE/TARGET": '"beam"的vertex (2, 0.4)'},
                    "readable": {"Load name": "P", "力值": "-10000 N"}, "load_case": "LC1"}},
    "load_cases": ["LC1"],
    "mesh": {"nodes": 100, "elements": 30},
    "analyses": {"Linear": {"commands": ["LINSTA"], "dcf": ["*LINSTA", "  SOLVE PARDIS", "*END"]}},
}


def modified():
    after = copy.deepcopy(BASE)
    after["materials"]["C30"]["parameters"]["MATCLS/CONCR/LEI/LINEAR/ELASTI/YOUNG"] = 3.25e10
    after["loads"]["P"]["parameters"]["FORCE/TARGET"] = '"beam"的vertex (3, 0.4)'
    after["loads"]["P"]["readable"]["力值"] = "-12000 N"  # readable copies are not compared
    after["materials"]["C40"] = {"parameters": {}, "class": "混凝土", "model": "总应变裂缝"}
    del after["shapes"]["beam"]["size"]
    after["load_cases"].append("LC2")
    after["mesh"]["nodes"] = 400
    after["units"]["LENGTH"] = "MM"
    after["analyses"]["Linear"]["dcf"][1] = "  SOLVE ITERAT"
    return after


def test_diff_reports_additions_removals_and_old_new_values():
    changes = sn.diff(BASE, modified())
    texts = [c.text() for c in changes]
    assert "单位：LENGTH（长度） M → MM" in texts
    assert "网格：nodes（节点数） 100 → 400" in texts
    assert "新增材料 “C40”：混凝土 / 总应变裂缝" in texts
    assert "材料 “C30”：MATCLS/CONCR/LEI/LINEAR/ELASTI/YOUNG（弹性模量） 3e+10 → 3.25e+10" in texts
    assert "荷载 “P”：FORCE/TARGET（挂载位置） \"beam\"的vertex (2, 0.4) → \"beam\"的vertex (3, 0.4)" in texts
    assert "几何体 “beam”：size（尺寸(长/面积/体积)） 1.6 → （无）" in texts
    assert "新增荷载工况 “LC2”" in texts
    dcf = [c for c in changes if c.lines]
    assert dcf and dcf[0].lines == ["-  SOLVE PARDIS", "+  SOLVE ITERAT"]
    assert not any("力值" in t for t in texts)


def test_diff_ignores_float_noise():
    after = copy.deepcopy(BASE)
    after["materials"]["C30"]["parameters"]["MATCLS/CONCR/LEI/LINEAR/ELASTI/YOUNG"] = 3e10 * (1 + 1e-13)
    after["shapes"]["beam"]["bbox"][1] = 4.0 + 1e-12
    assert sn.diff(BASE, after) == []


def entry(text, source):
    e = Entry(0, 1, text, source=source)
    e.name, e.args, e.kwargs = parse_call(text)
    e.category = category(e.name)
    return e


def test_attribute_uses_calls_that_name_the_item():
    changes = sn.diff(BASE, modified())
    sn.attribute(changes, [
        entry('setParameter( "MATERIAL", "C30", "LINEAR/ELASTI/YOUNG", 3.25e10 )', "user"),
        entry('addMaterial( "C40", "CONCR", "TSCR", [] )', "ai"),
        entry('generateMesh( [  ] )', "user"),
        entry('projectName(  )', "ai"),
    ])
    by = {(c.section, c.item, c.key): c.by for c in changes}
    assert by[("materials", "C30", "MATCLS/CONCR/LEI/LINEAR/ELASTI/YOUNG")] == "user"
    assert by[("materials", "C40", None)] == "ai"
    assert by[("mesh", None, "nodes")] == "user"
    assert by[("load_cases", "LC2", None)] is None


def test_store_saves_lists_finds_and_prunes(tmp_path, monkeypatch):
    monkeypatch.setattr(sn, "KEEP_PER_PROJECT", 2)
    store = sn.SnapshotStore(tmp_path)
    infos = []
    for i in range(4):
        snap = copy.deepcopy(BASE)
        snap["taken"] = 1000.0 + i
        infos.append(store.save(snap, label="checkpoint" if i == 0 else None))
    key = sn.project_key("F:/p/beam.dpf", "beam")
    kept = store.list(key)
    assert [i.id for i in kept] == [infos[3].id, infos[2].id, infos[0].id]  # labelled ones survive pruning
    assert store.latest(key).id == infos[3].id
    assert store.find("checkpoint", key).id == infos[0].id
    assert store.find(infos[2].id).taken == 1002.0
    assert store.load(infos[3])["taken"] == 1003.0
    assert sn.project_key(None, "x") == "x-unsaved"


class FakeDiana:
    """Minimal DianaIE namespace: the commands the snapshot uses, without the report data model."""

    def __init__(self):
        self.ns = {
            "projectName": lambda: "beam", "currentProject": lambda: "F:/p/beam.dpf",
            "modelDimensions": lambda: ["2D"], "modelAnalysisAspects": lambda: ["STRUCT"],
            "quantities": lambda: ["LENGTH"], "unit": lambda q: "M",
            "shapes": lambda: ["beam"], "bodyType": lambda s: "SHEET", "boundingBox": lambda s: (0.0, 4.0, 0.0, 0.4, 0, 0),
            "areaOf": lambda s: 1.6, "vertexCount": lambda s: 4, "edgeCount": lambda s: 4, "faceCount": lambda s: 1,
            "materials": lambda: ["C30"], "geometries": lambda: [],
            "availableParameters": lambda kind, name: ["/MATERI(1)/MATCLS/CONCR/LEI/LINEAR/ELASTI/YOUNG"],
            "parameter": lambda kind, name, path: 3e10,
            "geometryLoads": lambda: [], "geometrySupports": lambda: [],
            "geometryLoadSets": lambda: ["LC1"], "geometrySupportSets": lambda: [],
            "allNodeIds": lambda: (1, 2, 3), "allElementIds": lambda: (1,),
            "analyses": lambda: ["Linear"], "analysisCommands": lambda a: ["LINSTA"], "phaseNames": lambda a: [],
        }


def test_snapshot_module_runs_on_a_namespace_without_the_report_model(tmp_path):
    module = runpy.run_path(str(sn.SNAPSHOT_SCRIPT), run_name="diana_mcp_snapshot")
    out = tmp_path / "snap.json"
    snap = module["take"](FakeDiana().ns, str(out))
    assert out.is_file()
    assert snap["project"] == {"name": "beam", "path": "F:/p/beam.dpf", "dimension": ["2D"], "aspects": ["STRUCT"]}
    assert snap["materials"] == {"C30": {"parameters": {"MATCLS/CONCR/LEI/LINEAR/ELASTI/YOUNG": 30000000000.0}}}
    assert snap["shapes"]["beam"]["size"] == 1.6 and snap["shapes"]["beam"]["kind"] == "other"
    assert snap["mesh"] == {"nodes": 3, "elements": 1}
    assert snap["analyses"] == {"Linear": {"commands": ["LINSTA"]}}
    assert snap["element_data"] == {}  # elementData() is missing: an empty section, not an error
    assert "errors" not in snap or "materials" not in snap["errors"]


def test_snapshot_corrects_the_stale_path_after_save_as():
    fake = FakeDiana()
    fake.ns["projectName"] = lambda: "beam_nl"  # saved as beam_nl.dpf; DIANA still reports beam.dpf
    module = runpy.run_path(str(sn.SNAPSHOT_SCRIPT), run_name="diana_mcp_snapshot")
    project = module["take"](fake.ns)["project"]
    assert project["path"] == "F:/p/beam_nl.dpf" and project["stored_path"] == "F:/p/beam.dpf"


def test_refine_project_path_uses_the_session_history():
    from diana_mcp.tracking import refine_project_path

    snap = {"project": {"name": "copy", "path": "F:/p/copy.dpf"}}
    refine_project_path(snap, [entry('openProject( "F:/p/beam.dpf" )', "user"),
                               entry('saveProjectAs( "D:/other/copy.dpf" )', "user"),
                               entry('fitAll(  )', "user")])
    assert snap["project"] == {"name": "copy", "path": "D:/other/copy.dpf", "stored_path": "F:/p/copy.dpf"}
    fresh = {"project": {"name": "new", "path": ""}}
    refine_project_path(fresh, [entry('newProject( "F:/p/new", 10, {} )', "user")])
    assert fresh["project"]["path"] == "F:/p/new.dpf"
    after = copy.deepcopy(snap)
    after["project"]["stored_path"] = "elsewhere"
    assert sn.diff(snap, after) == []


def test_snapshot_records_errors_per_section(tmp_path):
    fake = FakeDiana()
    fake.ns["materials"] = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    module = runpy.run_path(str(sn.SNAPSHOT_SCRIPT), run_name="diana_mcp_snapshot")
    snap = module["take"](fake.ns)
    assert snap["materials"] is None and "boom" in snap["errors"]["materials"]
    assert snap["mesh"] == {"nodes": 3, "elements": 1}


@pytest.mark.parametrize("value, text", [(3e10, "3e+10"), (None, "（无）"), ([1, 2.5], "[1, 2.5]"), ("x", "x")])
def test_show(value, text):
    assert sn.show(value) == text
