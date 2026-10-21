"""Keyword reference from DIANA's data-model schema (``<DIANA home>/lib/SCHEMA``).

The user's manual does not list the keywords DianaIE scripting needs: material classes and models
(addMaterial), geometry and element classes (addGeometry, setElementClassType), load types, or the parameter
directories used in setParameter paths. SCHEMA defines all of them, with titles, units, domains, defaults
and enumerations. Its syntax, as far as used here:

    :Define MCConcrete                                      macro definition, included elsewhere as @MCConcrete
      :PushDir concr :Func=Value( MCNAME, CONCR )           directory, active when MCNAME = CONCR
        MATMDL.c*6   :Enum=( LEI :"Linear elastic isotropic", TSCR:"Total strain based crack model" ) \
                     :Default=TSCR                          typed item: .d real, .i integer, .c*6 keyword ...
      :PopDir
    :End

Like the API reference and the manual, the parsed schema stays on the local machine.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config

_ITEM = re.compile(r"^([A-Z][A-Z0-9_]*)\.([a-z])(\*\d+)?(\(\s*\d*\s*\))?\s*(.*)$")
_ATTR = re.compile(r':(\w+)(?:\s*=\s*(\((?:[^()"]|"[^"]*")*\)|"[^"]*"|\[[^\]]*\]\S*|[^\s:]+(?:\s*\([^)]*\))?))?')
_ENUM = re.compile(r'(\w+)\s*:\s*"([^"]*)"')
_TYPES = {"d": "real", "r": "real", "i": "integer", "c": "keyword/text", "l": "logical", "b": "logical"}


@dataclass
class SchemaEntry:
    keyword: str
    kind: str  # item type ('real', 'keyword/text', ...) or 'directory'
    define: str
    dirs: list[str]
    title: str = ""
    text: str = ""
    quantity: str = ""
    domain: str = ""
    default: str = ""
    condition: str = ""  # e.g. 'MATMDL = TSCR' for conditional directories
    enum: list[tuple[str, str]] = field(default_factory=list)
    required: bool = False
    line: int = 0

    def one_line(self, context: str = "") -> str:
        where = "/".join(self.dirs)
        parts = [f"{self.keyword} ({self.kind})"]
        if self.title:
            parts.append(f'"{self.title}"')
        if self.quantity:
            parts.append(f"unit {self.quantity}")
        if self.domain:
            parts.append(f"domain {self.domain}")
        if self.default:
            parts.append(f"default {self.default}")
        if self.condition:
            parts.append(f"when {self.condition}")
        if self.required:
            parts.append("required")
        head = " ".join(parts)
        location = f"{self.define}" + (f" [{where}]" if where else "") + (f" <- {context}" if context else "")
        enum = ""
        if self.enum:
            shown = self.enum if len(self.enum) <= 40 else self.enum[:40] + [("...", f"{len(self.enum) - 40} more")]
            enum = "\n      values: " + "; ".join(f"{k}={v}" for k, v in shown)
        return f"{head}\n      in {location}{enum}"


def _strip_comment(line: str) -> str:
    inside = False
    for i, ch in enumerate(line):
        if ch == '"':
            inside = not inside
        elif ch == "#" and not inside:
            return line[:i]
    return line


def _logical_lines(text: str):
    buffer, start = "", 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw).rstrip()
        if not buffer:
            start = number
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        yield start, (buffer + line).strip()
        buffer = ""
    if buffer:
        yield start, buffer.strip()


def _attributes(text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for match in _ATTR.finditer(text):
        key, value = match.group(1), (match.group(2) or "").strip()
        if key in attrs and key == "Validation":
            continue
        attrs.setdefault(key, value.strip('"') if value.startswith('"') else value)
    return attrs


class Schema:
    def __init__(self, entries: list[SchemaEntry], includes: dict[str, list[tuple[str, str]]]):
        self.entries = entries
        self.includes = includes  # define -> [(including define, dirs at the include)]

    @classmethod
    def parse(cls, text: str) -> Schema:
        entries: list[SchemaEntry] = []
        includes: dict[str, list[tuple[str, str]]] = {}
        define, dirs = "(top)", []
        for number, line in _logical_lines(text):
            if not line:
                continue
            if line.startswith(":Define"):
                define, dirs = line.split()[1] if len(line.split()) > 1 else "(unnamed)", []
                continue
            if line.startswith(":End"):
                define, dirs = "(top)", []
                continue
            if line.startswith(":PushDir"):
                pieces = line.split(None, 2)
                name = pieces[1] if len(pieces) > 1 else "?"
                attrs = _attributes(pieces[2] if len(pieces) > 2 else "")
                condition = ""
                func = re.match(r"Value\s*\(\s*(\w+)\s*,\s*([\w ,]+)\)", attrs.get("Func", ""))
                if func:
                    condition = f"{func.group(1)} = {func.group(2).strip()}"
                entries.append(SchemaEntry(name.upper(), "directory", define, list(dirs), title=attrs.get("Title", ""),
                                           condition=condition, line=number))
                dirs = dirs + [name]
                continue
            if line.startswith(":PopDir"):
                dirs = dirs[:-1]
                continue
            if line.startswith("@") or "/@" in line.split()[0]:
                target = line.split()[0].split("@", 1)[1]
                sub = line.split()[0].split("/@")[0] if "/@" in line.split()[0] else ""
                includes.setdefault(target, []).append((define, "/".join(dirs + ([sub] if sub else []))))
                continue
            item = _ITEM.match(line)
            if not item:
                continue
            keyword, type_code, _, _, rest = item.groups()
            attrs = _attributes(rest)
            enum = _ENUM.findall(attrs.get("Enum", ""))
            entries.append(SchemaEntry(
                keyword, _TYPES.get(type_code, type_code), define, list(dirs),
                title=attrs.get("Title", ""), text=attrs.get("Text", ""),
                quantity=attrs.get("Quantity", ""), domain=attrs.get("Domain", ""),
                default=attrs.get("Default", ""), enum=enum, required="Required" in attrs, line=number,
            ))
        return cls(entries, includes)

    @classmethod
    def load(cls, install: config.DianaInstall) -> Schema:
        path = install.home / "lib" / "SCHEMA"
        if not path.is_file():
            raise FileNotFoundError(f"no schema file at {path}")
        return cls.parse(path.read_text(encoding="utf-8", errors="replace"))

    def context(self, entry: SchemaEntry) -> str:
        """Where the entry's macro is included, e.g. 'MCConcrete [matcls/concr]'."""
        parents = self.includes.get(entry.define, [])
        return ", ".join(f"{d}" + (f" [{p}]" if p else "") for d, p in parents[:3])

    def search(self, query: str, limit: int = 12) -> list[SchemaEntry]:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w]
        if not words:
            return []
        scored = []
        for entry in self.entries:
            keyword = entry.keyword.lower()
            enum_keys = " ".join(k.lower() for k, _ in entry.enum)
            enum_titles = " ".join(t.lower() for _, t in entry.enum)
            prose = f"{entry.title} {entry.text} {entry.condition}".lower()
            place = f"{entry.define} {' '.join(entry.dirs)}".lower()
            score = 0.0
            for word in words:
                if word == keyword:
                    score += 20
                elif keyword.startswith(word):
                    score += 8
                if re.search(rf"\b{re.escape(word)}\b", enum_keys):
                    score += 12
                if word in enum_titles:
                    score += 4
                if word in prose:
                    score += 5
                if word in place:
                    score += 2
            if score and all(w in f"{keyword} {enum_keys} {enum_titles} {prose} {place}" for w in words):
                score *= 2  # every word matched somewhere
            if score:
                scored.append((score, -len(entry.enum), entry.line, entry))
        scored.sort(key=lambda s: (-s[0], s[1], s[2]))
        return [s[3] for s in scored[:limit]]

    def describe(self, keyword: str, parent_dir: str | None = None) -> SchemaEntry | None:
        """Best entry for a keyword, preferring one inside a directory named ``parent_dir``."""
        keyword = keyword.upper()
        candidates = [e for e in self.entries if e.keyword == keyword and e.kind != "directory"]
        if parent_dir:
            parent = re.sub(r"\(\d*\)$", "", parent_dir.lower())
            exact = [e for e in candidates if e.dirs and re.sub(r"\(\d*\)$", "", e.dirs[-1].lower()) == parent]
            candidates = exact or candidates
        titled = [e for e in candidates if e.title]
        return (titled or candidates or [None])[0]


_CACHE: dict[str, Schema] = {}


def load_schema(install: config.DianaInstall) -> Schema:
    key = str(install.home)
    if key not in _CACHE:
        _CACHE[key] = Schema.load(install)
    return _CACHE[key]
