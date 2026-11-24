#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Hadoop Streaming Mapper —— 作业二：每个 URL 计一次请求。

输入（stdin，每行一条 nginx access log）
--------------------------------------
10.12.3.44 - - [24/Nov/2026:09:12:33 +0800] "GET /api/v1/orders HTTP/1.1" 200 532 "-" "curl/8.4.0"

输出（stdout，每行一条，字段用 TAB 分隔）
----------------------------------------
/api/v1/orders<TAB>1

    key   = "/api/v1/orders"   ← 请求路径（不含域名和 query string，见下方说明）
    value = "1"

设计说明
--------
  * 只取 path、丢掉 `?a=1&b=2` 这类 query string：真实场景里 query 的取值几乎无限，
    带上它会让「热门 URL」变成一堆只出现一次的键，统计失去意义。想按完整 URL 统计
    就把 PARSE_FULL_URL 打开。
  * 不做任何过滤（不看状态码）：404 的路径同样是「被请求过的热门路径」，
    如果只想统计成功请求，在 mapper 里加 `if status != 200: return None` 即可。
"""

from __future__ import annotations

import sys

# 复用作业一的解析逻辑：同一份日志格式，不要写两遍正则
from mapper_pv_uv import LOG_RE

# True = 保留 query string，按完整 URL 统计
PARSE_FULL_URL = False


def _utf8_stdio() -> None:
    """stdin 按 UTF-8 读（坏字节替换掉，别让脏数据搞崩作业），stdout/stderr 统一 UTF-8 输出。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def parse_line(line: str) -> str | None:
    """从一行日志里取出 URL；非法行返回 None。"""
    match = LOG_RE.match(line)
    if match is None:
        return None
    path = match.group("path")
    if not PARSE_FULL_URL:
        path = path.split("?", 1)[0]
    return path or None


def main() -> int:
    _utf8_stdio()

    total = 0
    emitted = 0
    skipped = 0
    write = sys.stdout.write

    for raw in sys.stdin:
        total += 1
        line = raw.lstrip("\ufeff").rstrip("\r\n")
        if not line.strip():
            skipped += 1
            continue

        url = parse_line(line)
        if url is None:
            skipped += 1
            continue

        write("%s\t1\n" % url)
        emitted += 1

    print("[mapper_top_url] read=%d emitted=%d skipped=%d" % (total, emitted, skipped),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
