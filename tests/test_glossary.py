import struct

import pytest

from diana_mcp import config
from diana_mcp.glossary import QM_MAGIC, Glossary, GlossaryError, GlossaryStore, parse_qm


def make_qm(pairs):
    """A minimal compiled Qt translation file with the given (source, translation) messages."""
    body = b""
    for source, translation in pairs:
        zh = translation.encode("utf-16-be")
        en = source.encode("utf-8")
        body += b"\x03" + struct.pack(">I", len(zh)) + zh + b"\x06" + struct.pack(">I", len(en)) + en
        body += b"\x07" + struct.pack(">I", 3) + b"ctx" + b"\x05" + b"\0\0\0\0" + b"\x01"
    return QM_MAGIC + b"\x42" + struct.pack(">I", 0) + b"\x69" + struct.pack(">I", len(body)) + body


PAIRS = [
    ("Compressive fracture energy", "受压断裂能"),
    ("Fracture energy in compression", "受压断裂能"),
    ("Total strain based crack model", "基于总应变开裂模型"),
    ("Tension softening", "受拉软化"),
    ("Young's modulus", "杨氏模量"),
    ("Young's modulus and shear modulus may not both be zero.", "杨氏模量和剪切模量可能不能同时为零。"),
    ("&Name", "名称"),
]


def test_parse_qm_and_reject_other_files():
    assert parse_qm(make_qm(PAIRS)) == [(en, zh, "ctx") for en, zh in PAIRS]
    with pytest.raises(GlossaryError):
        parse_qm(b"not a qm file at all")


def test_translate_lookup_and_chinese_names():
    glossary = Glossary(PAIRS)
    english, terms = glossary.translate("基于总应变开裂模型的受拉软化和受压断裂能 TENSTR")
    assert terms == [("基于总应变开裂模型", ["Total strain based crack model"]), ("受拉软化", ["Tension softening"]),
                     ("受压断裂能", ["Compressive fracture energy", "Fracture energy in compression"])]
    assert english == "TENSTR Total strain based crack model Tension softening Compressive fracture energy"
    assert glossary.to_chinese("young's modulus") == "杨氏模量"
    assert glossary.lookup("Name") == [{"en": "Name", "zh": "名称"}]  # GUI accelerators (&) are removed
    assert glossary.lookup("受压断裂能")[0]["en"] == "Compressive fracture energy"
    assert "杨氏模量和剪切模量可能不能同时为零。" not in glossary.terms  # sentences are not terms


def test_store_caches_the_extracted_pairs(tmp_path, monkeypatch):
    from diana_mcp import glossary as module

    home = tmp_path / "Diana 10.5"
    (home / "share" / "lib").mkdir(parents=True)
    qm = home / "share" / "lib" / "DianaIE_zh_CN.qm"
    qm.write_bytes(make_qm(PAIRS))
    install = config.DianaInstall(home, "10.5")
    assert len(GlossaryStore(tmp_path / "cache").get(install)) == len(PAIRS)

    def fail(data):
        raise AssertionError("the cache should have been used")

    monkeypatch.setattr(module, "parse_qm", fail)
    assert len(GlossaryStore(tmp_path / "cache").get(install)) == len(PAIRS)  # served from the cache
    monkeypatch.undo()
    qm.write_bytes(make_qm(PAIRS[:2]))  # a different translation file: rebuilt, not the stale cache
    assert len(GlossaryStore(tmp_path / "cache").get(install)) == 2
    qm.unlink()
    with pytest.raises(FileNotFoundError):
        GlossaryStore(tmp_path / "cache").get(install)
