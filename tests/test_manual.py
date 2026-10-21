import sqlite3
import struct
import zlib

import pytest

from diana_mcp import config
from diana_mcp.manual import Manual, ManualStore, extract_pages, find_qch

PAGES = [
    {"name": "Theory/Theoryse738.html", "title": "General Commands", "text": (
        "I.1.1.2 Options for Tabular Output\n D i a n a produces tabular output with the device name TABULA .\n"
        " BEGIN LAYOUT\n [ LINPAG nlines n ]\n [ COLLIN ncols n ]\n END LAYOUT\n"
        " LINPAG nlines specifies the number of lines per page. The default is 66 lines per page.")},
    {"name": "Theory/Theoryse350.html", "title": "Discrete Cracking", "text": (
        "53.3.3 Nonlinear Tension Softening (Hordijk et al.)\n The Hordijk tension softening curve ...")},
    {"name": "CumIdxL.html", "title": "Cum Index L", "text": "LINPAG command DIANA Documentation : 581 LINPAG"},
]


def test_search_ranks_content_above_the_index_and_reports_sections():
    manual = Manual("10.5", PAGES)
    hits = manual.search("LINPAG lines per page")
    assert hits[0].page == "Theory/Theoryse738.html"
    assert hits[0].section == "I.1.1.2 Options for Tabular Output"
    assert "LINPAG" in hits[0].snippet
    assert manual.search("Hordijk")[0].section.startswith("53.3.3")
    assert manual.search("nothing-like-this") == []


def test_read_with_find_offset_and_short_names():
    manual = Manual("10.5", PAGES)
    page = manual.read("Theoryse738", find="linpag nlines specifies", max_chars=500)
    assert page["page"] == "Theory/Theoryse738.html"
    assert "The default is 66" in page["text"]
    assert page["next_offset"] is None
    with pytest.raises(KeyError):
        manual.read("Theoryse999")
    with pytest.raises(LookupError):
        manual.read("Theoryse738", find="not on the page")


def _fake_qch(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE FileNameTable (FolderId INTEGER, Name TEXT, FileId INTEGER, Title TEXT)")
    db.execute("CREATE TABLE FileDataTable (Id INTEGER PRIMARY KEY, Data BLOB)")
    markup = "<html><head><title>General Commands</title></head><body><p>BEGIN LAYOUT</p><p>LINPAG 0</p></body></html>"
    data = markup.encode("utf-8")
    db.execute("INSERT INTO FileDataTable VALUES (1, ?)", (struct.pack(">I", len(data)) + zlib.compress(data),))
    db.execute("INSERT INTO FileNameTable VALUES (1, 'Theory/Theoryse738.html', 1, 'x')")
    db.execute("INSERT INTO FileDataTable VALUES (2, ?)", (b"\0\0\0\0",))
    db.execute("INSERT INTO FileNameTable VALUES (1, 'pic/a.png', 2, 'a.png')")
    db.commit()
    db.close()


def test_extract_and_cache_from_a_qch(tmp_path):
    home = tmp_path / "Diana 10.5"
    (home / "share" / "Man" / "HTML").mkdir(parents=True)
    _fake_qch(home / "share" / "Man" / "HTML" / "diana-10.5.qch")
    install = config.DianaInstall(home, "10.5")
    assert find_qch(install).name == "diana-10.5.qch"
    assert extract_pages(find_qch(install)) == [
        {"name": "Theory/Theoryse738.html", "title": "General Commands", "text": "BEGIN LAYOUT\n LINPAG 0"}
    ]
    tutorial = home / "share" / "Tutorials" / "Beam"
    tutorial.mkdir(parents=True)
    (tutorial / "beam.py").write_text('addMaterial("Concrete", "CONCR", "TSCR", [])\n', encoding="utf-8")
    store = ManualStore(tmp_path / "cache")
    manual = store.get(install)
    assert manual.search("LINPAG")[0].title == "General Commands"
    assert manual.search("addMaterial TSCR")[0].page == "Examples/Tutorials/Beam/beam.py"
    assert (tmp_path / "cache" / "diana-10.5.json").is_file()
