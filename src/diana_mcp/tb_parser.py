"""Streaming reader for DIANA tabulated result files (.tb, TABULA output device).

Layout written by DIANA 10.5 (all variants below were produced and checked with the real solver):

    <TEXT label>                                        Page 1    <- only with page layout (LINPAG > 0)
    Analysis type         NONLIN                                   <- block header: key, value
    Step nr.              1                                           (or Load case nr. / Mode nr. + Eigen frequency)
    Load factor           1.000E-03
    Result                STRESS TOTAL  CAUCHY                      ('Combination' for LAYOUT COMBIN)
    Operation             PRINCI
    Location of results   INTPNT
    Element type  CQ16M                                            <- extreme values (SELECT LOADS/STEPS .. MIN|MAX)
    Extreme values        SXX    -1.364E+04   Loadcasenr     1   Elmnr     1   Nodnr   177
                          SYY    -4.355E+04   Loadcasenr     1   Elmnr     2   Nodnr   178
    Elmnr Intpt        S1         S2         S3        xX    xY    xZ      <- column header
        1     1    1.166E+03  2.121E+02  0.000E+00    0.70 -0.71  0.71    <- AXES: fixed-point cosines
              2   -3.660E+02 -2.836E+03  0.000E+00   -0.74 -0.67  0.67    <- the element number is printed once
    Nodnr       DtX        DtY        DtZ        FBX        FBY        FBZ    <- COMBIN: absent values are blank
        3    6.623E-05 -4.361E-04  0.000E+00
    Nodnr            DtX                 DtY                                  <- extremes: '(load case)' after
        3    6.115E-06 (2)      -4.361E-04 (1)                                   each value ('( 1-3)' = case-truck)
        1   1   NO CRACK YET                                                   <- status results are text

Values are therefore matched to columns by position, not by counting tokens. A table wider than COLLIN
(130 characters by default) is wrapped: the column header and every row span several physical lines, which are
matched line by line. Files can be gigabytes, so both scanning and extraction stream, and the index keeps the
byte offset of each block so that one block can be read without re-reading the file.
"""
from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

_HEADER_KEYS = {
    "Analysis type": "analysis_type",
    "Step nr.": "step",
    "Load factor": "load_factor",
    "Load case nr.": "load_case",
    "Mode nr.": "mode",
    "Eigen frequency": "frequency",
    "Time": "time",
    "Result": "result",
    "Operation": "operation",
    "Axes": "axes",
    "Location of results": "location",
    "Extreme results": "extreme",
    "Element type": "element_type",
}
_HEADER_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(k) for k in sorted(_HEADER_KEYS, key=len, reverse=True)) + r")(?:\s+(.*?))?\s*$"
)
_EXTREME_RE = re.compile(
    r"^\s*(?:Extreme values)?\s+(\S+)\s+([-+]?\d[\d.]*(?:[EeDd][-+]?\d+)?)\s+Loadcasenr\s+(\d+)"
    r"(?:\s+Truckpos\s+(\d+))?(?:\s+Elmnr\s+(\d+))?(?:\s+Nodnr\s+(\d+))?"
)
_OTHER_HEADER_RE = re.compile(r"^\s*(\S.*?)\s{2,}(\S.*?)\s*$")
_PAGE_RE = re.compile(r"\sPage\s+\d+\s*$")
_TOKEN_RE = re.compile(r"\([^)]*\)|\S+")
_INT_RE = re.compile(r"^[-+]?\d+$")
_NUMBER_RE = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:[EeDd][-+]?\d+)?$|^\*+$")
_ID_NAME_RE = re.compile(r"(?i)^(\w*nr|intpt|layer|lay|ipnr|pnt)$")


class TbError(RuntimeError):
    pass


