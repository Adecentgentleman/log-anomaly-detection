#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Hadoop Streaming Mapper —— 作业一：每小时 PV / UV。

输入（stdin，每行一条 nginx access log）
--------------------------------------
10.12.3.44 - - [24/Nov/2026:09:12:33 +0800] "GET /api/v1/orders HTTP/1.1" 200 532 "-" "curl/8.4.0"

输出（stdout，每行一条，字段用 TAB 分隔）
----------------------------------------
2026-11-24 09<TAB>1<TAB>10.12.3.44

Hadoop Streaming 以**每行第一个 TAB** 切分 key / value，所以：

    key   = "2026-11-24 09"     ← 小时，聚合的维度
    value = "1\t10.12.3.44"     ← 先 1（计一次 PV），再跟客户端 IP（供 UV 去重）

容错约定
--------
非法行统一**静默跳过，不抛异常**——一行脏数据不能把整个作业搞挂
（真实日志里混着半行、被截断的行、爬虫乱码都是常态）。
跳过的条数只写到 stderr：stderr 不会进 reducer，不会污染结果，
在集群上会出现在 task 的 syslog / userlogs 里，方便排查。
"""

from __future__ import annotations

import re
import sys

# 12 个月份缩写 -> 月份数字。nginx 的 $time_local 是英文缩写，不能直接给 datetime 解析
# （%b 依赖 locale，容器里常常是 C locale，这里自己查表最稳）。
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# 只匹配需要的字段：IP、时间戳、请求行、状态码。
# 后面的 referer / user-agent 可能含引号和空格，直接用 match 忽略尾部更稳。
LOG_RE = re.compile(
    r"^(?P<ip>\d{1,3}(?:\.\d{1,3}){3})"                     # 客户端 IP
    r"\s+\S+\s+\S+\s+"                                       # ident + authuser（一般是 - -）
    r"\[(?P<day>\d{2})/(?P<mon>[A-Za-z]{3})/(?P<year>\d{4}):"  # [24/Nov/2026:
    r"(?P<hh>\d{2}):(?P<mi>\d{2}):(?P<ss>\d{2})\s+(?P<tz>[+-]\d{4})\]"  # 09:12:33 +0800]
    r'\s+"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/\d\.\d"'  # "GET /api/v1/orders HTTP/1.1"
    r"\s+(?P<status>\d{3})\s+(?:\d+|-)",                     # 200 532
)


def _utf8_stdio() -> None:
    """固定三个标准流的编码，避免不同平台/locale 下行为不一致。

    * stdin：按 UTF-8 读，坏字节用替换字符兜住——一行编码错乱的日志不该让作业挂掉
      （Windows 默认按 GBK 解码管道，日志里出现 UTF-8 中文 UA 时会直接抛异常）；
    * stdout / stderr：统一 UTF-8 输出，Streaming 是透传字节，不做任何转换。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):   # 老版本 Python / 已被重定向的流
            pass


def _valid_ip(ip: str) -> bool:
    parts = ip.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def parse_line(line: str) -> tuple[str, str] | None:
    """解析一行日志。

    返回 ("2026-11-24 09", "10.12.3.44")；非法行返回 None（调用方负责计数）。
    """
    match = LOG_RE.match(line)
    if match is None:
        return None

    if not _valid_ip(match.group("ip")):
        return None

    month = MONTHS.get(match.group("mon").title())
    if month is None:
        return None

    day = int(match.group("day"))
    hour = int(match.group("hh"))
    minute = int(match.group("mi"))
    second = int(match.group("ss"))
    status = int(match.group("status"))
    if not (1 <= day <= 31 and 0 <= hour <= 23 and 0 <= minute <= 59
            and 0 <= second <= 59 and 100 <= status <= 599):
        return None

    key = "%s-%02d-%02d %02d" % (match.group("year"), month, day, hour)
    return key, match.group("ip")


def main() -> int:
    _utf8_stdio()

    total = 0
    emitted = 0
    skipped = 0
    write = sys.stdout.write

    for raw in sys.stdin:
        total += 1
        line = raw.lstrip("\ufeff").rstrip("\r\n")   # 兼容 BOM 和 CRLF
        if not line.strip():
            skipped += 1
            continue

        parsed = parse_line(line)
        if parsed is None:
            skipped += 1
            continue

        key, ip = parsed
        # 直接写，不攒大列表：日志几百 GB 时也能保持恒定内存
        write("%s\t1\t%s\n" % (key, ip))
        emitted += 1

    print("[mapper_pv_uv] read=%d emitted=%d skipped=%d" % (total, emitted, skipped),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
