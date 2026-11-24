"""入口：执行巡检、打印终端表格、生成报表与 dry-run 告警邮件。

退出码：
  0  所有检查均为 PASS / WARN / SKIP
  1  至少存在一项 FAIL（可直接接入 CI / crontab 告警）
  2  参数或配置错误、报表写入失败
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from checks import CheckResult, run_all_checks, summarize
from mailer import build_mail
from report import render_html, write_csv

REPO_ROOT = Path(__file__).resolve().parent.parent
TABLE_LIMITS = (10, 24, 16, 24, 6, 35)


def _configure_stdout() -> None:
    """Windows 控制台尽量切到 UTF-8；不是 TextIOWrapper 时安静跳过。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in "WFA" else 1
    return width


def _truncate(text: str, limit: int) -> str:
    if _display_width(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    out: list[str] = []
    used = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in "WFA" else 1
        )
        if used + char_width > limit - 1:
            break
        out.append(char)
        used += char_width
    return "".join(out) + "…"


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _render_terminal_table(results: list[CheckResult]) -> str:
    headers = ["检查项", "对象", "实际值", "阈值", "状态", "详情"]
    fields = ["name", "target", "value", "threshold", "status", "detail"]
    rows: list[list[str]] = []
    for item in results:
        row = []
        for key, limit in zip(fields, TABLE_LIMITS):
            row.append(_truncate(str(getattr(item, key, "")), limit))
        rows.append(row)

    widths = []
    for index, header in enumerate(headers):
        content_width = max(
            [_display_width(header)] + [_display_width(row[index]) for row in rows]
        )
        widths.append(min(TABLE_LIMITS[index], content_width))

    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def line(cells: list[str]) -> str:
        return "|" + "|".join(
            f" {_pad(cell, width)} " for cell, width in zip(cells, widths)
        ) + "|"

    output = [border, line(headers), border]
    output.extend(line(row) for row in rows)
    output.append(border)
    return "\n".join(output)


def _load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("配置文件根节点必须是 JSON 对象")
    return config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Python 自动化巡检：磁盘、内存、CPU、端口、进程、目录、文件大小。",
        epilog="退出码：0=无 FAIL；1=存在 FAIL；2=参数/配置/报表错误。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", default="config.json", help="巡检配置文件路径")
    parser.add_argument("--out", default="reports", help="HTML/CSV 报表输出目录")
    parser.add_argument(
        "--no-mail", action="store_true", help="即使存在 FAIL 也不生成告警邮件"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="不打印标题和明细表格，只输出摘要与报表路径"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = _build_parser().parse_args(argv)
    started = time.perf_counter()

    config_path = Path(args.config).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()

    if not args.quiet:
        print("=" * 68)
        print("Python 自动化巡检脚本")
        print("=" * 68)
        print(f"配置文件：{config_path}")
        print(f"输出目录：{out_dir}")
        print()

    try:
        config = _load_config(config_path)
    except Exception as exc:
        print(f"[错误] 无法读取配置 {config_path}：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    check_started = time.perf_counter()
    results = run_all_checks(config)
    check_elapsed = time.perf_counter() - check_started
    summary = summarize(results)

    if not args.quiet:
        print(_render_terminal_table(results))
        print()

    timestamp = datetime.now()
    stem = f"inspect_{timestamp:%Y%m%d_%H%M%S}"
    html_path = out_dir / f"{stem}.html"
    csv_path = out_dir / f"{stem}.csv"

    try:
        html_written = render_html(
            results,
            config,
            html_path,
            elapsed=check_elapsed,
            generated_at=timestamp,
        )
        csv_written = write_csv(results, csv_path)
    except Exception as exc:
        print(f"[错误] 报表写入失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.no_mail:
        if summary["FAIL"]:
            print("告警邮件：已跳过（--no-mail）。")
    elif summary["FAIL"]:
        try:
            eml_path = build_mail(results, config, REPO_ROOT / "outbox")
            if eml_path is not None:
                print(f"告警邮件（dry-run）：{eml_path}")
        except Exception as exc:
            print(f"[警告] 告警邮件生成失败：{type(exc).__name__}: {exc}", file=sys.stderr)
    else:
        print("告警邮件：无 FAIL，无需生成。")

    elapsed = time.perf_counter() - started
    print()
    print(
        "巡检完成："
        f"总计 {summary['total']} 项 | "
        f"PASS {summary['PASS']} | WARN {summary['WARN']} | "
        f"FAIL {summary['FAIL']} | SKIP {summary['SKIP']}"
    )
    print(f"总耗时：{elapsed:.2f}s")
    print(f"HTML 报表：{html_written}")
    print(f"CSV 报表：{csv_written}")
    print(f"退出码：{1 if summary['FAIL'] else 0}")
    return 1 if summary["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