@dataclass
class TbBlock:
    index: int
    line: int
    offset: int = 0  # byte offset of the block's first header line
    label: str | None = None  # TEXT of the output block, from the page header (paged layout only)
    analysis_type: str | None = None
    step: int | None = None
    load_factor: float | None = None
    load_case: int | None = None
    mode: int | None = None  # eigenvalue analyses: 'Mode nr.'
    frequency: float | None = None  # 'Eigen frequency'
    time: float | None = None
    result: str | None = None
    operation: str | None = None  # derived quantity, e.g. VONMIS or PRINCI
    axes: str | None = None
    location: str | None = None
    extreme: str | None = None
    element_type: str | None = None
    extremes: list[dict] = field(default_factory=list)  # 'Extreme values' lines of MIN/MAX selections
    header_lines: list[list[tuple[str, float]]] = field(default_factory=list)  # (label, centre) per physical line
    rows: int = 0
    skipped: int = 0  # lines inside the block that could not be read as data rows
    extra: dict[str, str] = field(default_factory=dict)  # header lines this parser does not know

    @property
    def columns(self) -> list[str]:
        return [label for line in self.header_lines for label, _ in line]

    @property
    def id_columns(self) -> list[str]:
        ids = []
        for column in self.columns:
            if not _ID_NAME_RE.match(column):
                break
            ids.append(column)
        return ids

    @property
    def value_columns(self) -> list[str]:
        return self.columns[len(self.id_columns):]

    def describe(self) -> dict:
        keys = ("index", "line", "label", "analysis_type", "step", "load_factor", "load_case", "mode", "frequency",
                "time", "result", "operation", "axes", "location", "extreme", "element_type", "extremes",
                "columns", "rows", "skipped", "extra")
        info = {k: getattr(self, k) for k in keys}
        if not self.skipped:
            del info["skipped"]
        if len(self.header_lines) > 1:
            info["wrapped_lines_per_row"] = len(self.header_lines)
        return {k: v for k, v in info.items() if v not in (None, [], "", {})}


def _number(text: str) -> float:
    try:
        return float(text.replace("D", "E").replace("d", "e"))
    except ValueError:
        return math.nan  # overflow markers such as '*********'


def _set_header(block: TbBlock, key: str, value: str) -> None:
    attr = _HEADER_KEYS[key]
    value = re.sub(r"\s+", " ", value or "").strip()
    if attr in ("step", "load_case", "mode"):
        setattr(block, attr, int(value) if _INT_RE.match(value) else None)
    elif attr in ("load_factor", "time", "frequency"):
        setattr(block, attr, _number(value))
    else:
        setattr(block, attr, value)


def _encoding(path: Path) -> str:
    """Labels are UTF-8 when written by a localized DIANA; fall back to GBK for older files."""
    with open(path, "rb") as f:
        probe = f.read(1 << 16)
    try:
        probe.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < len(probe) - 4:  # not just a multi-byte character cut at the probe boundary
            return "gbk"
    return "utf-8"


def _lines(path: Path, encoding: str, start: int = 0) -> Iterator[tuple[int, str]]:
    """(byte offset, text) of every line from ``start``; binary reading keeps the offsets exact."""
    with open(path, "rb") as f:
        f.seek(start)
        offset = start
        for raw in f:
            yield offset, raw.decode(encoding, errors="replace").rstrip("\r\n")
            offset += len(raw)


def _tokens(line: str) -> list[tuple[str, float]]:
    return [(m.group(0), (m.start() + m.end() - 1) / 2) for m in _TOKEN_RE.finditer(line)]


def _is_label_line(tokens: list[tuple[str, float]]) -> bool:
    return bool(tokens) and not any(_NUMBER_RE.match(t) for t, _ in tokens)


def _is_data_line(tokens: list[tuple[str, float]]) -> bool:
    return bool(tokens) and bool(_NUMBER_RE.match(tokens[0][0]))


def _match_line(header: list[tuple[str, float]], tokens: list[tuple[str, float]], out: dict, notes: dict) -> None:
    """Assign the tokens of one physical data line to the header labels by position."""
    text: list[str] = []
    last_label: str | None = None
    column = 0
    for token, centre in tokens:
        if token.startswith("(") and token.endswith(")"):
            if last_label is not None:  # '(12  )' / '( 1-3)': load case (and truck position) of the extreme
                notes[last_label] = re.sub(r"\s+", "", token[1:-1])
            continue
        if not _NUMBER_RE.match(token):
            text.append(token)
            continue
        if column >= len(header):
            text.append(token)
            continue
        # nearest label at or after the current column, keeping the order of the columns
        best = min(range(column, len(header)), key=lambda k: abs(header[k][1] - centre))
        label = header[best][0]
        out[label] = token
        last_label = label
        column = best + 1
    if text:
        out["value"] = (out.get("value", "") + " " + " ".join(text)).strip()


