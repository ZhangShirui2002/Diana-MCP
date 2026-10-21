"""Markdown files with a YAML front matter block, limited to the YAML subset the experience handbook uses.

Supported between the two ``---`` lines (all valid YAML, so editors and tools like Obsidian read the files too):

    key: plain text or "double quoted" or 'single quoted'
    key: [flow, "list of", scalars]
    key:
      - block list item
    key: |
      block text over
      several lines
    key:
      nested: mapping of scalars (one level)

Full-line ``#`` comments and `` #`` comments after plain scalars are ignored. Everything is read as strings, lists
of strings or one-level mappings; the writer quotes values that YAML would otherwise read as numbers, booleans or
null, so real YAML parsers see the same strings.
"""
from __future__ import annotations

import json
import re
from typing import Any

FENCE = "---"
_KEY = re.compile(r"^([A-Za-z_][\w-]*):(?:\s+(.*)|\s*)$")
_NEEDS_QUOTES = re.compile(r"""^(?:[-?:,\[\]{}#&*!|>'"%@`]|\s)|[\s]$|:\s|\s#|^(?:true|false|yes|no|on|off|null|~)$|^[-+.\d]""",
                           re.IGNORECASE)


class FrontMatterError(ValueError):
    def __init__(self, message: str, line: int | None = None):
        super().__init__(f"line {line}: {message}" if line else message)
        self.line = line


def split(text: str) -> tuple[str, str, int]:
    """(front matter, body, line number where the body starts); raises if there is no front matter block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        raise FrontMatterError("the file must start with a '---' line", 1)
    for index in range(1, len(lines)):
        if lines[index].strip() == FENCE:
            body = "\n".join(lines[index + 1:])
            return "\n".join(lines[1:index]), body.lstrip("\n"), index + 2
    raise FrontMatterError("no closing '---' line after the front matter", 1)


def _scalar(raw: str, line: int) -> Any:
    raw = raw.strip()
    if raw.startswith('"'):
        end = _closing_quote(raw, '"')
        if end < 0:
            raise FrontMatterError(f"unterminated string: {raw}", line)
        return json.loads(raw[: end + 1])
    if raw.startswith("'"):
        end = _closing_quote(raw, "'")
        if end < 0:
            raise FrontMatterError(f"unterminated string: {raw}", line)
        return raw[1:end].replace("''", "'")
    raw = re.split(r"\s#", raw, maxsplit=1)[0].strip()
    if raw in ("", "~", "null", "Null", "NULL"):
        return None
    return raw


def _closing_quote(raw: str, quote: str) -> int:
    i = 1
    while i < len(raw):
        if quote == '"' and raw[i] == "\\":
            i += 2
            continue
        if raw[i] == quote:
            if quote == "'" and i + 1 < len(raw) and raw[i + 1] == "'":
                i += 2
                continue
            return i
        i += 1
    return -1


def _flow_list(raw: str, line: int) -> list[Any]:
    inner = raw.strip()
    if not inner.endswith("]"):
        raise FrontMatterError(f"a flow list must end with ']' on the same line: {raw}", line)
    inner = inner[1:-1].strip()
    items, current, quote = [], "", None
    i = 0
    while i < len(inner):
        ch = inner[i]
        if quote:
            current += ch
            if ch == "\\" and quote == '"' and i + 1 < len(inner):
                current += inner[i + 1]
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'" and not current.strip():
            quote = ch
            current += ch
        elif ch == ",":
            items.append(current)
            current = ""
        else:
            current += ch
        i += 1
    if current.strip():
        items.append(current)
    return [_scalar(item, line) for item in items if item.strip()]


def _value(raw: str, line: int) -> Any:
    raw = raw.strip()
    if raw.startswith("["):
        return _flow_list(raw, line)
    return _scalar(raw, line)


def parse(block: str, first_line: int = 2) -> dict[str, Any]:
    """The front matter block as {key: str | list | dict | None}."""
    lines = block.splitlines()
    out: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        number = first_line + i
        text = lines[i]
        if not text.strip() or text.lstrip().startswith("#"):
            i += 1
            continue
        if text[:1].isspace():
            raise FrontMatterError(f"unexpected indentation: {text.strip()}", number)
        match = _KEY.match(text.rstrip())
        if not match:
            raise FrontMatterError(f"expected 'key: value', got: {text.strip()}", number)
        key, raw = match.group(1), (match.group(2) or "").strip()
        if key in out:
            raise FrontMatterError(f"duplicate key '{key}'", number)
        i += 1
        if raw in ("|", "|-", ">", ">-"):
            chunk = []
            while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
                chunk.append(lines[i])
                i += 1
            indent = min((len(l) - len(l.lstrip()) for l in chunk if l.strip()), default=0)
            text_lines = [l[indent:] for l in chunk]
            while text_lines and not text_lines[-1].strip():
                text_lines.pop()
            out[key] = ("\n" if raw.startswith("|") else " ").join(text_lines)
            continue
        if raw and not raw.startswith("#"):
            out[key] = _value(raw, number)
            continue
        nested = []
        while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()):
            if lines[i].strip() and not lines[i].lstrip().startswith("#"):
                nested.append((first_line + i, lines[i].strip()))
            i += 1
        if not nested:
            out[key] = None
        elif all(item.startswith("- ") or item == "-" for _, item in nested):
            out[key] = [_value(item[1:], n) for n, item in nested]
        else:
            mapping: dict[str, Any] = {}
            for n, item in nested:
                sub = _KEY.match(item)
                if not sub:
                    raise FrontMatterError(f"expected 'key: value' in '{key}', got: {item}", n)
                mapping[sub.group(1)] = _value(sub.group(2) or "", n)
            out[key] = mapping
    return out


def quote(value: str, in_flow: bool = False) -> str:
    if value == "" or _NEEDS_QUOTES.search(value) or "\n" in value or (in_flow and any(c in value for c in ",[]{}")):
        return json.dumps(value, ensure_ascii=False)
    return value


def _dump_value(value: Any, in_flow: bool = False) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return quote(str(value), in_flow)


def dump(data: dict[str, Any]) -> str:
    """Front matter text (without the fences) for ``data``, keeping its key order."""
    out = []
    for key, value in data.items():
        if value is None or value == [] or value == {}:
            out.append(f"{key}: []" if isinstance(value, list) else f"{key}:")
        elif isinstance(value, list):
            flow = "[" + ", ".join(_dump_value(v, in_flow=True) for v in value) + "]"
            if len(flow) <= 100:
                out.append(f"{key}: {flow}")
            else:
                out.append(f"{key}:")
                out.extend(f"  - {_dump_value(v)}" for v in value)
        elif isinstance(value, dict):
            out.append(f"{key}:")
            out.extend(f"  {k}: {_dump_value(v)}" for k, v in value.items())
        elif isinstance(value, str) and "\n" in value:
            out.append(f"{key}: |")
            out.extend(f"  {line}" if line else "" for line in value.splitlines())
        else:
            out.append(f"{key}: {_dump_value(value)}")
    return "\n".join(out)


def read(text: str) -> tuple[dict[str, Any], str]:
    block, body, body_line = split(text)
    return parse(block), body


def write(data: dict[str, Any], body: str) -> str:
    return f"{FENCE}\n{dump(data)}\n{FENCE}\n\n{body.strip()}\n"
