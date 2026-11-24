#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Hadoop Streaming Reducer —— 作业一：按小时汇总 PV / UV。

输入（stdin，**已经由 Streaming 的 shuffle 按 key 排好序**）
-----------------------------------------------------------
2026-11-24 09<TAB>1<TAB>10.12.3.44
2026-11-24 09<TAB>1<TAB>10.12.3.91
2026-11-24 09<TAB>1<TAB>10.12.3.44
2026-11-24 10<TAB>1<TAB>10.12.3.7
...

输出（stdout，每行一条，字段用 TAB 分隔）
----------------------------------------
2026-11-24 09<TAB>137<TAB>29
即 hour<TAB>PV<TAB>UV：

  * PV = 该小时出现的日志条数（每个 value 里的那个 1 累加）；
  * UV = 该小时去重后的客户端 IP 个数（用 IP 近似独立访客，见 README 的「局限」）。

为什么可以边读边算
------------------
Streaming 保证**同一个 key 的所有记录连续到达**（这正是 shuffle 排序的意义），
所以只需要在内存里保留「当前小时」的 IP 集合：读完一个 key 就输出一行、
清空集合。哪怕一天有几十亿条日志，reducer 的峰值内存也只有「一个小时的去重 IP 数」。
如果框架不排序（比如自己 cat ... | reducer），就得先把整批数据读进来再 group by。
"""

from __future__ import annotations

import sys


def _utf8_stdio() -> None:
    """stdin 按 UTF-8 读（坏字节替换掉，别让脏数据搞崩作业），stdout/stderr 统一 UTF-8 输出。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> int:
    _utf8_stdio()

    write = sys.stdout.write
    current_key: str | None = None
    pv = 0
    ips: set[str] = set()
    groups = 0            # 输出了多少个小时
    bad_records = 0       # 格式不对的中间记录（正常情况下应为 0）
    total_pv = 0

    def flush() -> None:
        """输出当前 key 的聚合结果，并重置累加器。"""
        nonlocal groups, total_pv
        if current_key is None:
            return
        write("%s\t%d\t%d\n" % (current_key, pv, len(ips)))
        groups += 1
        total_pv += pv

    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        if not line:
            continue

        parts = line.split("\t")
        if len(parts) < 2:
            bad_records += 1
            continue

        key = parts[0]
        try:
            count = int(parts[1])
        except ValueError:
            bad_records += 1
            continue

        # 换了 key：先把上一个小结输出掉
        if key != current_key:
            flush()
            current_key = key
            pv = 0
            ips = set()

        pv += count
        # parts[2] 才是 IP；万一只给了 "1" 没有 IP，就只算 PV 不算 UV
        if len(parts) >= 3 and parts[2]:
            ips.add(parts[2])

    flush()
    print("[reducer_pv_uv] hours=%d total_pv=%d bad_records=%d"
          % (groups, total_pv, bad_records), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
