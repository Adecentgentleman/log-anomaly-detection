#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""在本地模拟整套 Hadoop Streaming 流水线（不需要 Hadoop 集群）。

集群上由框架替我们做的事，这里用 subprocess 手动串起来：

    cat data/sample_access.log
      | python src/mapper_pv_uv.py          # map
      | sort -k1,1                          # shuffle：按 key 排序
      | python src/reducer_pv_uv.py         # reduce
      > results/pv_hourly.csv

两个作业（PV/UV、Top URL）依次跑，结果写成带表头的 CSV。

为什么不用系统的 sort 命令
--------------------------
Windows 上没有 coreutils 的 sort；PowerShell 的 Sort-Object 按区域设置和大小写规则排序，
和 Hadoop 的按字节排序并不等价（中文、大小写混排时结果会不一样）。所以这里直接在 Python
里对中间结果排序（sorted 是稳定排序，key 相同的记录保持原顺序），
Windows / macOS / Linux 上跑出来的结果完全一致。

为什么这不是「假跑」
--------------------
mapper / reducer 就是提交到集群的那两个文件，一行没改：它们只认 stdin -> stdout。
本地和集群的差别只有「谁来做 shuffle 排序」：集群是 Hadoop，本地是这个脚本里的
sorted()。所以本地跑通 = 集群逻辑跑通。

用法
----
    python run_local.py
