#!/usr/bin/env python3
"""解析 nginx combined 日志并做特征工程。

日志格式（combined 末尾追加响应时间）：
10.12.3.44 - - [24/Nov/2026:09:12:33 +0800] "GET /api/v1/orders HTTP/1.1" 200 532 "-" "curl/8.4.0" 0.023
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

LOG_PATTERN = re.compile(
    r'^(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\s+-\s+-\s+'
    r'\[(?P<time>[^\]]+)\]\s+'
    r'"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/\d(?:\.\d+)?"\s+'
    r'(?P<status>\d{3})\s+(?P<bytes>\d+|-)\s+'
    r'"(?P<referer>[^"]*)"\s+"(?P<ua>[^"]*)"\s+'
    r'(?P<latency>[0-9.]+|-)\s*$'
)

TIME_FORMAT = "%d/%b/%Y:%H:%M:%S %z"

FEATURE_NAMES = [
    "latency_s",
    "latency_route_ratio_log1p",
    "bytes_log1p",
    "status_ge_400",
    "status_ge_500",
    "hour_sin",
    "hour_cos",
    "ip_requests_60s",
    "ip_distinct_paths_60s",
    "ip_errors_60s",
    "path_length",
    "is_static",
]


def parse_log_file(path: str | Path) -> pd.DataFrame:
    """逐行解析日志，返回按 line_id（从 1 开始的物理行号）索引的 DataFrame。

    不合法行会被跳过，并在 ``df.attrs`` 中留下统计信息；有效字段包含：
    line_id, ip, time, time_str, method, path, status, bytes, latency,
    referer, ua。
    """
    path = Path(path)
    records: list[dict] = []
    skipped: list[tuple[int, str]] = []

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                skipped.append((line_number, "空行"))
                continue
            match = LOG_PATTERN.match(line)
            if match is None:
                skipped.append((line_number, "格式不匹配"))
                continue
            groups = match.groupdict()
            try:
                parsed_time = datetime.strptime(groups["time"], TIME_FORMAT)
            except ValueError:
                skipped.append((line_number, "时间格式错误"))
                continue

            records.append(
                {
                    "line_id": line_number,
                    "ip": groups["ip"],
                    "time_str": groups["time"],
                    "method": groups["method"],
                    "path": groups["path"],
                    "status": groups["status"],
                    "bytes": groups["bytes"],
                    "latency": groups["latency"],
                    "referer": groups["referer"],
                    "ua": groups["ua"],
                    "_parsed_time": parsed_time,
                }
            )

    if not records:
        details = f"，首个错误行: {skipped[0]}" if skipped else ""
        raise ValueError(f"没有解析出任何有效日志行: {path}{details}")

    frame = pd.DataFrame.from_records(records)
    frame["time"] = pd.to_datetime(frame["time_str"], format=TIME_FORMAT)
    frame["status"] = pd.to_numeric(frame["status"], errors="coerce").fillna(0).astype(int)
    frame["bytes"] = pd.to_numeric(frame["bytes"], errors="coerce")
    frame["latency"] = pd.to_numeric(frame["latency"], errors="coerce")
    frame["ip"] = frame["ip"].fillna("").astype(str)
    frame["path"] = frame["path"].fillna("").astype(str)
    frame["ua"] = frame["ua"].fillna("-").astype(str)

    frame = frame.drop(columns=["_parsed_time"])
    frame.attrs["lines_read"] = len(records) + len(skipped)
    frame.attrs["lines_parsed"] = len(records)
    frame.attrs["lines_skipped"] = len(skipped)
    frame.attrs["parse_errors"] = skipped[:10]
    return frame.reset_index(drop=True)


def _window_statistics(
    timestamps: np.ndarray,
    ips: np.ndarray,
    paths: np.ndarray,
    statuses: np.ndarray,
    window_seconds: float = 60.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """统计每个请求所属 IP 在前后 window_seconds 内的请求/路径/错误数。"""
    n_rows = len(timestamps)
    request_counts = np.zeros(n_rows, dtype=np.float64)
    distinct_path_counts = np.zeros(n_rows, dtype=np.float64)
    error_counts = np.zeros(n_rows, dtype=np.float64)

    for ip in pd.unique(ips):
        indices = np.flatnonzero(ips == ip)
        group_order = indices[np.argsort(timestamps[indices], kind="stable")]
        group_times = timestamps[group_order]
        group_paths = paths[group_order]
        group_errors = statuses[group_order] >= 400

        left = np.searchsorted(group_times, group_times - window_seconds, side="left")
        right = np.searchsorted(group_times, group_times + window_seconds, side="right")
        for position, row_index in enumerate(group_order):
            start = int(left[position])
            end = int(right[position])
            request_counts[row_index] = end - start
            distinct_path_counts[row_index] = len(set(group_paths[start:end]))
            error_counts[row_index] = int(group_errors[start:end].sum())

    return request_counts, distinct_path_counts, error_counts


def build_features(df: pd.DataFrame) -> tuple[np.ndarray, list[str]]:
    """把解析后的日志转为数值特征矩阵，保证输出矩阵没有 NaN / inf。"""
    if df.empty:
        raise ValueError("日志 DataFrame 为空，无法构造特征")

    latency = pd.to_numeric(df["latency"], errors="coerce")
    latency_median = float(latency.median()) if latency.notna().any() else 0.0
    if not np.isfinite(latency_median) or latency_median < 0:
        latency_median = 0.0
    latency = latency.fillna(latency_median).clip(lower=0.0).astype(float)

    byte_values = pd.to_numeric(df["bytes"], errors="coerce")
    bytes_median = float(byte_values.median()) if byte_values.notna().any() else 0.0
    if not np.isfinite(bytes_median) or bytes_median < 0:
        bytes_median = 0.0
    byte_values = byte_values.fillna(bytes_median).clip(lower=0.0).astype(float)

    statuses = pd.to_numeric(df["status"], errors="coerce").fillna(0).astype(int)
    paths = df["path"].fillna("").astype(str)
    ips = df["ip"].fillna("").astype(str)

    parsed_times = df["time"]
    if not pd.api.types.is_datetime64_any_dtype(parsed_times):
        parsed_times = pd.to_datetime(parsed_times, format=TIME_FORMAT, errors="coerce")
    hour_float = (
        parsed_times.dt.hour.fillna(0)
        + parsed_times.dt.minute.fillna(0) / 60.0
        + parsed_times.dt.second.fillna(0) / 3600.0
    ).to_numpy(dtype=np.float64)
    timestamps = parsed_times.map(
        lambda value: value.timestamp() if not pd.isna(value) else np.nan
    ).to_numpy(dtype=np.float64)
    if np.isnan(timestamps).any():
        # 极端脏数据也不允许 NaN 进模型；没有解析到时间的行按当天 0 点处理。
        finite_fallback = timestamps[np.isfinite(timestamps)]
        fallback = float(finite_fallback.min()) if finite_fallback.size else 0.0
        timestamps = np.where(np.isnan(timestamps), fallback, timestamps)

    request_counts, distinct_path_counts, error_counts = _window_statistics(
        timestamps, ips.to_numpy(), paths.to_numpy(), statuses.to_numpy()
    )

    is_static = paths.str.startswith("/static/").to_numpy(dtype=bool)
    byte_log = np.log1p(np.maximum(byte_values.to_numpy(dtype=np.float64), 0.0))
    hour_angle = 2.0 * np.pi * hour_float / 24.0

    # 相对延迟：同一路由模板内与中位数对比，避免不同接口的基线差异掩盖慢请求。
    route_signature = (
        paths.str.replace(r"\?.*$", "", regex=True).str.replace(r"\d+", "{id}", regex=True)
    )
    route_median = latency.groupby(route_signature).transform("median")
    route_median = route_median.fillna(latency_median).clip(lower=0.001)
    route_ratio = np.log1p(latency.to_numpy() / route_median.to_numpy())

    features = np.column_stack(
        [
            latency.to_numpy(dtype=np.float64),
            route_ratio,
            byte_log,
            (statuses.to_numpy() >= 400).astype(np.float64),
            (statuses.to_numpy() >= 500).astype(np.float64),
            np.sin(hour_angle),
            np.cos(hour_angle),
            request_counts,
            distinct_path_counts,
            error_counts,
            paths.str.len().to_numpy(dtype=np.float64),
            is_static.astype(np.float64),
        ]
    )

    if features.shape[1] != len(FEATURE_NAMES):
        raise AssertionError("特征数量与特征名数量不一致")
    if not np.isfinite(features).all():
        bad_columns = [
            FEATURE_NAMES[index]
            for index in np.where(~np.isfinite(features).all(axis=0))[0]
        ]
        raise ValueError(f"特征矩阵包含非有限值: {bad_columns}")

    return np.ascontiguousarray(features, dtype=np.float64), list(FEATURE_NAMES)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    default_path = Path(__file__).resolve().parents[1] / "data" / "sample_access.log"
    parsed = parse_log_file(default_path)
    matrix, names = build_features(parsed)
    print(f"解析 {len(parsed)} 行，特征矩阵 {matrix.shape[0]} x {matrix.shape[1]}")
    print("特征:", ", ".join(names))
