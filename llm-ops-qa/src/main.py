# -*- coding: utf-8 -*-
"""智能运维问答助手 —— 命令行入口。

用法::

    python src/main.py --query "nginx 502 怎么排查"
    python src/main.py --eval
    python src/main.py --interactive

不带任何参数时打印帮助。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

# Windows 控制台默认可能是 GBK，输出中文/符号会抛 UnicodeEncodeError。
# reconfigure 在部分环境下不可用（被重定向的管道、老版本 Python），所以用 try 包住。
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001 - 拿不到就按默认编码走，不要让程序起不来
    pass

# 支持 `python src/main.py` 直接运行（此时 src/ 已在 sys.path[0]）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from generator import generate  # noqa: E402
from retriever import Retriever  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parent.parent
KB_PATH = ROOT_DIR / "data" / "kb.jsonl"
EVAL_PATH = ROOT_DIR / "data" / "eval_questions.jsonl"
REPORT_PATH = ROOT_DIR / "reports" / "eval_report.md"

SEP = "=" * 68
SUB_SEP = "-" * 68


# --------------------------------------------------------------------- 输出工具
def _display_width(text: str) -> int:
    """终端里中日韩字符占两列，按显示宽度算才能让表格对齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = " " * max(0, width - _display_width(text))
    return text + gap if align == "left" else gap + text


def print_hits(hits: list[tuple[dict[str, Any], float]]) -> None:
    """打印检索结果表格（相似度保留 3 位）。"""
    if not hits:
        print("检索结果：无（查询里没有能匹配知识库的词）")
        return
    print("检索结果：")
    print(
        f"  {_pad('#', 3)}{_pad('文档ID', 9)}{_pad('相似度', 8, 'right')}  标题"
    )
    for rank, (doc, score) in enumerate(hits, start=1):
        print(
            f"  {_pad(str(rank), 3)}"
            f"{_pad(str(doc.get('id', '?')), 9)}"
            f"{_pad(f'{score:.3f}', 8, 'right')}  "
            f"{doc.get('title', '')}"
        )


def print_answer(query: str, hits: list[tuple[dict[str, Any], float]]) -> None:
    print(SEP)
    print(f"问题：{query}")
    print(SUB_SEP)
    print_hits(hits)
    print(SUB_SEP)
    print(generate(query, hits))
    print(SEP)


# --------------------------------------------------------------------- 单次问答
def run_query(retriever: Retriever, query: str, top_k: int) -> int:
    hits = retriever.search(query, top_k=top_k)
    print_answer(query, hits)
    return 0


# --------------------------------------------------------------------- 批量评测
def _load_eval_questions(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path} 第 {lineno} 行不是合法 JSON：{exc}") from exc
    return items


def _md_cell(text: str) -> str:
    """Markdown 表格单元格转义：竖线和换行会把表格撑坏。"""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _write_report(
    rows: list[dict[str, Any]],
    hit1: int,
    hit3: int,
    n_kb: int,
    n_eval: int,
) -> Path:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append("# 检索效果评测报告（Hit@1 / Hit@3）")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 知识库：`data/kb.jsonl`，共 {n_kb} 条")
    lines.append(f"- 评测集：`data/eval_questions.jsonl`，共 {n_eval} 条")
    lines.append("- 检索器：字符级 bigram + 英文数字分词，TF-IDF + 余弦相似度")
    lines.append("- 判定口径：`expected_id` 出现在 top1 记 Hit@1，出现在 top3 记 Hit@3")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 命中 | 总数 | 命中率 |")
    lines.append("| --- | ---: | ---: | ---: |")
    lines.append(f"| Hit@1 | {hit1} | {n_eval} | {hit1 / n_eval:.1%} |")
    lines.append(f"| Hit@3 | {hit3} | {n_eval} | {hit3 / n_eval:.1%} |")
    lines.append("")
    lines.append("## 逐条结果")
    lines.append("")
    lines.append(
        "| # | 问题 | 期望文档 | Top1 文档 | Top1 标题 | Top1 分数 | Hit@1 | Hit@3 |"
    )
    lines.append("| ---: | --- | --- | --- | --- | ---: | :---: | :---: |")
    for row in rows:
        lines.append(
            f"| {row['index']} | {_md_cell(row['question'])} | {row['expected']} "
            f"| {row['top1_id']} | {_md_cell(row['top1_title'])} "
            f"| {row['top1_score']:.3f} "
            f"| {'PASS' if row['hit1'] else 'MISS'} "
            f"| {'PASS' if row['hit3'] else 'MISS'} |"
        )
    lines.append("")

    lines.append("## Top3 明细")
    lines.append("")
    lines.append("| # | 期望文档 | Top3（id / 分数） |")
    lines.append("| ---: | --- | --- |")
    for row in rows:
        top3_text = "；".join(f"{tid} / {score:.3f}" for tid, score in row["top3"])
        lines.append(f"| {row['index']} | {row['expected']} | {_md_cell(top3_text)} |")
    lines.append("")

    misses = [row for row in rows if not row["hit1"]]
    lines.append("## 未命中 Top1 的用例")
    lines.append("")
    if not misses:
        lines.append(f"无，{n_eval} 条全部命中 Top1。")
    else:
        for row in misses:
            lines.append(
                f"- `{row['index']}` 「{row['question']}」期望 `{row['expected']}`，"
                f"Top1 是 `{row['top1_id']}`（{row['top1_title']}，{row['top1_score']:.3f}）"
            )
    lines.append("")

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return REPORT_PATH


