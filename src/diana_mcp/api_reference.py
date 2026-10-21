"""Searchable DianaIE API reference generated from the local DIANA installation.

Every callable in DianaIE's scripting namespace is dumped together with its docstring, so names and
signatures always match the installed version. The dump is cached under the diana-mcp state
directory and never shipped with the package: the docstrings belong to DIANA FEA BV.
"""
from __future__ import annotations

import difflib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config

CACHE_FORMAT = 2  # bump when the dump changes, so old caches are regenerated

# Runs inside DianaIE (Python 3.7); ``globals()`` is DianaIE's scripting namespace. Commands wrapped by the
# 10.4 compatibility layer (diana.compat.*) have no docstring of their own; their documentation is taken from
# the wrapped original in the other diana.* modules.
DUMP_FUNCTION = r'''
def _diana_mcp_dump_api(path):
    import builtins, json, sys
    namespace = globals()
    standard = set(dir(builtins))
    foreign = {"builtins", "functools", "itertools", "collections", "collections.abc", "_collections_abc", "abc", "typing"}
    originals = [m for n, m in sorted(sys.modules.items()) if n.startswith("diana.") and not n.startswith("diana.compat")]
    functions, constants = {}, {}
    for name in sorted(namespace):
        if name.startswith("_") or name in standard:
            continue
        obj = namespace[name]
        if isinstance(obj, str) and name.isupper():
            constants[name] = obj
        elif callable(obj) and getattr(obj, "__module__", "") not in foreign:
            doc = (getattr(obj, "__doc__", None) or "").strip()
            if not doc and str(getattr(obj, "__module__", "")).startswith("diana.compat"):
                for module in originals:
                    original = getattr(module, name, None)
                    if original is not None and original is not obj and (getattr(original, "__doc__", None) or "").strip():
                        doc = original.__doc__.strip() + "\n\n(Deprecated 10.4 argument forms are still accepted.)"
                        break
            functions[name] = {"doc": doc, "kind": type(obj).__name__}
    version = str(namespace["diaVersion"]()) if "diaVersion" in namespace else "unknown"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"format": FORMAT, "diana_version": version, "functions": functions, "constants": constants}, f)
    return len(functions)
'''.replace("FORMAT", str(CACHE_FORMAT))

_WORDS = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|\d+")
_SWIG_INTERNAL = re.compile(r"_swigregister$|^SwigPy|^ParameterValue_|^ParameterTypeFromVariantType$")


def words(text: str) -> list[str]:
    """Split identifiers and prose into lower-case words: 'addMaterialSafetyFactors' -> add, material, ..."""
    return [w.lower() for w in _WORDS.findall(text)]


def version_slug(diana_version: str) -> str:
    """'Diana 10.5, Latest update: ...' or '10.5' -> '10.5'."""
    match = re.search(r"(\d+(?:\.\d+)+)", diana_version)
    return match.group(1) if match else "unknown"


@dataclass(frozen=True)
class ApiEntry:
    name: str
    doc: str
    kind: str

    @property
    def summary(self) -> str:
        return next((line.strip() for line in self.doc.splitlines() if line.strip()), "")

    @property
    def usages(self) -> list[str]:
        pattern = rf"^\s*({re.escape(self.name)}\s*\(.*\))\s*$"
        return [m.strip() for m in re.findall(pattern, self.doc, flags=re.MULTILINE)]

    def one_line(self) -> str:
        signature = self.usages[0] if self.usages else f"{self.name}(...)"
        return f"{signature} — {self.summary}" if self.summary else signature


class ApiReference:
    def __init__(self, diana_version: str, functions: dict[str, dict], constants: dict[str, str] | None = None):
        self.diana_version = diana_version
        self.entries = {
            name: ApiEntry(name, spec.get("doc", ""), spec.get("kind", ""))
            for name, spec in functions.items()
            if not _SWIG_INTERNAL.search(name)
        }
        self.constants = constants or {}
        self._lower = {name.lower(): name for name in self.entries}

    @classmethod
    def load(cls, path: Path) -> ApiReference:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(data.get("diana_version", "unknown"), data.get("functions", {}), data.get("constants", {}))

    def search(self, query: str, limit: int = 15) -> list[ApiEntry]:
        text = query.strip().lower()
        terms = set(words(query)) | set(re.findall(r"[a-z0-9]+", text))
        scored = []
        for entry in self.entries.values():
            name = entry.name.lower()
            score = 0.0
            if name == text.replace(" ", ""):
                score += 100
            elif text and name.startswith(text):
                score += 40
            elif text and text in name:
                score += 25
            name_words = set(words(entry.name))
            summary = entry.summary.lower()
            doc = entry.doc.lower()
            for term in terms:
                if term in name_words:
                    score += 12
                elif term in name:
                    score += 6
                if term in summary:
                    score += 3
                if term in doc:
                    score += 1
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], len(item[1].name), item[1].name))
        return [entry for _, entry in scored[:limit]]

    def get(self, name: str) -> ApiEntry | None:
        exact = self.entries.get(name) or self.entries.get(self._lower.get(name.lower(), ""))
        return exact

    def suggest(self, name: str, n: int = 8) -> list[str]:
        matches = difflib.get_close_matches(name.lower(), list(self._lower), n=n, cutoff=0.6)
        return [self._lower[m] for m in matches]


def dump_code(output_path: Path) -> str:
    return (f"{DUMP_FUNCTION}\ntry:\n    print(_diana_mcp_dump_api({config.to_windows(output_path)!r}))\n"
            "finally:\n    del _diana_mcp_dump_api\n")


def generate_with_batch_dianaie(install: config.DianaInstall, output_path: Path, timeout: float = 300) -> None:
    """Dump the API with a short-lived, non-interactive DianaIE run (no live session needed)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="diana-mcp-") as tmp:
        script = Path(tmp) / "dump_api.py"
        script.write_text(dump_code(output_path), encoding="utf-8")
        subprocess.run(
            config.batch_argv(install.dianaie, "--noSplash", "--workingDir",
                              config.to_windows(tmp), config.to_windows(script)),
            cwd=tmp, timeout=timeout, check=False, env=config.launch_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    if not output_path.is_file():
        raise RuntimeError(f"DianaIE did not write the API reference to {output_path}")


class ApiReferenceStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or config.api_dir()
        self._loaded: dict[str, ApiReference] = {}

    def path_for(self, version: str) -> Path:
        return self.directory / f"diana-{version_slug(version)}.json"

    def cached_versions(self) -> list[str]:
        return sorted(p.stem.removeprefix("diana-") for p in self.directory.glob("diana-*.json"))

    def get(self, version: str, generate: Callable[[Path], None]) -> ApiReference:
        """Load the reference for ``version``, calling ``generate(path)`` first if it is not cached."""
        slug = version_slug(version)
        if slug not in self._loaded:
            path = self.path_for(version)
            if not path.is_file() or json.loads(path.read_text(encoding="utf-8")).get("format") != CACHE_FORMAT:
                self.directory.mkdir(parents=True, exist_ok=True)
                generate(path)
            self._loaded[slug] = ApiReference.load(path)
        return self._loaded[slug]
