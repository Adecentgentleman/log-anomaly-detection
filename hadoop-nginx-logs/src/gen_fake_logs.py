#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""生成合成的 nginx 访问日志（全部是假数据，可以安全地提交到公开仓库）。

为什么要有这个脚本
------------------
简历项目里不能放真实的生产日志（会泄露真实 IP、内网结构、业务域名、用户行为），
所以这里用固定随机种子合成一份「形状像真的」日志：

  * 24 小时分布，白天（09:00-18:00）流量明显更高；
  * URL 请求量服从齐夫（Zipf）分布——少数 URL 占大头，剩下是长尾；
  * 42 个客户端 IP，访问频次同样不均匀（少数 IP 是重度用户）；
  * 带少量 404 / 500 / 304，方便下游作业做状态码维度分析；
  * 日志按时间升序排列，和真实 nginx access.log 一致。

合规红线（改这个脚本时请保持）
------------------------------
  * 客户端 IP 只从 10.0.0.0/8（RFC 1918 私网）、192.0.2.0/24、198.51.100.0/24、
    203.0.113.0/24（RFC 5737 文档示例地址）里取，不对应任何真实主机；
  * 域名只用 example.com，路径全部是虚构的业务路径；
  * 固定种子 => 同一版本下每次生成的文件逐字节一致，脚本最后打印 sha256 供核对。

输出格式（nginx combined 格式的子集）
------------------------------------
10.12.3.44 - - [24/Nov/2026:09:12:33 +0800] "GET /api/v1/orders HTTP/1.1" 200 532 "-" "curl/8.4.0"
│            │ │  │                          │     │                     │   │   │    │
│            │ │  │                          │     │                     │   │   │    └─ User-Agent
│            │ │  │                          │     │                     │   │   └────── Referer
│            │ │  │                          │     │                     │   └────────── body_bytes_sent
│            │ │  │                          │     │                     └────────────── status
│            │ │  │                          │     └──────────────────────────────────── request
│            │ │  │                          └────────────────────────────────────────── 本地时间 + 时区
│            │ │  └───────────────────────────────────────────────────────────────────── authuser / ident
└────────────┴─┴──────────────────────────────────────────────────────────────────────── remote_addr

用法
----
    python src/gen_fake_logs.py                     # 2000 行 -> data/sample_access.log
    python src/gen_fake_logs.py --lines 500         # 造一份小数据
    python src/gen_fake_logs.py --seed 7 --out data/other.log
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
from collections import Counter
from pathlib import Path

# --------------------------------------------------------------------------- #
# 固定日期：2026-11-24（全部日志都在这一天，方便按小时做聚合）
# --------------------------------------------------------------------------- #
YEAR = 2026
MONTH_NAME = "Nov"
DAY = 24
TZ = "+0800"

# --------------------------------------------------------------------------- #
# 假 URL 池：按「预期热度」从高到低排序，权重用 zipf 衰减
# --------------------------------------------------------------------------- #
CORE_URLS = [
    "/api/v1/orders",                  # 下单接口，最热
    "/product/list",                   # 商品列表页
    "/api/v1/products",                # 商品接口
    "/static/app.js",                  # 前端主包
    "/",                               # 首页
    "/product/detail",                 # 商品详情页
    "/api/v1/users",                   # 用户信息
    "/static/style.css",
    "/api/v1/cart",                    # 购物车
    "/api/v1/search",
    "/api/v1/login",
    "/product/search",
    "/api/v1/pay",
    "/static/vendor.js",
    "/api/v1/recommend",
    "/index.html",
    "/user/center",
    "/api/v1/comments",
    "/static/logo.png",
    "/blog/2026/11/hadoop-streaming",
    "/api/v1/categories",
    "/blog/2026/10/nginx-log-format",
    "/api/v1/coupons",
    "/product/review",
    "/static/fonts/app.woff2",
    "/api/v1/health",
    "/about",
    "/api/v1/upload",
    "/static/icon-sprite.svg",
    "/help/faq",
]