退出码 0 表示两个作业都成功、且自检通过。
"""

from __future__ import annotations

import csv
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable or "python"      # 用当前解释器，避免 PATH 里抓到另一个 Python
SRC = ROOT / "src"
LOG_PATH = ROOT / "data" / "sample_access.log"
RESULTS = ROOT / "results"

LINE = "=" * 72


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def ms(seconds: float) -> str:
    return "%.1f ms" % (seconds * 1000)


def run_stage(cmd: list[str], payload: str, stage: str) -> tuple[str, str, float]:
    """把 payload 从 stdin 喂给一个 mapper/reducer，收集 stdout / stderr。"""
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",      # 万一混进非法字节，宁可替换也不要整条流水线崩掉
    )
    elapsed = time.perf_counter() - t0
    if proc.returncode != 0:
        print("\n[FAIL] %s 退出码 %d" % (stage, proc.returncode))
        print(proc.stderr)
        sys.exit(1)
    return proc.stdout, proc.stderr.strip(), elapsed


def shuffle_sort(records: list[str]) -> list[str]:
    """模拟 Hadoop shuffle：按每行第一个 TAB 之前的 key 排序（稳定排序）。"""
    return sorted(records, key=lambda line: line.split("\t", 1)[0])


def write_csv(path: Path, header: list[str], rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def preview(header: list[str], rows: list[tuple], n: int = 5) -> list[str]:
    lines = [",".join(header)]
    lines += [",".join(str(v) for v in row) for row in rows[:n]]
    return lines


def check(ok: bool, text: str) -> bool:
    print("  [%s] %s" % ("OK" if ok else "FAIL", text))
    return ok


def wpad(text: str, width: int) -> str:
    """按终端显示宽度补空格（中文占 2 列），让控制台的列能对齐。"""
    shown = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                for ch in text)
    return text + " " * max(0, width - shown)


def step(idx: int, total: int, label: str, detail: str, elapsed: float,
         note: str = "") -> None:
    """打印一行流水线日志： [2/5] map   2000 条中间记录   58.4 ms | mapper 的 stderr"""
    line = "  [%d/%d] %s %s %9s" % (idx, total, wpad(label, 13),
                                   wpad(detail, 32), ms(elapsed))
    if note:
        line += "   | " + note
    print(line.rstrip())


def parse_gauge(stderr: str, name: str) -> int | None:
    """从 mapper/reducer 的 stderr 里取出 emitted=123 这类计数。"""
    for token in stderr.replace(",", " ").split():
        if token.startswith(name + "="):
            try:
                return int(token.split("=", 1)[1])
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------- #
# 作业一：每小时 PV / UV
# --------------------------------------------------------------------------- #
def job_pv_uv(log_text: str) -> tuple[list[tuple], dict]:
    print("\n%s\n作业 1/2   每小时 PV / UV\n%s" % (LINE, LINE))

    t0 = time.perf_counter()
    log_lines = log_text.splitlines()
    t_read = time.perf_counter() - t0
    step(1, 5, "读日志", "%d 行" % len(log_lines), t_read)

    map_out, map_err, t_map = run_stage(
        [PYTHON, str(SRC / "mapper_pv_uv.py")], log_text, "mapper_pv_uv")
    mapped = [ln for ln in map_out.splitlines() if ln]
    step(2, 5, "map", "%d 条中间记录" % len(mapped), t_map, map_err)

    t0 = time.perf_counter()
    shuffled = shuffle_sort(mapped)
    t_sort = time.perf_counter() - t0
    groups = len({ln.split("\t", 1)[0] for ln in shuffled})
    step(3, 5, "shuffle/sort", "%d 条 -> %d 个 key" % (len(shuffled), groups), t_sort)

    reduce_in = "\n".join(shuffled) + ("\n" if shuffled else "")
    red_out, red_err, t_red = run_stage(
        [PYTHON, str(SRC / "reducer_pv_uv.py")], reduce_in, "reducer_pv_uv")
    step(4, 5, "reduce", "%d 行" % len(red_out.splitlines()), t_red, red_err)

    rows: list[tuple] = []
    for line in red_out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            print("[FAIL] reducer_pv_uv 输出格式不对: %r" % line)
            sys.exit(1)
        rows.append((parts[0], int(parts[1]), int(parts[2])))

    out_path = RESULTS / "pv_hourly.csv"
    t0 = time.perf_counter()
    write_csv(out_path, ["hour", "pv", "uv"], rows)
    t_write = time.perf_counter() - t0
    step(5, 5, "写 CSV", str(out_path.relative_to(ROOT)), t_write)

    print("\n  结果前 5 行:")
    for line in preview(["hour", "pv", "uv"], rows):
        print("    " + line)

    # ---- 自检：PV 合计必须等于 mapper 的有效行数，且每行 UV <= PV ----
    total_pv = sum(r[1] for r in rows)
    emitted = parse_gauge(map_err, "emitted")
    ok = True
    ok &= check(emitted is not None and total_pv == emitted,
                "PV 合计 %d == mapper 有效行数 %s" % (total_pv, emitted))
    ok &= check(all(r[2] <= r[1] and r[2] >= 1 for r in rows),
                "每一行都满足 1 <= UV <= PV")
    ok &= check(len(rows) == groups, "输出 %d 行 == shuffle 后的 %d 个 key"
                % (len(rows), groups))
    if not ok:
        print("\n[FAIL] 作业一自检未通过")
        sys.exit(1)

    return rows, {"total_pv": total_pv, "elapsed": t_map + t_sort + t_red}


# --------------------------------------------------------------------------- #
# 作业二：Top 10 热门 URL
# --------------------------------------------------------------------------- #
def job_top_url(log_text: str) -> tuple[list[tuple], dict]:
    print("\n%s\n作业 2/2   Top 10 热门 URL\n%s" % (LINE, LINE))

    map_out, map_err, t_map = run_stage(
        [PYTHON, str(SRC / "mapper_top_url.py")], log_text, "mapper_top_url")
    mapped = [ln for ln in map_out.splitlines() if ln]
    step(1, 4, "map", "%d 条中间记录" % len(mapped), t_map, map_err)

    t0 = time.perf_counter()
    shuffled = shuffle_sort(mapped)
    t_sort = time.perf_counter() - t0
    step(2, 4, "shuffle/sort", "%d 条" % len(shuffled), t_sort)

    reduce_in = "\n".join(shuffled) + ("\n" if shuffled else "")
    red_out, red_err, t_red = run_stage(
        [PYTHON, str(SRC / "reducer_top_url.py")], reduce_in, "reducer_top_url")
    rows: list[tuple] = []
    for line in red_out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            print("[FAIL] reducer_top_url 输出格式不对: %r" % line)
            sys.exit(1)
        rows.append((parts[0], int(parts[1])))
    step(3, 4, "reduce", "%d 行" % len(rows), t_red, red_err)

    out_path = RESULTS / "top_url.csv"
    t0 = time.perf_counter()
    write_csv(out_path, ["url", "count"], rows)
    t_write = time.perf_counter() - t0
    step(4, 4, "写 CSV", str(out_path.relative_to(ROOT)), t_write)

    print("\n  结果前 5 行:")
    for line in preview(["url", "count"], rows):
        print("    " + line)

    total_requests = parse_gauge(map_err, "emitted") or 0
    ok = True
    ok &= check(len(rows) == 10, "输出正好 10 行（Top 10）")
    ok &= check(all(rows[i][1] >= rows[i + 1][1] for i in range(len(rows) - 1)),
                "count 从大到小排列")
    ok &= check(sum(r[1] for r in rows) <= total_requests,
                "Top 10 命中的请求数 %d <= 总请求数 %d"
                % (sum(r[1] for r in rows), total_requests))
    if not ok:
        print("\n[FAIL] 作业二自检未通过")
        sys.exit(1)

    return rows, {"elapsed": t_map + t_sort + t_red,
                  "top_share": sum(r[1] for r in rows) / max(total_requests, 1)}


def main() -> int:
    _utf8_stdio()

    started = time.perf_counter()
    print(LINE)
    print("Hadoop Streaming 本地模拟（不需要集群，subprocess 串管道）")
    print(LINE)
    print("  python      %s" % PYTHON)
    print("  输入日志    %s" % LOG_PATH.relative_to(ROOT))

    if not LOG_PATH.exists():
        print("\n[FAIL] 找不到 %s，先跑 python src/gen_fake_logs.py" % LOG_PATH)
        return 1
    log_text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    print("  日志行数    %d" % len(log_text.splitlines()))
    print("  计时口径    每个 mapper/reducer 都是一次独立进程，耗时含解释器启动"
          "（Windows 上受杀毒软件影响波动较大）")

    pv_rows, pv_meta = job_pv_uv(log_text)
    top_rows, top_meta = job_top_url(log_text)

    print("\n" + LINE)
    print("汇总")
    print(LINE)
    summary = [
        ("PV 合计", "%d（= 合法日志行数）" % pv_meta["total_pv"]),
        ("有数据的小时数", "%d" % len(pv_rows)),
        ("Top1 URL", "%s（%d 次，占全部请求 %.1f%%）"
         % (top_rows[0][0], top_rows[0][1], top_meta["top_share"] * 100)),
        ("结果文件", "results/pv_hourly.csv, results/top_url.csv"),
        ("总耗时", ms(time.perf_counter() - started)),
    ]
    for key, value in summary:
        print("  %s %s" % (wpad(key, 18), value))
    print("  全部自检通过，退出码 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
