#!/usr/bin/env python3
"""命令行入口：解析日志 -> 特征工程 -> 孤立森林 -> 异常报告与评估。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from iforest import IsolationForest
from parser import FEATURE_NAMES, build_features, parse_log_file

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "sample_access.log"
DEFAULT_LABELS = ROOT / "data" / "labels.csv"
DEFAULT_OUT = ROOT / "reports"

RANDOM_STATE = 42
N_ESTIMATORS = 100
MAX_SAMPLES = 256


def _configure_stdout() -> None:
    """Windows 控制台默认可能是 GBK，统一改成 UTF-8。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        # 极老的 Python 或已被重定向且不支持 reconfigure 的流。
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="基于孤立森林的 nginx 访问日志无监督异常检测"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="nginx 日志路径（默认 data/sample_access.log）",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS,
        help="真值表 CSV 路径（默认 data/labels.csv）",
    )
    parser.add_argument(
        "--contamination",
        type=float,
        default=0.03,
        help="预期异常比例，范围 (0, 0.5]，默认 0.03",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="输出目录（默认 reports）",
    )
    return parser.parse_args(argv)


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _safe_metric(numerator: int, denominator: int) -> float:
    return float(numerator / denominator) if denominator else 0.0


def evaluate(
    report: pd.DataFrame, labels_path: Path
) -> tuple[dict, pd.DataFrame] | None:
    """按 line_id 对齐标签并计算 precision / recall / F1。"""
    if not labels_path.exists():
        print(f"\n[评估] 未找到标签文件 {labels_path}，跳过指标计算。")
        return None

    labels = pd.read_csv(labels_path)
    missing = {"line_id", "is_anomaly"} - set(labels.columns)
    if missing:
        raise ValueError(f"标签文件缺少列: {sorted(missing)}")
    if labels["line_id"].duplicated().any():
        raise ValueError("标签文件存在重复的 line_id")

    labels = labels.rename(columns={"is_anomaly": "true_label"})
    labels["line_id"] = pd.to_numeric(labels["line_id"], errors="coerce")
    labels["true_label"] = pd.to_numeric(labels["true_label"], errors="coerce")
    labels = labels.dropna(subset=["line_id", "true_label"])
    labels["line_id"] = labels["line_id"].astype(int)
    labels["true_label"] = labels["true_label"].astype(int)
    if not labels["true_label"].isin([0, 1]).all():
        raise ValueError("标签 is_anomaly 只能是 0 或 1")

    evaluated = report.merge(
        labels[["line_id", "true_label"]], on="line_id", how="inner"
    )
    if evaluated.empty:
        print("\n[评估] 标签与日志行号没有交集，跳过指标计算。")
        return None

    y_true = evaluated["true_label"].to_numpy(dtype=int)
    y_pred = evaluated["is_anomaly"].to_numpy(dtype=int)
    tp = int(((y_true == 1) & (y_pred == 1)).sum())
    fp = int(((y_true == 0) & (y_pred == 1)).sum())
    fn = int(((y_true == 1) & (y_pred == 0)).sum())
    tn = int(((y_true == 0) & (y_pred == 0)).sum())

    precision = _safe_metric(tp, tp + fp)
    recall = _safe_metric(tp, tp + fn)
    f1 = _safe_metric(2 * precision * recall, precision + recall)

    metrics = {
        "n_samples": int(len(report)),
        "n_labeled": int(len(evaluated)),
        "n_true_anomalies": int(y_true.sum()),
        "n_predicted_anomalies": int((report["is_anomaly"] == 1).sum()),
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
    }

    print("\n[评估] 以 labels.csv 为真值（按 line_id 对齐）")
    print(f"  参与评估: {len(evaluated)} 行，其中真异常 {int(y_true.sum())} 行")
    print(f"  混淆矩阵: TP={tp}  FP={fp}  FN={fn}  TN={tn}")
    print(f"  Precision: {precision:.4f}")
    print(f"  Recall:    {recall:.4f}")
    print(f"  F1:        {f1:.4f}")
    return metrics, evaluated