# 命中即返回 404 的路径（扫描器 / 老接口 / 不存在的静态文件）
NOT_FOUND_URLS = [
    "/wp-login.php",
    "/admin",
    "/api/v1/legacy",
    "/favicon.ico",
]

# 这些接口用 POST，其余用 GET
POST_URLS = {
    "/api/v1/login",
    "/api/v1/orders",
    "/api/v1/pay",
    "/api/v1/cart",
    "/api/v1/upload",
    "/api/v1/comments",
}

# 每小时权重：00-05 深夜很低，09-18 白天是高峰，19-23 逐步回落
HOUR_WEIGHTS = [
    0.30, 0.18, 0.12, 0.10, 0.12, 0.25,   # 00-05
    0.70, 1.20, 2.00, 2.80, 3.40, 3.60,   # 06-11
    3.00, 3.20, 3.50, 3.60, 3.40, 3.00,   # 12-17
    2.60, 2.40, 2.20, 1.80, 1.20, 0.60,   # 18-23
]

REFERERS = [
    "-", "-", "-", "-", "https://example.com/",
    "https://example.com/product/list",
    "https://example.com/blog/2026/11/hadoop-streaming",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36",
    "curl/8.4.0",
    "python-requests/2.32.3",
    "PostmanRuntime/7.42.0",
    "Googlebot/2.1 (+http://www.example.com/bot.html)",
]


# --------------------------------------------------------------------------- #
# 假 IP 池
# --------------------------------------------------------------------------- #
def build_ip_pool(rng: random.Random, n_private: int = 26, n_doc: int = 6,
                  n_doc2: int = 5, n_doc3: int = 5) -> list[str]:
    """造 42 个假 IP：私网 10/8 + 三段 RFC 5737 文档地址，全部去重。"""
    ips: list[str] = []
    seen: set[str] = set()
    while len(ips) < n_private:
        ip = "10.%d.%d.%d" % (rng.randrange(1, 255), rng.randrange(0, 256),
                              rng.randrange(1, 255))
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    for prefix, count in (("192.0.2.", n_doc), ("198.51.100.", n_doc2),
                          ("203.0.113.", n_doc3)):
        for host in rng.sample(range(1, 255), count):
            ips.append("%s%d" % (prefix, host))
    return ips


def build_url_pool(rng: random.Random, n_tail: int = 40) -> tuple[list[str], list[float]]:
    """假 URL 池 + 齐夫权重：排名第 i 的 URL 权重 ~ 1/(i+1)，长尾再补一批低权重的详情页。"""
    urls = list(CORE_URLS) + list(NOT_FOUND_URLS)
    weights = [1.0 / (i + 1) for i in range(len(urls))]

    # 长尾：商品详情页 /product/detail/<id>，每个只来几次，永远不会进 Top 10
    for pid in rng.sample(range(1000, 9999), n_tail):
        urls.append("/product/detail/%d" % pid)
        weights.append(0.02)

    total = sum(weights)
    return urls, [w / total for w in weights]


# --------------------------------------------------------------------------- #
# 单行日志的各个字段
# --------------------------------------------------------------------------- #
def choose_status(rng: random.Random, url: str) -> int:
    if url in NOT_FOUND_URLS:
        return 404
    if url.startswith("/api/") and rng.random() < 0.012:   # 接口偶发 500
        return 500
    if url.startswith("/static/") and rng.random() < 0.18:  # 静态资源走缓存
        return 304
    return 200


def choose_size(rng: random.Random, url: str, status: int) -> int:
    if status == 304:
        return 0
    if status == 404:
        return rng.choice([153, 162, 169, 178])
    if status == 500:
        return rng.choice([0, 0, 87, 154])
    if url.startswith("/static/"):
        if url.endswith(".js"):
            return rng.randrange(40000, 120000)
        if url.endswith(".css"):
            return rng.randrange(8000, 30000)
        if url.endswith((".png", ".svg")):
            return rng.randrange(1200, 9000)
        if url.endswith(".woff2"):
            return rng.randrange(15000, 40000)
        return rng.randrange(500, 5000)
    if url.startswith("/api/"):
        return rng.randrange(120, 3200)
    return rng.randrange(4000, 26000)


