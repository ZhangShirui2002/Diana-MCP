"""Export the experience handbook as one readable document: Markdown and a self-contained HTML page.

Entries are grouped by type, each with its status, triggers, verification result and usage, followed by an index
of error codes and tags. The HTML opens in any browser and prints to PDF; nothing outside this file is needed.
"""
from __future__ import annotations

import datetime as dt
import html
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .experience import SOURCES, STATUSES, TYPES, Entry, ExperienceStore
from .usage import Usage

STATUS_ORDER = ("verified", "draft", "disputed", "obsolete")


def _anchor(entry_id: str) -> str:
    return "e-" + entry_id


def _demote(body: str) -> str:
    """Entry sections become level-4 headings inside the document."""
    return re.sub(r"^(#{1,3})\s", lambda m: "#" * (len(m.group(1)) + 2) + " ", body, flags=re.MULTILINE)


def build_markdown(entries: list[Entry], stats: dict[str, Usage], title: str = "DIANA 经验手册") -> str:
    today = dt.date.today().isoformat()
    counts = {STATUSES[s]: sum(1 for e in entries if e.status == s) for s in STATUS_ORDER}
    versions = sorted({e.diana_version for e in entries if e.diana_version})
    lines = [f"# {title}", "",
             f"生成于 {today}，共 {len(entries)} 条（" + "，".join(f"{k} {v}" for k, v in counts.items() if v) + "）"
             + (f"；DIANA 版本 {'、'.join(versions)}" if versions else "") + "。", "",
             "状态说明：已验证 = 用户确认过；未验证 = 草稿，仅供参考；存疑 = 有反例或验证失败；过时 = 不再适用。", ""]
    by_type: dict[str, list[Entry]] = defaultdict(list)
    for entry in entries:
        by_type[entry.type].append(entry)
    lines += ["## 目录", ""]
    for kind, label in TYPES.items():
        if by_type.get(kind):
            lines.append(f"- [{label}](#t-{kind})（{len(by_type[kind])} 条）")
    lines.append("- [错误码与标签索引](#index)")
    lines.append("")
    for kind, label in TYPES.items():
        group = sorted(by_type.get(kind, []), key=lambda e: (STATUS_ORDER.index(e.status), e.title))
        if not group:
            continue
        lines += [f'## {label} <a id="t-{kind}"></a>', ""]
        for entry in group:
            lines += [f'### {entry.title} <a id="{_anchor(entry.id)}"></a>', ""]
            meta = [f"`{entry.id}`", f"**{STATUSES[entry.status]}**", SOURCES[entry.source]]
            if entry.diana_version:
                meta.append(f"DIANA {entry.diana_version}")
            if entry.updated:
                meta.append(f"更新于 {entry.updated}")
            if entry.checks:
                result = "自动验证通过" if entry.checks.get("result") == "pass" else "自动验证失败"
                meta.append(f"{result}（{entry.checks.get('date')}，DIANA {entry.checks.get('diana_version')}）")
            use = stats.get(entry.id)
            if use:
                counts = [f"{label} {n} 次" for label, n in (("被推荐", use.recommended), ("检索命中", use.shown),
                                                             ("标为有用", use.helpful), ("标为无用", use.unhelpful)) if n]
                if counts:
                    meta.append("、".join(counts))
            lines += ["　·　".join(meta), ""]
            if entry.triggers:
                lines += ["触发：" + "、".join(f"`{t}`" for t in entry.triggers), ""]
            if entry.tags:
                lines += ["标签：" + "、".join(entry.tags), ""]
            if entry.manual:
                lines += ["手册：" + "、".join(f"`{m}`" for m in entry.manual), ""]
            lines += [_demote(entry.body).strip(), ""]
            if entry.evidence:
                lines += ["#### 证据", ""] + [f"- {e}" for e in entry.evidence] + [""]
            if entry.approval:
                lines += [f"> 批准：{entry.approval.get('date')}，{entry.approval.get('by')}：{entry.approval.get('note')}", ""]
            if entry.dispute:
                lines += [f"> {STATUSES[entry.status]}：{entry.dispute.get('date')}，{entry.dispute.get('note')}", ""]
    codes: dict[str, list[Entry]] = defaultdict(list)
    tags: dict[str, list[Entry]] = defaultdict(list)
    for entry in entries:
        for trigger in entry.triggers:
            if re.search(r"/DIANA/|\d", trigger):
                codes[trigger].append(entry)
        for tag in entry.tags:
            tags[tag].append(entry)
    lines += ['## 错误码与标签索引 <a id="index"></a>', "", "| 错误码 / 触发 | 经验 |", "|---|---|"]
    for code in sorted(codes):
        links = "；".join(f"[{e.title[:40]}](#{_anchor(e.id)})" for e in codes[code])
        lines.append(f"| `{code}` | {links} |")
    lines += ["", "| 标签 | 条数 |", "|---|---|"] + [f"| {t} | {len(v)} |" for t, v in sorted(tags.items())]
    return "\n".join(lines) + "\n"


