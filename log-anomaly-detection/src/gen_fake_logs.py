#!/usr/bin/env python3
"""生成用于演示的合成 nginx 访问日志与真值表。

所有数据均为脚本合成：IP 仅来自 RFC 5737 / RFC 1918 文档或私网保留段，
域名仅使用 example.com / example.internal，不包含任何真实服务器信息。

固定随机种子，重复运行会得到完全一致的数据。
"""

from __future__ import annotations

import csv
import ipaddress
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
SEED = 20261124
TOTAL_ROWS = 600
N_ANOMALIES = 18  # 恰好 3.0%
BASE_DATE = datetime(2026, 11, 24)
TIMEZONE_TEXT = "+0800"

# 只在文档保留段 / 私网段中取 IP，防止任何真实公网地址进入样例数据。
NORMAL_IPS = [
    "10.12.3.44",
    "10.12.3.45",
    "10.12.3.77",
    "10.20.30.12",
    "10.20.30.15",
    "10.20.30.21",
    "10.20.30.33",
    "10.30.4.8",
    "10.30.4.9",
    "10.30.4.10",
    "10.40.5.101",
    "10.40.5.102",
    "10.40.5.110",
    "10.40.5.121",
    "192.0.2.10",
    "192.0.2.23",
    "192.0.2.44",
    "198.51.100.17",
    "198.51.100.24",
    "198.51.100.31",
    "203.0.113.9",
    "203.0.113.28",
    "203.0.113.41",
    "10.50.6.7",
]
SCANNER_IP = "203.0.113.66"
ALLOWED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
]

# 白天高、凌晨低的访问权重（0 点至 23 点）。
HOUR_WEIGHTS = np.array(
    [
        0.22,
        0.16,
        0.12,
        0.10,
        0.12,
        0.25,
        0.52,
        0.92,
        1.45,
        2.20,
        2.65,
        2.45,
        2.05,
        2.25,
        2.40,
        2.30,
        2.10,
        1.90,
        1.65,
        1.35,
        1.10,
        0.82,
        0.55,
        0.32,
    ],
    dtype=float,
)
HOUR_PROBS = HOUR_WEIGHTS / HOUR_WEIGHTS.sum()

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "curl/8.4.0",
]
REFERERS = [
    "-",
    "-",
    "-",
    "https://shop.example.com/",
    "https://shop.example.com/catalog",
    "https://shop.example.com/products",
]

SEARCH_TERMS = [
    "mechanical_keyboard",
    "usb_hub",
    "monitor_arm",
    "laptop_stand",
    "noise_cancelling",
]

# 正常路由：(path 生成用的 key, 权重, 方法, 响应体大小区间, 延迟中位数秒)
ROUTES = [
    ("orders_list", 0.14, "GET"),
    ("order_detail", 0.10, "GET"),
    ("user_me", 0.08, "GET"),
    ("products", 0.11, "GET"),
    ("search", 0.10, "GET"),
    ("create_order", 0.04, "POST"),
    ("login", 0.04, "POST"),
    ("health", 0.05, "GET"),
    ("static_css", 0.10, "GET"),
    ("static_js", 0.10, "GET"),
    ("static_img", 0.09, "GET"),
    ("favicon", 0.05, "GET"),
]
ROUTE_WEIGHTS = np.array([r[1] for r in ROUTES], dtype=float)
ROUTE_WEIGHTS = ROUTE_WEIGHTS / ROUTE_WEIGHTS.sum()

SCAN_PATHS = [
    "/admin",
    "/wp-login.php",
    "/.env",
    "/.git/config",
    "/phpmyadmin/index.php",
    "/server-status",
    "/api/v1/users?id=1%20OR%201=1",
    "/actuator/env",
    "/config.json",
    "/backup.zip",
    "/shell.php",
    "/api/v1/debug",
]


def _assert_allowed_ip(ip: str) -> None:
    """拒绝不是保留段的地址，作为数据合规的最后一道检查。"""
    address = ipaddress.ip_address(ip)
    if not any(address in network for network in ALLOWED_NETWORKS):
        raise ValueError(f"IP 不在允许的保留段内: {ip}")


def _format_nginx_time(dt: datetime) -> str:
    return (
        f"{dt.day:02d}/{MONTHS[dt.month - 1]}/{dt.year}:"
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} {TIMEZONE_TEXT}"
    )


def _random_normal_time(rng: np.random.Generator) -> datetime:
    hour = int(rng.choice(24, p=HOUR_PROBS))
    return BASE_DATE.replace(
        hour=hour,
        minute=int(rng.integers(0, 60)),
        second=int(rng.integers(0, 60)),
    )


