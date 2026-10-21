import pytest

from diana_mcp import frontmatter as fm

TEXT = """\
---
id: attach-snaps
title: attach() 只会吸附到已有顶点
type: api-usage   # comment after a plain value
status: 已验证
diana_version: "10.5"
triggers: [attach, "re:attach.*False", "a, b"]
tags:
  - 荷载
  - 支座
manual: []
note: |
  first line
    indented line

  after a blank line
approval:
  date: "2026-09-22"
  by: 用户
  note: '同意，it''s fine'
empty:
---

## 现象

body text
"""


def test_parse_the_supported_subset():
    data, body = fm.read(TEXT)
    assert data["id"] == "attach-snaps" and data["type"] == "api-usage" and data["status"] == "已验证"
    assert data["diana_version"] == "10.5"
    assert data["triggers"] == ["attach", "re:attach.*False", "a, b"]
    assert data["tags"] == ["荷载", "支座"] and data["manual"] == []
    assert data["note"] == "first line\n  indented line\n\nafter a blank line"
    assert data["approval"] == {"date": "2026-09-22", "by": "用户", "note": "同意，it's fine"}
    assert data["empty"] is None
    assert body.startswith("## 现象")


def test_roundtrip_quotes_what_yaml_would_reinterpret():
    data = {"id": "x-y", "version": "10.5", "date": "2026-09-22", "flag": "yes", "colon": "a: b",
            "hash": "x #y", "list": ["a, b", "[c]", "普通"], "multi": "one\ntwo", "none": None,
            "nested": {"by": "用户", "note": "ok: fine"}}
    text = fm.write(data, "## 现象\n\ntext")
    assert 'version: "10.5"' in text and 'flag: "yes"' in text and 'colon: "a: b"' in text
    parsed, body = fm.read(text)
    assert parsed == data and body.strip() == "## 现象\n\ntext"


@pytest.mark.parametrize("text, message", [
    ("no front matter", "must start"),
    ("---\nid: x\n", "no closing"),
    ("---\nid: x\nid: y\n---\n", "duplicate key"),
    ("---\n  indented: x\n---\n", "unexpected indentation"),
    ("---\njust text\n---\n", "expected 'key: value'"),
    ('---\ntitle: "open\n---\n', "unterminated"),
    ("---\ntags: [a, b\n---\n", "flow list"),
])
def test_errors_name_the_line(text, message):
    with pytest.raises(fm.FrontMatterError, match=message):
        fm.read(text)
