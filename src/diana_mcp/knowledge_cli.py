"""python -m diana_mcp knowledge init|status|validate|commit -m "说明"

Maintenance of the experience handbook (knowledge/): create the repository, count entries per status, check the
format of hand-edited files, and commit hand edits (tools commit their own changes).
"""
from __future__ import annotations

import argparse
import sys
import time

from .experience import STATUSES, ExperienceStore


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m diana_mcp knowledge", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("init", "status", "validate", "commit", "mine-outs", "verify", "handbook",
                                           "evaluate", "vector-index", "vector-status", "vector-off"))
    parser.add_argument("paths", nargs="*", help="folders or .out files (mine-outs), entry ids (verify), "
                                                 "evaluation files (evaluate)")
    parser.add_argument("-m", "--message", help="commit message (for commit)")
    parser.add_argument("--model", help="vector-index: embedding model spec, e.g. sentence-transformers:BAAI/bge-m3; "
                                        "evaluate: compare with this built index instead of the active one (none = off)")
    parser.add_argument("--device", help="vector-index: auto (GPU if available, default), cuda or cpu; "
                                         "evaluate: device for the query model")
    parser.add_argument("--weight", type=float, help="evaluate: weight of the vector retriever in the fusion")
    parser.add_argument("--no-activate", action="store_true", help="vector-index: build without making it active")
    parser.add_argument("--dry-run", action="store_true", help="mine-outs: only report what would be created")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    store = ExperienceStore()
    if args.action == "verify":
        from . import config
        from .verify import verify

        report = verify(store, config.default_install(), args.paths or None)
        print(f"DIANA {report['diana_version']}：检查 {report['checked']} 条，通过 {len(report.get('passed', []))}，"
              f"失败 {len(report.get('failed', []))}")
        for failure in report.get("failed", []):
            print(f"  失败 {failure['id']}：{failure['error']}")
        for key in ("not_run", "without_script", "note"):
            if report.get(key):
                print(f"  {key}: {report[key]}")
        return 1 if report.get("failed") else 0
    if args.action == "handbook":
        from . import usage
        from .handbook import export

        result = export(store, usage.experience_usage())
        print(f"已导出 {result['entries']} 条：\n  {result['html']}\n  {result['markdown']}")
        return 0
    if args.action == "evaluate":
        import os

        from .evaluation import main as evaluate_main

        for option, variable in ((args.model, "DIANA_MCP_VECTOR_MODEL"), (args.weight, "DIANA_MCP_VECTOR_WEIGHT"),
                                 (args.device, "DIANA_MCP_VECTOR_DEVICE")):
            if option is not None:
                os.environ[variable] = str(option)
        return evaluate_main(args.paths)
    if args.action in ("vector-index", "vector-status", "vector-off"):
        from . import vector
        from .evaluation import default_base

        if args.action == "vector-off":
            vector.disable()
            print("已关闭向量检索（索引文件保留）")
            return 0
        if args.action == "vector-status":
            for key, value in vector.status().items():
                print(f"{key}: {value}")
            return 0
        if not args.model:
            print("请用 --model 指定嵌入模型，例如 --model sentence-transformers:BAAI/bge-m3")
            return 2
        started, shown = time.time(), [0.0]

        def progress(done: int, total: int) -> None:
            if done == total or time.time() - shown[0] > 20:
                shown[0] = time.time()
                print(f"  {done}/{total}  {time.time() - started:.0f} 秒", flush=True)

        report = vector.build(default_base().context, args.model, progress=progress, device=args.device or "auto",
                              activate=not args.no_activate)
        print(f"向量索引完成（{time.time() - started:.0f} 秒）：{report}")
        return 0
    if args.action == "mine-outs":
        from . import mining

        paths = mining.find_out_files(args.paths)
        print(f"找到 {len(paths)} 个 .out 文件，正在解析……", flush=True)
        kinds, stats = mining.scan(paths, progress=lambda i, p: print(f"  {i}/{len(paths)}", flush=True)
                                   if i and i % 100 == 0 else None)
        print(f"解析 {stats['files']} 个（跳过 {stats['skipped']}），结束状态 {stats['endings']}，消息类别 {len(kinds)} 种")
        report = mining.seed(store, kinds, dry_run=args.dry_run,
                             message=args.message or f"knowledge: 从 {stats['files']} 个历史日志生成草稿")
        for key, value in report.items():
            print(f"{key}: {value}")
        return 0
    if args.action == "init":
        store.ensure()
        print(f"经验手册：{store.root}")
        return 0
    if args.action == "status":
        entries, problems = store.load()
        print(f"经验手册：{store.root}")
        for key, label in STATUSES.items():
            print(f"  {label}：{sum(1 for e in entries if e.status == key)} 条")
        for problem in problems:
            print(f"  无法读取：{problem}")
        for path in store.uncommitted():
            print(f"  未提交：{path}")
        return 0
    problems = store.validate()
    for problem in problems:
        print(f"格式问题：{problem}")
    if args.action == "validate":
        print("格式检查通过" if not problems else f"共 {len(problems)} 处问题")
        return 1 if problems else 0
    if problems:
        print("先修正以上问题再提交")
        return 1
    if not args.message:
        print("请用 -m 写明这次修改的内容")
        return 2
    changed = [store.root / path.strip('"') for path in store.uncommitted()]
    if not changed:
        print("没有需要提交的修改")
        return 0
    result = store.commit(changed, f"knowledge: {args.message}\n\n手工编辑")
    print("已提交 " + result["commit"] if result.get("committed") else f"未提交：{result.get('reason')}")
    return 0 if result.get("committed") else 1
