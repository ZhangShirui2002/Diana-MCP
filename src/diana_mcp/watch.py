"""Follow mode: report the user's DianaIE edits as they happen.

    python -m diana_mcp watch            wait for the next batch of edits, print it, exit (Bash in the background)
    python -m diana_mcp watch --follow   keep printing one report per batch (Claude Code's Monitor tool)

A batch is reported once the user has paused for --quiet seconds, so one report covers one piece of work instead of
every click. Each report lists the operations (with what an undo reverted) and, when a bridge is connected, the net
model changes with old -> new values. It advances the same cursor and baseline as the diana_changes tool.
"""
from __future__ import annotations

import argparse
import sys
import time

from . import changes as ch
from .session import SessionManager
from .tracking import Tracker, TrackingError, format_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m diana_mcp watch", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--follow", action="store_true", help="keep reporting until DianaIE closes")
    parser.add_argument("--quiet", type=float, default=4.0, help="seconds without new commands before reporting")
    parser.add_argument("--timeout", type=float, default=1800.0,
                        help="give up after this many seconds without edits (per report in --follow mode)")
    parser.add_argument("--include-view", action="store_true", help="also report view and result-plot operations")
    parser.add_argument("--no-diff", action="store_true", help="do not snapshot the model (operations only)")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    categories = ch.ALL_CATEGORIES if args.include_view else ch.DEFAULT_CATEGORIES
    tracker = Tracker(SessionManager())
    try:
        log, _ = tracker.current_log(tracker.live_session())
    except TrackingError as exc:
        print(f"[DianaIE 跟随] 无法开始：{exc}", flush=True)
        return 1
    print(f"[DianaIE 跟随] 开始监视 {log.path.name}（停手 {args.quiet:g} 秒后汇报）", flush=True)
    while True:
        try:
            changed = tracker.wait_for_changes(args.timeout, quiet=args.quiet, categories=categories)
        except TrackingError as exc:
            print(f"[DianaIE 跟随] 停止：{exc}", flush=True)
            return 1
        except KeyboardInterrupt:
            return 0
        if not changed and not _still_running(log):
            print("[DianaIE 跟随] DianaIE 已关闭，停止跟随", flush=True)
            return 0
        if not changed:
            if not args.follow:
                print(f"[DianaIE 跟随] {args.timeout:g} 秒内没有新的建模操作", flush=True)
                return 0
            continue
        report = tracker.changes(advance=True, categories=categories, with_diff=not args.no_diff)
        print(format_report(report), flush=True)
        if not args.follow:
            return 0
        time.sleep(0.2)


def _still_running(log: ch.HistoryFile) -> bool:
    """DianaIE renames its history file when it exits; a killed DianaIE leaves it but its process is gone."""
    if not log.path.exists():
        return False
    return any(h.key == log.key for h in ch.logs_of_running_dianaie().values())


if __name__ == "__main__":
    sys.exit(main())
