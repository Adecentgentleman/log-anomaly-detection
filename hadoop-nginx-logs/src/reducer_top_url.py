#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Hadoop Streaming Reducer —— 作业二：求 Top 10 热门 URL。

输入（stdin，已经由 Streaming 按 URL 排好序）
-------------------------------------------
/api/v1/orders<TAB>1
/api/v1/orders<TAB>1
/product/list<TAB>1
...

输出（stdout，count 从大到小，同 count 按 URL 字典序，保证结果稳定可复现）
----------------------------------------------------------------------
/product/list<TAB>233
/api/v1/orders<TAB>219
...

实现取舍（重要，面试会问）
--------------------------
Top-N 是**全局聚合**，天生和「流式 + 分片」冲突：任何单个 reducer 都看不到全部数据，
而要看全部就只能把数据收拢到一处。这里选了最简单的方案：

    **单 reducer + 全量内存累加 -> heapq.nlargest 取 Top 10**

  * 优点：一个作业搞定，map 端只做 1 计数，逻辑最少；
  * 代价：所有 URL 的计数表都堆在一个 reducer 的堆内存里。2000 行 / 74 个 URL 毫无压力，
    但真实集群上如果 URL 有上亿个（长尾），单 reducer 会 OOM，而且所有数据挤到一个
    reduce task 上，并行度直接退化成 1，Shuffle 也会成为瓶颈。

真实大数据量下应该怎么做（按代价从小到大）：

  1. **两阶段作业**：作业 A 先按 URL 全量聚合成 `url<TAB>count`（可按 hash 分区并行），
     作业 B 再对一个已经小得多的结果集求 Top-N；
  2. **每个 reducer 先算局部 Top-N，再起一个作业合并**（经典 pattern，
     map-side 也可以先做 combiner 预聚合，Shuffle 数据量能降一到两个数量级）；
  3. 用 `TotalOrderPartitioner` + 采样做全局排序，然后直接取头 N 条；
  4. 换成 Spark / Flink 的 `topK` 或 `sortBy().limit()`，由框架做局部预聚合。

TOP_N 可以用环境变量覆盖（Hadoop Streaming 的 -cmdenv TOP_N=20），默认 10。
"""

from __future__ import annotations

import heapq
import os
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

    try:
        top_n = max(1, int(os.environ.get("TOP_N", "10")))
    except ValueError:
        top_n = 10

    counts: dict[str, int] = {}
    total = 0          # 收到的记录条数（= 有效请求数）
    bad_records = 0

    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        if not line:
            continue

        # rsplit：URL 理论上不含 TAB，但按最后一个 TAB 切更保险
        url, sep, num = line.rpartition("\t")
        if not sep:
            bad_records += 1
            continue
        try:
            count = int(num)
        except ValueError:
            bad_records += 1
            continue

        counts[url] = counts.get(url, 0) + count
        total += count

    # 用堆取 Top-N：O(n log k)，比全量排序 O(n log n) 省一点。
    # 并列时先按 count 降序、再按 URL 升序，保证每次跑出来的顺序都一样。
    top = heapq.nlargest(top_n, counts.items(), key=lambda kv: (kv[1], kv[0]))
    top.sort(key=lambda kv: (-kv[1], kv[0]))

    for url, count in top:
        sys.stdout.write("%s\t%d\n" % (url, count))

    print("[reducer_top_url] distinct_urls=%d total_requests=%d emitted=%d bad_records=%d"
          % (len(counts), total, len(top), bad_records), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