def _route_path(route: str, rng: np.random.Generator) -> tuple[str, str]:
    """返回（路径, 路由类型），路径全部是假域名下的假接口。"""
    if route == "orders_list":
        return "/api/v1/orders", "api"
    if route == "order_detail":
        return f"/api/v1/orders/{int(rng.integers(1000, 1100))}", "api"
    if route == "user_me":
        return "/api/v1/users/me", "api"
    if route == "products":
        return f"/api/v1/products?page={int(rng.integers(1, 8))}", "api"
    if route == "search":
        term = SEARCH_TERMS[int(rng.integers(0, len(SEARCH_TERMS)))]
        return f"/api/v1/search?q={term}", "api"
    if route == "create_order":
        return "/api/v1/orders", "api"
    if route == "login":
        return "/login", "login"
    if route == "health":
        return "/health", "health"
    if route == "static_css":
        return "/static/css/app.css", "static"
    if route == "static_js":
        digest = f"{int(rng.integers(0, 16**8)):08x}"
        return f"/static/js/app.{digest}.js", "static"
    if route == "static_img":
        return "/static/img/logo.png", "static"
    if route == "favicon":
        return "/favicon.ico", "static"
    raise AssertionError(f"未知路由: {route}")


def _make_normal_event(rng: np.random.Generator, dt: datetime) -> dict:
    ip = NORMAL_IPS[int(rng.choice(len(NORMAL_IPS), p=_ip_probs()))]
    route = ROUTES[int(rng.choice(len(ROUTES), p=ROUTE_WEIGHTS))][0]
    path, kind = _route_path(route, rng)
    method = "POST" if route in {"create_order", "login"} else "GET"

    # 业务接口以 200 为主，偶发少量 4xx；正常流量没有 5xx。
    if kind == "static":
        status = 200
        if route == "static_img":
            size = int(rng.integers(4_000, 80_000))
            latency = float(rng.lognormal(np.log(0.009), 0.25))
        elif route == "static_css":
            size = int(rng.integers(8_000, 28_000))
            latency = float(rng.lognormal(np.log(0.006), 0.22))
        elif route == "static_js":
            size = int(rng.integers(40_000, 180_000))
            latency = float(rng.lognormal(np.log(0.010), 0.28))
        else:
            size = int(rng.integers(500, 1_500))
            latency = float(rng.lognormal(np.log(0.004), 0.20))
    elif kind == "health":
        status = 200
        size = 15
        latency = float(rng.lognormal(np.log(0.003), 0.25))
    elif kind == "login":
        draw = rng.random()
        if draw < 0.82:
            status, size = 200, int(rng.integers(120, 320))
        elif draw < 0.97:
            status, size = 302, 0
        else:
            status, size = 401, int(rng.integers(80, 140))
        latency = float(rng.lognormal(np.log(0.045), 0.40))
    else:
        draw = rng.random()
        if draw < 0.985:
            status = 201 if method == "POST" else 200
            size = int(rng.integers(180, 6_500))
            latency = float(rng.lognormal(np.log(0.075), 0.48))
        elif draw < 0.995:
            status = 404 if rng.random() < 0.7 else 400
            size = int(rng.integers(60, 180))
            latency = float(rng.lognormal(np.log(0.030), 0.35))
        else:
            status, size = 429, int(rng.integers(60, 160))
            latency = float(rng.lognormal(np.log(0.012), 0.30))

    latency = float(np.clip(latency, 0.002, 1.2))
    if kind == "health" and rng.random() < 0.6:
        ua = "curl/8.4.0"
        referer = "-"
    elif kind == "static":
        ua = USER_AGENTS[int(rng.integers(0, 3))]
        referer = REFERERS[int(rng.integers(0, len(REFERERS)))]
    else:
        ua = USER_AGENTS[int(rng.integers(0, 3))]
        referer = "-" if rng.random() < 0.65 else REFERERS[int(rng.integers(3, len(REFERERS)))]

    return {
        "dt": dt,
        "ip": ip,
        "method": method,
        "path": path,
        "status": int(status),
        "size": int(size),
        "referer": referer,
        "ua": ua,
        "latency": round(latency, 3),
        "label": 0,
        "anomaly_type": "normal",
    }


def _make_latency_spike(rng: np.random.Generator) -> dict:
    ip = NORMAL_IPS[int(rng.integers(0, len(NORMAL_IPS)))]
    return {
        "dt": _random_normal_time(rng),
        "ip": ip,
        "method": "GET",
        "path": "/api/v1/orders",
        "status": 200,
        "size": int(rng.integers(300, 2_500)),
        "referer": "-",
        "ua": USER_AGENTS[int(rng.integers(0, 3))],
        "latency": round(float(rng.uniform(3.0, 15.0)), 3),
        "label": 1,
        "anomaly_type": "latency_spike",
    }