def _print_top_anomalies(report: pd.DataFrame, top_n: int = 5) -> None:
    print(f"\nTop {top_n} 异常行摘要（按异常分数降序）:")
    header = (
        f"{'排名':<4} {'行号':>5}  {'时间':<19}  {'来源 IP':<15}  "
        f"{'方法':<4} {'路径':<36} {'状态':>4} {'延迟(s)':>9} {'分数':>8}  判定"
    )
    print(header)
    print("-" * len(header))
    for rank, row in enumerate(report.head(top_n).itertuples(index=False), start=1):
        path = str(row.path)
        if len(path) > 34:
            path = path[:31] + "..."
        verdict = "异常" if int(row.is_anomaly) == 1 else "正常"
        print(
            f"{rank:<4} {int(row.line_id):>5}  {str(row.time):<19}  {str(row.ip):<15}  "
            f"{str(row.method):<4} {path:<36} {int(row.status):>4} "
            f"{float(row.latency):>9.3f} {float(row.anomaly_score):>8.4f}  {verdict}"
        )


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    args = _parse_args(argv)
    if not 0.0 < args.contamination <= 0.5:
        print("错误: --contamination 必须在 (0, 0.5] 区间内", file=sys.stderr)
        return 2
    if not args.input.exists():
        print(f"错误: 输入日志不存在: {args.input}", file=sys.stderr)
        return 2

    print("=" * 78)
    print("基于孤立森林的服务器日志异常检测")
    print("=" * 78)

    # 1. 解析
    print("\n[1/4] 解析 nginx 访问日志")
    print(f"  输入: {_display_path(args.input)}")
    frame = parse_log_file(args.input)
    lines_read = int(frame.attrs.get("lines_read", len(frame)))
    skipped = int(frame.attrs.get("lines_skipped", 0))
    print(f"  读取 {lines_read} 行，成功解析 {len(frame)} 行，跳过 {skipped} 行")
    time_min = frame["time"].min()
    time_max = frame["time"].max()
    if pd.notna(time_min) and pd.notna(time_max):
        print(f"  时间范围: {time_min} ~ {time_max}")
    if skipped:
        print(f"  首个跳过示例: 第 {frame.attrs['parse_errors'][0][0]} 行")

    # 2. 特征
    print("\n[2/4] 构造数值特征")
    features, feature_names = build_features(frame)
    print(f"  特征矩阵: {features.shape[0]} 行 x {features.shape[1]} 维")
    print(f"  特征: {', '.join(feature_names)}")
    if feature_names != FEATURE_NAMES:
        print("  警告: 特征名列表与 parser.FEATURE_NAMES 不一致", file=sys.stderr)

    # 3. 模型
    print("\n[3/4] 训练孤立森林")
    model = IsolationForest(
        n_estimators=N_ESTIMATORS,
        max_samples=MAX_SAMPLES,
        contamination=args.contamination,
        random_state=RANDOM_STATE,
    )
    print(
        f"  参数: n_estimators={N_ESTIMATORS}, max_samples={MAX_SAMPLES}, "
        f"contamination={args.contamination}, random_state={RANDOM_STATE}"
    )
    model.fit(features)
    scores = model.score_samples(features)
    predictions = model.predict(features)
    predicted_total = int((predictions == 1).sum())
    print(f"  每棵树实际子采样: {model.max_samples_} 行")
    print(f"  判定阈值: {model.threshold_:.8f}")
    print(f"  预测异常: {predicted_total} / {len(frame)} ({predicted_total / len(frame):.2%})")

    # 4. 报告
    print("\n[4/4] 生成报告")
    report = pd.DataFrame(
        {
            "line_id": frame["line_id"].astype(int),
            "time": frame["time_str"].astype(str),
            "ip": frame["ip"].astype(str),
            "method": frame["method"].astype(str),
            "path": frame["path"].astype(str),
            "status": frame["status"].astype(int),
            "bytes": frame["bytes"],
            "latency": frame["latency"],
            "ua": frame["ua"].astype(str),
            "referer": frame["referer"].astype(str),
            "anomaly_score": np.round(scores, 8),
            "is_anomaly": (predictions == 1).astype(int),
        }
    )
    report = report.sort_values(
        "anomaly_score", ascending=False, kind="mergesort"
    ).reset_index(drop=True)

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "anomaly_report.csv"
    report.to_csv(report_path, index=False, encoding="utf-8")
    print(f"  异常报告: {_display_path(report_path)}（{len(report)} 行）")

    result = evaluate(report, args.labels)
    if result is not None:
        metrics, _ = result
        metrics["config"] = {
            "n_estimators": N_ESTIMATORS,
            "max_samples": MAX_SAMPLES,
            "contamination": args.contamination,
            "random_state": RANDOM_STATE,
        }
        metrics_path = out_dir / "metrics.json"
        with metrics_path.open("w", encoding="utf-8") as handle:
            json.dump(metrics, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(f"  评估指标: {_display_path(metrics_path)}")

    _print_top_anomalies(report, top_n=5)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
