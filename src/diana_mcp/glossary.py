"""Official English <-> Chinese DIANA terminology, read from DianaIE's own GUI translation.

DianaIE ships its Chinese interface as share/lib/DianaIE_zh_CN.qm, a compiled Qt translation file with ~15,000
source/translation pairs: every label, option and message of the GUI ("Total strain based crack model" =
"基于总应变开裂模型", "Compressive fracture energy" = "受压断裂能"). It lets a Chinese question find the English
manual pages, maps what the user sees in the Chinese GUI to DIANA's terms, and names parameters in Chinese.
Like the manual, the extracted text stays in the local cache and is never shipped with this package.

.qm layout: 16-byte magic, then sections (1-byte tag, 4-byte big-endian length). In the messages section (0x69)
each message is a run of tags until tag 1 (end): 3 = translation (UTF-16BE, length-prefixed), 6 = source text,
7 = context, 8 = comment (UTF-8, length-prefixed), 5 = obsolete 4-byte hash, 2 = obsolete, no payload.
"""
from __future__ import annotations

import json
import re
import struct
from collections import defaultdict
from pathlib import Path

from . import config

CACHE_FORMAT = 1
QM_MAGIC = bytes([0x3C, 0xB8, 0x64, 0x18, 0xCA, 0xEF, 0x9C, 0x95, 0xCD, 0x21, 0x1C, 0xBF, 0x60, 0xA1, 0xBD, 0xDD])
_TERM_MAX_EN, _TERM_MAX_ZH = 60, 24


class GlossaryError(ValueError):
    pass


def parse_qm(data: bytes) -> list[tuple[str, str, str]]:
    """(source, translation, context) of every message in a compiled Qt translation file."""
    if data[:16] != QM_MAGIC:
        raise GlossaryError("not a Qt .qm translation file")
    pos, sections = 16, {}
    while pos + 5 <= len(data):
        tag, length = data[pos], struct.unpack(">I", data[pos + 1:pos + 5])[0]
        sections[tag] = data[pos + 5:pos + 5 + length]
        pos += 5 + length
    messages = sections.get(0x69, b"")
    out, current, p = [], {}, 0
    while p < len(messages):
        tag = messages[p]
        p += 1
        if tag == 1:
            if current.get("source") and current.get("translation"):
                out.append((current["source"], current["translation"], current.get("context", "")))
            current = {}
        elif tag == 2:
            continue
        elif tag == 5:
            p += 4
        elif tag in (3, 6, 7, 8):
            length = struct.unpack(">I", messages[p:p + 4])[0]
            p += 4
            if length == 0xFFFFFFFF:
                continue
            raw = messages[p:p + length]
            p += length
            if tag == 3:
                current["translation"] = raw.decode("utf-16-be", errors="replace")
            else:
                current[{6: "source", 7: "context", 8: "comment"}[tag]] = raw.decode("utf-8", errors="replace")
        else:
            raise GlossaryError(f"unknown message tag {tag} at byte {p - 1}")
    return out


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("&", "")).strip()


def _is_term(en: str, zh: str) -> bool:
    """Short labels (terms), not sentences or messages with placeholders."""
    return (len(en) <= _TERM_MAX_EN and len(zh) <= _TERM_MAX_ZH and "%" not in en
            and not re.search(r"[.!?:;。！？：；]$", en) and len(zh) >= 2)