def draw_hours(rng: random.Random, n: int) -> list[int]:
    """按白天高、深夜低的权重抽 n 个小时，并保证 24 个小时每个至少出现一次。"""
    hours = rng.choices(range(24), weights=HOUR_WEIGHTS, k=n)
    counts = Counter(hours)
    for h in range(24):
        if counts[h] == 0:
            busiest = counts.most_common(1)[0][0]
            hours[hours.index(busiest)] = h
            counts[busiest] -= 1
            counts[h] = 1
    return hours


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def generate(n_lines: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    ips = build_ip_pool(rng)
    urls, url_weights = build_url_pool(rng)

    # 少数 IP 是重度用户：权重同样按 1/(i+1)^0.8 衰减
    ip_weights_raw = [1.0 / (i + 1) ** 0.8 for i in range(len(ips))]
    ip_total = sum(ip_weights_raw)
    ip_weights = [w / ip_total for w in ip_weights_raw]

    entries: list[tuple[int, int, int, str]] = []
    for hour in draw_hours(rng, n_lines):
        minute = rng.randrange(60)
        second = rng.randrange(60)
        url = rng.choices(urls, weights=url_weights, k=1)[0]
        ip = rng.choices(ips, weights=ip_weights, k=1)[0]
        status = choose_status(rng, url)
        size = choose_size(rng, url, status)
        method = "POST" if url in POST_URLS else "GET"
        referer = rng.choice(REFERERS)
        ua = rng.choice(USER_AGENTS)

        line = (
            '%s - - [%02d/%s/%d:%02d:%02d:%02d %s] '
            '"%s %s HTTP/1.1" %d %d "%s" "%s"'
            % (ip, DAY, MONTH_NAME, YEAR, hour, minute, second, TZ,
               method, url, status, size, referer, ua)
        )
        entries.append((hour, minute, second, line))

    # 真实 access.log 是时间升序的；sorted 是稳定排序，同一秒内保持生成顺序
    entries.sort(key=lambda e: e[:3])
    return [e[3] for e in entries]


def summarize(lines: list[str]) -> str:
    import re

    ip_re = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})\s")
    status_re = re.compile(r'"\s+(\d{3})\s+\d+')
    ips = [m.group(1) for line in lines if (m := ip_re.match(line))]
    statuses = Counter(m.group(1) for line in lines if (m := status_re.search(line)))
    hours = Counter(line.split("[")[1][12:14] for line in lines if "[" in line)
    status_txt = "  ".join("%s:%d" % (k, statuses[k]) for k in sorted(statuses))
    return (
        "  行数        %d\n"
        "  不同 IP     %d\n"
        "  时间范围    %s 00:00 ~ %s 23:59（共 %d 个小时有数据）\n"
        "  状态码      %s"
        % (len(lines), len(set(ips)), lines[0].split("[")[1][:11],
           lines[0].split("[")[1][:11], len(hours), status_txt)
    )


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="生成合成的 nginx 访问日志（假数据）")
    parser.add_argument("--lines", type=int, default=2000, help="生成多少行，默认 2000")
    parser.add_argument("--seed", type=int, default=20261124, help="随机种子，默认 20261124")
    parser.add_argument("--out", type=Path, default=root / "data" / "sample_access.log",
                        help="输出路径，默认 data/sample_access.log")
    args = parser.parse_args(argv)

    lines = generate(args.lines, args.seed)
    content = "\n".join(lines) + "\n"

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" 保证在 Windows 上也写出 LF，避免 \r 混进日志
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    print("[gen_fake_logs] 已写入 %s" % out_path)
    print(summarize(lines))
    print("  sha256      %s" % digest)
    print("  （种子固定，同一份代码每次生成的内容逐字节一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
