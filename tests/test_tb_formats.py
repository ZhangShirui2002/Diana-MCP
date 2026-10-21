"""Output layouts produced by the real DIANA 10.5 solver (tests/data/formats, simply supported beam)."""
from pathlib import Path

from diana_mcp.tb_parser import read_tb_rows, scan_tb, select_blocks

FORMATS = Path(__file__).parent / "data" / "formats"


def rows(name, **filters):
    path = FORMATS / name
    blocks = select_blocks(scan_tb(path), **{k: v for k, v in filters.items() if k in ("result", "step", "load_case")})
    ids = {k: v for k, v in filters.items() if k in ("node_ids", "element_ids")}
    return list(read_tb_rows(path, blocks, **ids))


def test_combin_table_with_blank_reaction_columns():
    blocks = scan_tb(FORMATS / "combin.tb")
    assert [(b.result, b.load_case, b.rows, b.skipped) for b in blocks] == [
        ("Combination", 1, 4, 0), ("Combination", 2, 4, 0)]
    assert blocks[0].columns == ["Nodnr", "DtX", "DtY", "DtZ", "FBX", "FBY", "FBZ"]
    node3, node5 = [r for r in rows("combin.tb", load_case=1) if r["Nodnr"] in (3, 5)]
    assert node3["DtY"] == -4.361e-4 and "FBX" not in node3  # blank: not a support
    assert (node5["FBX"], node5["FBY"]) == (-4.144e-08, 5000.0)


def test_coordinates_and_principal_axes():
    displa = rows("coorax.tb", result="DISPLA", load_case=1)
    assert [(r["Nodnr"], r["X0"], r["Y0"]) for r in displa] == [(3, 2.0, 0.4), (6, 2.0, 0.0)]
    principal = rows("coorax.tb", result="PRINCI", load_case=1)
    assert [(r["Elmnr"], r["Intpt"]) for r in principal] == [(1, 1), (1, 2), (1, 3), (1, 4)]
    first = principal[0]
    assert (first["S1"], first["S2"], first["xX"], first["xY"], first["xZ"]) == (1166.0, 212.1, 0.70, -0.71, 0.71)
    assert "yX" not in first


def test_extreme_values_with_load_case_annotations():
    displa, stress = scan_tb(FORMATS / "extreme.tb")
    assert displa.load_case is None and displa.extremes[:2] == [
        {"component": "DtX", "value": 6.115e-06, "load_case": 2, "node": 3},
        {"component": "DtY", "value": -4.361e-04, "load_case": 1, "node": 3},
    ]
    assert stress.element_type == "CQ16M"
    assert stress.extremes[0] == {"component": "SXX", "value": -1.364e4, "load_case": 1, "element": 1, "node": 177}
    node3 = rows("extreme.tb", result="DISPLA", node_ids=[3])[0]
    assert (node3["DtX"], node3["DtX case"], node3["DtY"], node3["DtY case"]) == (6.115e-06, "2", -4.361e-04, "1")
    element2 = rows("extreme.tb", result="STRESS", element_ids=[2])
    assert len(element2) == 8 and element2[0]["Nodnr"] == 137 and element2[0]["SXY case"] == "1"


def test_wrapped_rows_equal_the_unwrapped_table():
    wrapped, wide = scan_tb(FORMATS / "wrap130.tb")[0], scan_tb(FORMATS / "wrap250.tb")[0]
    assert len(wrapped.header_lines) == 3 and len(wide.header_lines) == 1
    assert wrapped.columns == wide.columns
    strip = lambda rs: [{k: v for k, v in r.items() if k != "block"} for r in rs]
    assert strip(rows("wrap130.tb")) == strip(rows("wrap250.tb"))
    node3 = rows("wrap130.tb", load_case=1, node_ids=[3])[0]
    assert (node3["DtY"], node3["FEY"]) == (-4.361093467e-4, -1.0e4) and "FBY" not in node3


def test_crack_status_text_and_sparse_crack_strains():
    status, strain, totcrk = scan_tb(FORMATS / "crack_status.tb")
    assert (status.result, status.step, status.load_factor, status.location) == ("STATUS CRACK", 8, 2.0, "INTPNT")
    texts = {r["value"] for r in rows("crack_status.tb", result="STATUS CRACK")}
    assert texts == {"NO CRACK YET", "PARTIALLY OPEN LOADING"}
    assert rows("crack_status.tb", result="STRAIN CRACK", element_ids=[248])[0]["Eknn"] == 9.578e-06
    assert totcrk.columns == ["Elmnr", "Intpt", "ALPten", "ALPcom"] and totcrk.skipped == 0