class Glossary:
    def __init__(self, pairs: list[tuple[str, str]]):
        self.pairs = [(_clean(en), _clean(zh)) for en, zh in pairs if _clean(en) and _clean(zh)]
        zh_to_en: dict[str, list[str]] = defaultdict(list)
        en_to_zh: dict[str, list[str]] = defaultdict(list)
        for en, zh in self.pairs:
            if en not in zh_to_en[zh]:
                zh_to_en[zh].append(en)
            if zh not in en_to_zh[en.lower()]:
                en_to_zh[en.lower()].append(zh)
        self.zh_to_en = dict(zh_to_en)
        self.en_to_zh = dict(en_to_zh)
        self.terms = {zh: sorted(ens, key=len) for zh, ens in self.zh_to_en.items()
                      if any(_is_term(en, zh) for en in ens) and not re.fullmatch(r"[\x00-\x7f]+", zh)}
        self._max_zh = max((len(z) for z in self.terms), default=0)

    def __len__(self) -> int:
        return len(self.pairs)

    def translate(self, text: str) -> tuple[str, list[tuple[str, list[str]]]]:
        """English rendering of the DIANA terms in a (partly) Chinese text, longest terms first.

        Returns (english, [(chinese term, [english...])]); the english string also keeps the ASCII words of the
        text (keywords, error codes), so it can be searched in English sources directly.
        """
        found: list[tuple[str, list[str]]] = []
        i = 0
        while i < len(text):
            for length in range(min(self._max_zh, len(text) - i), 1, -1):
                piece = text[i:i + length]
                if piece in self.terms:
                    found.append((piece, self.terms[piece][:2]))
                    i += length
                    break
            else:
                i += 1
        ascii_words = re.findall(r"[A-Za-z0-9_./-]{2,}", text)
        english = " ".join(dict.fromkeys(ascii_words + [en for _, ens in found for en in ens[:1]]))
        return english, found

    def to_chinese(self, english: str) -> str | None:
        """The GUI's Chinese name for an English label, e.g. a SCHEMA parameter title."""
        names = self.en_to_zh.get(_clean(english).lower())
        return min(names, key=len) if names else None

    def lookup(self, term: str, limit: int = 12) -> list[dict[str, str]]:
        """Pairs for a term in either language: exact matches first, then the shortest pairs containing it."""
        term = _clean(term)
        if not term:
            return []
        lowered = term.lower()
        exact, partial = [], []
        seen = set()
        for en, zh in self.pairs:
            key = (en, zh)
            if key in seen:
                continue
            seen.add(key)
            if en.lower() == lowered or zh == term:
                exact.append({"en": en, "zh": zh})
            elif lowered in en.lower() or term in zh:
                partial.append({"en": en, "zh": zh})
        partial.sort(key=lambda p: (len(p["en"]) + len(p["zh"]), p["en"]))
        return (exact + partial)[:limit]


def find_qm(install: config.DianaInstall, language: str = "zh_CN") -> Path | None:
    path = install.home / "share" / "lib" / f"DianaIE_{language}.qm"
    return path if path.is_file() else None


class GlossaryStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory
        self._loaded: dict[str, Glossary] = {}

    @property
    def _dir(self) -> Path:
        return self.directory or config.state_dir() / "glossary"

    def get(self, install: config.DianaInstall, language: str = "zh_CN") -> Glossary:
        """The glossary of this installation; the cache is used while it matches the .qm file (path and size)."""
        qm = find_qm(install, language)
        if qm is None:
            raise FileNotFoundError(f"no DianaIE_{language}.qm in {install.home / 'share' / 'lib'}")
        key = f"{qm}|{qm.stat().st_size}"
        if key not in self._loaded:
            path = self._dir / f"diana-{install.version}-{language}.json"
            data = None
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("format") != CACHE_FORMAT or data.get("key") != key:
                    data = None
            if data is None:
                pairs = [(source, translation) for source, translation, _ in parse_qm(qm.read_bytes())]
                data = {"format": CACHE_FORMAT, "key": key, "version": install.version, "pairs": pairs}
                self._dir.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self._loaded[key] = Glossary([tuple(p) for p in data["pairs"]])
        return self._loaded[key]


_DEFAULT = GlossaryStore()


def default_glossary() -> Glossary | None:
    """The installed DIANA's Chinese glossary, or None when there is no installation or translation file."""
    try:
        return _DEFAULT.get(config.default_install())
    except (FileNotFoundError, GlossaryError, OSError, ValueError):
        return None