def run_eval(retriever: Retriever, top_k: int = 3) -> int:
    if not EVAL_PATH.exists():
        print(f"[error] 找不到评测文件：{EVAL_PATH}", file=sys.stderr)
        return 2
    questions = _load_eval_questions(EVAL_PATH)
    if not questions:
        print(f"[error] 评测文件为空：{EVAL_PATH}", file=sys.stderr)
        return 2

    print(SEP)
    print(f"评测：{len(questions)} 条问题，知识库 {len(retriever)} 条文档")
    print(SUB_SEP)

    rows: list[dict[str, Any]] = []
    hit1 = hit3 = 0
    for index, item in enumerate(questions, start=1):
        question = str(item.get("question", ""))
        expected = str(item.get("expected_id", ""))
        hits = retriever.search(question, top_k=top_k)
        ids = [doc.get("id") for doc, _ in hits]

        is_hit1 = bool(ids) and ids[0] == expected
        is_hit3 = expected in ids[:3]
        hit1 += int(is_hit1)
        hit3 += int(is_hit3)

        top1_doc, top1_score = (hits[0] if hits else ({}, 0.0))
        rows.append(
            {
                "index": index,
                "question": question,
                "expected": expected,
                "top1_id": top1_doc.get("id", "-"),
                "top1_title": top1_doc.get("title", "-"),
                "top1_score": float(top1_score),
                "hit1": is_hit1,
                "hit3": is_hit3,
                "top3": [
                    (doc.get("id", "-"), float(score)) for doc, score in hits[:3]
                ],
            }
        )

        print(f"[{index:>2}/{len(questions)}] {question}")
        print(
            f"      expected={expected}  top1={rows[-1]['top1_id']}"
            f"（{rows[-1]['top1_score']:.3f}）"
            f"  Hit@1={'命中' if is_hit1 else '未命中'}"
            f"  Hit@3={'命中' if is_hit3 else '未命中'}"
        )

    n = len(questions)
    report = _write_report(rows, hit1, hit3, len(retriever), n)

    print(SUB_SEP)
    print(f"Hit@1: {hit1}/{n} = {hit1 / n:.1%}")
    print(f"Hit@3: {hit3}/{n} = {hit3 / n:.1%}")
    print(f"报告已写入: {report.relative_to(ROOT_DIR)}")
    print(SEP)
    return 0


# --------------------------------------------------------------------- 交互模式
def run_interactive(retriever: Retriever, top_k: int) -> int:
    print(SEP)
    print("交互模式：输入运维问题回车提问，输入 exit 退出。")
    print(SUB_SEP)
    while True:
        try:
            query = input("\n你的问题> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            return 0

        if not query:
            continue
        if query.lower() in {"exit", "quit", "q"} or query == "退出":
            print("再见。")
            return 0

        hits = retriever.search(query, top_k=top_k)
        print(SUB_SEP)
        print_hits(hits)
        print(SUB_SEP)
        print(generate(query, hits))


# --------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="智能运维问答助手：检索运维知识库并生成回答（默认离线模式，不需要 API key）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  python src/main.py --query "nginx 502 怎么排查"\n'
            "  python src/main.py --eval\n"
            "  python src/main.py --interactive\n"
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-q", "--query", metavar="TEXT", help="单次问答，传入问题文本")
    group.add_argument("--eval", action="store_true", help="跑评测集，算 Hit@1 / Hit@3 并写报告")
    group.add_argument("-i", "--interactive", action="store_true", help="交互式问答，输入 exit 退出")
    parser.add_argument(
        "--top-k",
        type=int,
        default=3,
        metavar="K",
        help="检索返回条数，默认 3",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not (args.query or args.eval or args.interactive):
        parser.print_help()
        print()
        print('提示：先试一下 python src/main.py --query "nginx 502 怎么排查"')
        return 0

    if args.top_k < 1:
        print("[error] --top-k 必须 >= 1", file=sys.stderr)
        return 2

    if not KB_PATH.exists():
        print(f"[error] 找不到知识库：{KB_PATH}", file=sys.stderr)
        return 2

    try:
        retriever = Retriever(KB_PATH)
    except (OSError, ValueError) as exc:
        print(f"[error] 知识库加载失败：{exc}", file=sys.stderr)
        return 2

    if args.eval:
        return run_eval(retriever, top_k=max(args.top_k, 3))
    if args.interactive:
        return run_interactive(retriever, top_k=args.top_k)
    return run_query(retriever, args.query, top_k=args.top_k)


if __name__ == "__main__":
    raise SystemExit(main())