# --- a small Markdown renderer for the constructs the handbook uses ---------------------------------------------

def _inline(text: str) -> str:
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
            continue
        escaped = html.escape(part)
        escaped = re.sub(r"&lt;a id=&quot;([\w-]+)&quot;&gt;&lt;/a&gt;", r'<a id="\1"></a>', escaped)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"\[([^\]]+)\]\((#[\w-]+)\)", r'<a href="\2">\1</a>', escaped)
        out.append(escaped)
    return "".join(out)


def markdown_to_html(markdown: str) -> str:
    out: list[str] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = re.match(r"^```(\w*)\s*$", line.strip())
        if fence:
            code = []
            i += 1
            while i < len(lines) and lines[i].strip() != "```":
                code.append(lines[i])
                i += 1
            css = f' class="language-{fence.group(1)}"' if fence.group(1) else ""
            rendered = []
            for row in code:
                cls = ' class="add"' if fence.group(1) == "diff" and row.startswith("+") else (
                    ' class="del"' if fence.group(1) == "diff" and row.startswith("-") else "")
                rendered.append(f"<span{cls}>{html.escape(row)}</span>" if cls else html.escape(row))
            out.append(f"<pre><code{css}>" + "\n".join(rendered) + "</code></pre>")
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            body = [r for r in rows if not all(re.fullmatch(r":?-+:?", c) for c in r)]
            if body:
                head, *rest = body
                out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                           + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rest)
                           + "</tbody></table>")
            continue
        if line.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i][1:].strip())
                i += 1
            out.append("<blockquote>" + "<br>".join(_inline(q) for q in quote) + "</blockquote>")
            continue
        if re.match(r"^\s*- ", line):
            items = []
            while i < len(lines) and re.match(r"^\s*- ", lines[i]):
                items.append(re.sub(r"^\s*- ", "", lines[i]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + "</ul>")
            continue
        if not line.strip():
            i += 1
            continue
        paragraph = []
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,6}\s|```|\||>|\s*- )", lines[i]):
            paragraph.append(lines[i])
            i += 1
        out.append("<p>" + "<br>".join(_inline(p) for p in paragraph) + "</p>")
    return "\n".join(out)


STYLE = """
:root { --bg: #fdfcf9; --fg: #1f2328; --muted: #6a737d; --line: #e3e0d8; --code: #f3f1ec; --accent: #2f5d8a;
        --ok: #2e7d4f; --draft: #8a6d1f; --warn: #b3541e; --add: #1f7a3a; --del: #b42318; }
@media (prefers-color-scheme: dark) { :root { --bg: #16181c; --fg: #e6e6e6; --muted: #9aa4ae; --line: #2c3036;
        --code: #20242a; --accent: #7fb0e0; --ok: #6cc28f; --draft: #d8b45a; --warn: #e8905a; --add: #72c98a; --del: #f08a80; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.7 "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; }
main { max-width: 960px; margin: 0 auto; padding: 32px 16px 80px; }
h1 { font-size: 28px; margin: 0 0 8px; } h2 { margin-top: 48px; padding-bottom: 6px; border-bottom: 2px solid var(--line); }
h3 { margin-top: 36px; color: var(--accent); } h4 { margin: 18px 0 6px; font-size: 15px; color: var(--muted); }
code { background: var(--code); padding: 1px 5px; border-radius: 4px; font: 13px/1.5 Consolas, "Cascadia Mono", monospace; }
pre { background: var(--code); padding: 12px 14px; border-radius: 6px; overflow-x: auto; }
pre code { padding: 0; background: none; } .add { color: var(--add); } .del { color: var(--del); }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--line); padding: 5px 9px; text-align: left; vertical-align: top; }
blockquote { margin: 12px 0; padding: 6px 14px; border-left: 4px solid var(--line); color: var(--muted); }
a { color: var(--accent); } strong { color: var(--ok); }
@media print { main { max-width: none; } h3 { break-after: avoid; } pre { white-space: pre-wrap; } }
"""


def to_html(markdown: str, title: str = "DIANA 经验手册") -> str:
    return (f'<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>'
            f"<style>{STYLE}</style></head><body><main>\n{markdown_to_html(markdown)}\n</main></body></html>\n")


def export(store: ExperienceStore, stats: dict[str, Usage], folder: Path | None = None,
           statuses: Iterable[str] = ("verified", "draft", "disputed"), title: str = "DIANA 经验手册") -> dict[str, Any]:
    wanted = set(statuses)
    entries = [e for e in store.entries() if e.status in wanted]
    folder = folder or store.root / "export"
    folder.mkdir(parents=True, exist_ok=True)
    markdown = build_markdown(entries, stats, title)
    md_path, html_path = folder / f"{title}.md", folder / f"{title}.html"
    md_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(to_html(markdown, title), encoding="utf-8")
    return {"entries": len(entries), "markdown": str(md_path), "html": str(html_path)}