def _convert(block: TbBlock, raw: dict, previous: dict) -> tuple[dict, bool]:
    """Typed row; missing leading ids are inherited from the previous row (element rows print them once)."""
    row: dict = {}
    ids = block.id_columns
    complete = True
    for column in ids:
        if column in raw and _INT_RE.match(raw[column]):
            row[column] = int(raw[column])
        elif column in previous:
            row[column] = previous[column]
        else:
            complete = False
    for column in block.value_columns:
        if column in raw:
            row[column] = _number(raw[column])
    if "value" in raw:
        row["value"] = raw["value"]
    return row, (complete and bool(ids)) or (not ids and bool(raw))


_INDEX_CACHE: dict[tuple[str, int, int], list[TbBlock]] = {}


def scan_tb(path: Path) -> list[TbBlock]:
    """Index of all result blocks with their byte offsets (no data kept). Cached per file version."""
    if not path.is_file():
        raise TbError(f"file not found: {path}")
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if key not in _INDEX_CACHE:
        if len(_INDEX_CACHE) >= 16:
            _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
        _INDEX_CACHE[key] = [block for block, _ in _walk(path, _encoding(path), collect=False)]
    return _INDEX_CACHE[key]


def iter_block_rows(path: Path, block: TbBlock, encoding: str | None = None) -> Iterator[dict]:
    """Typed rows of one indexed block, read by seeking to its offset."""
    for _, rows in _walk(path, encoding or _encoding(path), collect=True, start=block.offset, single=True):
        yield from rows


def _walk(path: Path, encoding: str, collect: bool, start: int = 0, single: bool = False) -> Iterator[tuple[TbBlock, list]]:
    block: TbBlock | None = None
    rows: list[dict] = []
    labels: list[list[tuple[str, float]]] = []  # label-only lines seen in the header region
    in_header = False
    pending: list[list[tuple[str, float]]] | None = None  # physical lines of a wrapped row being collected
    previous: dict = {}
    notes: dict = {}
    page_label: str | None = None
    index = -1

    def finish_row():
        nonlocal previous
        raw: dict = {}
        row_notes: dict = {}
        for header, tokens in zip(block.header_lines, pending):
            _match_line(header, tokens, raw, row_notes)
        row, ok = _convert(block, raw, previous)
        if not ok:
            block.skipped += 1
            return
        previous = row
        block.rows += 1
        if collect:
            for column, case in row_notes.items():
                row[f"{column} case"] = case
            rows.append(row)

    for number, (offset, line) in enumerate(_lines(path, encoding, start), start=1):
        if pending is not None and block is not None and len(pending) < len(block.header_lines):
            pending.append(_tokens(line) if collect else [])  # continuation line of a wrapped row, even if blank
            if len(pending) == len(block.header_lines):
                if collect:
                    finish_row()
                else:
                    block.rows += 1
                pending = None
            continue
        stripped = line.lstrip()
        if not stripped:
            continue
        if not collect and not in_header and block is not None and block.header_lines and (
                stripped[0].isdigit() or stripped[0] in "-+*"):
            # Index only: count data rows without matching them to columns (much faster on huge files).
            if len(block.header_lines) == 1:
                block.rows += 1
            else:
                pending = [[]]
            continue
        if _PAGE_RE.search(line) and not _HEADER_RE.match(line):
            page_label = _PAGE_RE.sub("", line).strip() or None
            continue  # the column header repeated after it is skipped below
        header = _HEADER_RE.match(line)
        if header and (in_header or header.group(1) == "Analysis type" or block is None):
            if header.group(1) == "Analysis type" or block is None or not in_header:
                if block is not None and block.header_lines:
                    yield block, rows
                    if single:
                        return
                index += 1
                block = TbBlock(index=index, line=number, offset=offset, label=page_label)
                rows, labels, previous, in_header = [], [], {}, True
            _set_header(block, header.group(1), header.group(2) or "")
            continue
        if block is None:
            continue
        tokens = _tokens(line)
        if in_header:
            extreme = _EXTREME_RE.match(line)
            if extreme:
                component, value, case, truck, element, node = extreme.groups()
                entry = {"component": component, "value": _number(value), "load_case": int(case)}
                entry.update({k: int(v) for k, v in (("truck_position", truck), ("element", element), ("node", node)) if v})
                block.extremes.append(entry)
                continue
            if _is_label_line(tokens):
                labels.append(tokens)
                continue
            if not _is_data_line(tokens):
                other = _OTHER_HEADER_RE.match(line)
                if other:
                    block.extra[other.group(1)] = other.group(2)
                continue
            # First data line: the column header is the run of label lines starting at the last one that
            # begins with an id column (several lines when the table is wrapped).
            starts = [k for k, l in enumerate(labels) if _ID_NAME_RE.match(l[0][0])]
            first = starts[-1] if starts else len(labels) - 1
            if first < 0:
                block.skipped += 1
                continue
            for extra_line in labels[:first]:
                block.extra[" ".join(t for t, _ in extra_line)] = ""
            block.header_lines = labels[first:]
            in_header = False
        if _is_label_line(tokens):
            continue  # column header (lines) repeated after a page break
        if not _is_data_line(tokens):
            block.skipped += 1
            continue
        pending = [tokens]
        if len(block.header_lines) == 1:
            finish_row()
            pending = None
    if pending is not None and block is not None:
        pending += [[] for _ in range(len(block.header_lines) - len(pending))]
        finish_row()
    if block is not None and block.header_lines:
        yield block, rows


