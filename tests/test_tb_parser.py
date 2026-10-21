from pathlib import Path

from diana_mcp.tb_parser import read_tb_rows, scan_tb, select_blocks, write_csv

DATA = Path(__file__).parent / "data"
BEAM = DATA / "linsta_beam_small.tb"

NONLIN = """\
 contact_status                                                                    Page 1

 Analysis type         NONLIN
 Step nr.              1
 Load factor           1.000E-03
 Result                STATUS CONTAC
 Location of results   NODES

 Elmnr Nodnr
  1201  13875   NO CONTACT
        13876   NO CONTACT
  1202   1569   SLIPPING
         1570   NO CONTACT
 contact_status                                                                    Page 2

 Elmnr Nodnr
  1203   1571   STICKING

 Analysis type         NONLIN
 Step nr.              2
 Load factor           2.000E-03
 Result                DISPLA TOTAL  TRANSL
 Axes                  GLOBAL

 Nodnr       DtX        DtY
     6    1.000E-05 -2.000E-04
     7    **********  3.000E-04
"""


def test_scan_linear_blocks():
    blocks = scan_tb(BEAM)
    assert [(b.result, b.rows) for b in blocks] == [
        ("DISPLA TOTAL TRANSL", 15),  # 12 rows + 3 after the page break
        ("FORCE REACTI TRANSL", 2),
        ("STRESS TOTAL CAUCHY", 16),
    ]
    assert blocks[0].analysis_type == "LINSTA" and blocks[0].load_case == 1
    assert blocks[2].location == "NODES"
    assert blocks[2].id_columns == ["Elmnr", "Nodnr"]
    assert blocks[2].value_columns == ["SXX", "SYY", "SZZ", "SXY"]


def test_node_filter_reads_midspan_deflection():
    blocks = select_blocks(scan_tb(BEAM), result="DISPLA")
    (row,) = read_tb_rows(BEAM, blocks, node_ids=[6])
    assert row["Nodnr"] == 6
    assert row["DtY"] == -4.321e-4


def test_element_rows_inherit_the_element_number():
    blocks = select_blocks(scan_tb(BEAM), result="stress total")
    rows = list(read_tb_rows(BEAM, blocks, element_ids=[2]))
    assert len(rows) == 8
    assert {r["Elmnr"] for r in rows} == {2}
    assert rows[0]["Nodnr"] == 137 and rows[-1]["Nodnr"] == 811
    assert rows[-1]["SXX"] == 2.146e3


def test_reactions_balance_the_load():
    blocks = select_blocks(scan_tb(BEAM), result="REACTI")
    assert sum(r["FBY"] for r in read_tb_rows(BEAM, blocks)) == 1.0e4


def test_nonlinear_steps_status_text_and_overflow(tmp_path):
    path = tmp_path / "nonlin.tb"
    path.write_text(NONLIN, encoding="utf-8")
    status, displa = scan_tb(path)
    assert (status.step, status.load_factor, status.rows) == (1, 1.0e-3, 5)
    assert status.value_columns == []
    rows = list(read_tb_rows(path, [status]))
    assert [(r["Elmnr"], r["Nodnr"], r["value"]) for r in rows] == [
        (1201, 13875, "NO CONTACT"), (1201, 13876, "NO CONTACT"),
        (1202, 1569, "SLIPPING"), (1202, 1570, "NO CONTACT"), (1203, 1571, "STICKING"),
    ]
    assert displa.step == 2
    overflow = list(read_tb_rows(path, [displa], node_ids=[7]))[0]
    assert overflow["DtX"] != overflow["DtX"]  # NaN for the overflow marker
    assert select_blocks([status, displa], step=2) == [displa]


def test_operation_header_and_integration_points(tmp_path):
    path = tmp_path / "band_state.tb"
    path.write_text(
        " band_state                                                        Page 1\n\n"
        " Analysis type         NONLIN\n Step nr.              2\n Load factor           6.667E-03\n"
        " Result                STRAIN PLASTI GREEN\n Operation             VONMIS\n"
        " Location of results   INTPNT\n\n Elmnr Intpt       Epeq\n"
        "     1     1    1.965E-02\n           2    5.540E-03\n     2     1    7.000E-03\n",
        encoding="utf-8",
    )
    (block,) = scan_tb(path)
    assert (block.result, block.operation, block.location) == ("STRAIN PLASTI GREEN", "VONMIS", "INTPNT")
    assert block.id_columns == ["Elmnr", "Intpt"] and block.rows == 3
    assert select_blocks([block], result="vonmis") == [block]
    rows = list(read_tb_rows(path, [block], element_ids=[1]))
    assert [(r["Intpt"], r["Epeq"]) for r in rows] == [(1, 1.965e-2), (2, 5.54e-3)]


def test_nonlinear_history_of_one_node():
    path = DATA / "nonlin_beam_small.tb"
    blocks = select_blocks(scan_tb(path), result="DISPLA")
    history = [(r["step"], r["load_factor"], r["TDtY"]) for r in read_tb_rows(path, blocks, node_ids=[3])]
    assert history == [(1, 0.25, -1.09e-4), (2, 0.5, -2.181e-4)]
    reactions = select_blocks(scan_tb(path), result="REACTI", step=2)
    assert sum(r["FBY"] for r in read_tb_rows(path, reactions)) == 5000.0


def test_eigen_modes_are_selectable():
    path = DATA / "eigen_beam_small.tb"
    blocks = scan_tb(path)
    assert [(b.analysis_type, b.mode, b.frequency, b.rows) for b in blocks] == [
        ("EIGEN", 1, 37.96, 3), ("EIGEN", 2, 114.2, 3), ("EIGEN", 3, 174.4, 3), ("EIGEN", 4, 310.4, 3)]
    (row,) = read_tb_rows(path, select_blocks(blocks, mode=1), node_ids=[3])
    assert (row["mode"], row["frequency"], row["DtY"]) == (1, 37.96, -0.997681)


def test_csv_export(tmp_path):
    blocks = select_blocks(scan_tb(BEAM), result="DISPLA")
    target = tmp_path / "out" / "displa.csv"
    assert write_csv(target, read_tb_rows(BEAM, blocks)) == 15
    header = target.read_text(encoding="utf-8-sig").splitlines()[0]
    assert header == "block,step,load_factor,load_case,result,Nodnr,DtX,DtY"