def _make_server_error(rng: np.random.Generator) -> dict:
    ip = NORMAL_IPS[int(rng.integers(0, len(NORMAL_IPS)))]
    path, _ = _route_path(
        ["order_detail", "orders_list", "search"][int(rng.integers(0, 3))], rng
    )
    return {
        "dt": _random_normal_time(rng),
        "ip": ip,
        "method": "GET",
        "path": path,
        "status": int(rng.choice([500, 502, 503])),
        "size": int(rng.integers(80, 400)),
        "referer": "-",
        "ua": USER_AGENTS[int(rng.integers(0, 3))],
        "latency": round(float(rng.uniform(0.20, 1.50)), 3),
        "label": 1,
        "anomaly_type": "server_error",
    }


def _make_scan_burst(rng: np.random.Generator) -> list[dict]:
    """同一分钟内、同一 IP 对 12 个不同路径发起 404 扫描。"""
    burst_start = BASE_DATE.replace(hour=3, minute=42, second=7)
    events = []
    for index, path in enumerate(SCAN_PATHS):
        events.append(
            {
                "dt": burst_start + timedelta(seconds=int(rng.integers(0, 36))),
                "ip": SCANNER_IP,
                "method": "GET",
                "path": path,
                "status": 404,
                "size": int(rng.integers(140, 220)),
                "referer": "-",
                "ua": "Mozilla/5.0 (compatible; probe/1.0; +https://scanner.example.internal/bot)",
                "latency": round(float(rng.uniform(0.002, 0.009)), 3),
                "label": 1,
                "anomaly_type": "scan_404",
                "_burst_order": index,
            }
        )
    return events


def _ip_probs() -> np.ndarray:
    weights = np.linspace(2.6, 0.55, len(NORMAL_IPS))
    return weights / weights.sum()


def _format_log_line(event: dict) -> str:
    return (
        f"{event['ip']} - - [{_format_nginx_time(event['dt'])}] "
        f"\"{event['method']} {event['path']} HTTP/1.1\" "
        f"{event['status']} {event['size']} "
        f"\"{event['referer']}\" \"{event['ua']}\" {event['latency']:.3f}"
    )


def generate() -> tuple[list[dict], Counter]:
    """生成事件并按时间排序，返回事件列表与异常类型统计。"""
    rng = np.random.default_rng(SEED)

    # 18 个异常：3 个延迟尖峰、3 个 5xx、12 条扫描（共 3.0%）。
    # 12 条扫描是在 600 行总量、最多 3% 异常的前提下能保留的 burst 规模。
    events = [_make_normal_event(rng, _random_normal_time(rng)) for _ in range(TOTAL_ROWS - N_ANOMALIES)]
    events.extend(_make_latency_spike(rng) for _ in range(3))
    events.extend(_make_server_error(rng) for _ in range(3))
    events.extend(_make_scan_burst(rng))

    if len(events) != TOTAL_ROWS:
        raise AssertionError(f"期望 {TOTAL_ROWS} 行，实际 {len(events)} 行")

    # 稳定排序：同一秒内保持生成顺序。标签在排序后按 line_id 重新对应。
    events.sort(key=lambda item: item["dt"])

    for line_id, event in enumerate(events, start=1):
        event["line_id"] = line_id
        _assert_allowed_ip(event["ip"])

    counts = Counter(event["anomaly_type"] for event in events)
    return events, counts


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

    root = Path(__file__).resolve().parents[1]
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    events, counts = generate()
    log_path = data_dir / "sample_access.log"
    labels_path = data_dir / "labels.csv"

    with log_path.open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(_format_log_line(event) + "\n")

    with labels_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["line_id", "is_anomaly"])
        for event in events:
            writer.writerow([event["line_id"], event["label"]])

    total = len(events)
    anomaly_total = sum(event["label"] for event in events)
    print(f"随机种子: {SEED}")
    print(f"已生成 {total} 行日志 -> {log_path}")
    print(f"已生成标签 {total} 行 -> {labels_path}")
    print(f"异常行数: {anomaly_total} ({anomaly_total / total:.2%})")
    print("异常类型分布:")
    for anomaly_type in ("latency_spike", "server_error", "scan_404"):
        print(f"  - {anomaly_type}: {counts[anomaly_type]} 行")
    print("IP 范围: 10.0.0.0/8、192.0.2.0/24、198.51.100.0/24、203.0.113.0/24")
    print("域名范围: example.com / example.internal（纯虚构）")


if __name__ == "__main__":
    main()