def _result_tokens(block: TbBlock) -> list[str]:
    return f"{block.result or ''} {block.operation or ''}".upper().split()


def _matches(block: TbBlock, result: str | None, step: int | None, load_case: int | None, mode: int | None) -> bool:
    if result:
        have = _result_tokens(block)
        if not all(any(h.startswith(want) for h in have) for want in result.upper().split()):
            return False
    for wanted, actual in ((step, block.step), (load_case, block.load_case), (mode, block.mode)):
        if wanted is not None and actual != wanted:
            return False
    return True


def select_blocks(blocks: list[TbBlock], result: str | None = None, step: int | None = None,
                  load_case: int | None = None, mode: int | None = None) -> list[TbBlock]:
    """Blocks whose result/operation contain every word of ``result`` (as word prefixes) and match the numbers."""
    return [b for b in blocks if _matches(b, result, step, load_case, mode)]


def read_tb_rows(
    path: Path,
    blocks: list[TbBlock],
    node_ids: Iterable[int] | None = None,
    element_ids: Iterable[int] | None = None,
    ids: dict[str, Iterable[int]] | None = None,
) -> Iterator[dict]:
    """Rows of the given blocks as dicts, optionally filtered by node / element numbers or any id column."""
    filters = {k.lower(): set(v) for k, v in (ids or {}).items()}
    if node_ids:
        filters["nodnr"] = set(node_ids)
    if element_ids:
        filters["elmnr"] = set(element_ids)
    encoding = _encoding(path)
    for block in sorted(blocks, key=lambda b: b.offset):
        lowered = {c.lower(): c for c in block.id_columns}
        if any(name not in lowered for name in filters):
            continue  # the block does not have a filtered id column, e.g. node filter on an Elmnr/Intpt table
        for row in iter_block_rows(path, block, encoding):
            if any(row.get(lowered[name]) not in wanted for name, wanted in filters.items()):
                continue
            out = {"block": block.index, "step": block.step, "load_factor": block.load_factor,
                   "load_case": block.load_case, "result": block.result}
            if block.mode is not None:
                out.update(mode=block.mode, frequency=block.frequency)
            if block.time is not None:
                out["time"] = block.time
            if block.operation:
                out["operation"] = block.operation
            out.update(row)
            yield out


def write_csv(path: Path, rows: Iterable[dict]) -> int:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        fields.extend(k for k in row if k not in fields)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
