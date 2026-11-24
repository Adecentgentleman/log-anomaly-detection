"""HTML / CSV 报表生成。

HTML 为完全自包含的单文件：所有 CSS 内联，不引用任何外部字体、脚本或图片，
因此在离线环境和任意浏览器里都能直接打开。
"""

from __future__ import annotations

import csv
import html
import platform
import socket
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

STATUS_CLASS = {"PASS": "pass", "WARN": "warn", "FAIL": "fail", "SKIP": "skip"}
CSV_HEADERS = ["检查项", "对象", "实际值", "阈值", "状态", "详情"]

CSS = """
:root {
  --ink: #172033;
  --muted: #667085;
  --line: #e6eaf2;
  --bg: #f4f6fb;
  --card: #ffffff;
  --pass: #0f8a5f;
  --pass-bg: #e7f7f0;
  --warn: #a15c00;
  --warn-bg: #fff4dd;
  --fail: #c4322b;
  --fail-bg: #fdeceb;
  --skip: #687385;
  --skip-bg: #eef1f5;
  --navy: #12233f;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  background: var(--bg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  font-size: 14px;
  line-height: 1.55;
}
.page { max-width: 1240px; margin: 0 auto; padding: 28px 24px 44px; }
.hero {
  color: #fff;
  background: linear-gradient(120deg, #12233f 0%, #1b3d66 58%, #1d5b72 100%);
  border-radius: 18px;
  padding: 28px 30px 25px;
  box-shadow: 0 12px 30px rgba(18, 35, 63, .16);
}
.eyebrow { color: #9fd7e6; font-size: 12px; letter-spacing: .14em; text-transform: uppercase; font-weight: 700; }
h1 { margin: 6px 0 8px; font-size: 28px; line-height: 1.25; letter-spacing: -.02em; }
.subtitle { color: #cfdbeb; font-size: 13px; }
.subtitle span { color: #fff; font-weight: 600; }
.alert {
  margin-top: 18px; padding: 10px 13px; border: 1px solid rgba(255,255,255,.22);
  border-radius: 10px; color: #ffe4e0; background: rgba(196, 50, 43, .28); font-size: 13px;
}
.cards { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; margin: 18px 0 22px; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 13px;
  padding: 14px 16px; box-shadow: 0 3px 12px rgba(23,32,51,.035);
}
.card .label { color: var(--muted); font-size: 12px; font-weight: 600; }
.card .num { margin-top: 2px; font-size: 27px; font-weight: 750; line-height: 1.2; font-variant-numeric: tabular-nums; }
.card.pass .num { color: var(--pass); }
.card.warn .num { color: var(--warn); }
.card.fail .num { color: var(--fail); }
.card.skip .num { color: var(--skip); }
.card.total .num, .card.time .num { color: var(--navy); }
.card.time .num { font-size: 20px; padding-top: 6px; }
.panel {
  overflow: hidden; background: var(--card); border: 1px solid var(--line);
  border-radius: 15px; box-shadow: 0 5px 20px rgba(23,32,51,.045);
}
.panel-head { display: flex; justify-content: space-between; gap: 16px; align-items: baseline; padding: 17px 20px; border-bottom: 1px solid var(--line); }
.panel-head h2 { margin: 0; font-size: 16px; }
.panel-head .hint { color: var(--muted); font-size: 12px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
th {
  padding: 11px 14px; color: #596477; background: #f8f9fc; border-bottom: 1px solid var(--line);
  font-size: 11px; font-weight: 700; letter-spacing: .04em; text-align: left; text-transform: uppercase; white-space: nowrap;
}
td { padding: 13px 14px; border-bottom: 1px solid #edf0f6; vertical-align: top; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: #fbfcff; }
.name { font-weight: 650; white-space: nowrap; }
.target { min-width: 155px; font-weight: 550; }
.mono { color: #374151; font-family: "Cascadia Mono", Consolas, "Liberation Mono", monospace; font-size: 12.5px; }
.threshold { min-width: 175px; color: var(--muted); font-size: 12.5px; }
.detail { min-width: 260px; color: var(--muted); font-size: 12.5px; }
.badge {
  display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px;
  border-radius: 999px; font-size: 11px; font-weight: 800; letter-spacing: .04em; white-space: nowrap;
}
.badge::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.badge.pass { color: var(--pass); background: var(--pass-bg); }
.badge.warn { color: var(--warn); background: var(--warn-bg); }
.badge.fail { color: var(--fail); background: var(--fail-bg); }
.badge.skip { color: var(--skip); background: var(--skip-bg); }
.empty { padding: 42px 20px; color: var(--muted); text-align: center; }
footer { display: flex; justify-content: space-between; gap: 16px; margin-top: 16px; padding: 0 4px; color: #8a94a6; font-size: 11.5px; }
@media (max-width: 980px) {
  .cards { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 620px) {
  .page { padding: 14px 12px 28px; }
  .hero { padding: 22px 20px; }
  h1 { font-size: 22px; }
  .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .card.time { grid-column: span 2; }
  footer { display: block; }
}
@media print {
  body { background: #fff; }
  .page { max-width: none; padding: 0; }
  .hero, .panel, .card { box-shadow: none; }
  .card, .panel { break-inside: avoid; }
}
"""


def _safe_text(value: Any) -> str:
    """所有进入 HTML 的动态内容都必须经过这里。"""
    return html.escape("" if value is None else str(value), quote=True)


def _detail(value: Any) -> str:
    return _safe_text(value).replace("\r\n", "\n").replace("\n", "<br>")


def _count_config(config: dict[str, Any], key: str) -> int:
    value = config.get(key, [])
    return len(value) if isinstance(value, list) else 0


def _format_elapsed(elapsed: float | None) -> str:
    if elapsed is None:
        return "—"
    if elapsed < 60:
        return f"{elapsed:.2f}s"
    minutes, seconds = divmod(elapsed, 60)
    return f"{int(minutes)}m {seconds:.1f}s"


def render_html(
    results: Iterable[Any],
    config: dict[str, Any],
    out_path: str | Path,
    elapsed: float | None = None,
    generated_at: datetime | None = None,
) -> Path:
    """生成单文件 HTML 报表，返回绝对路径。"""
    rows = list(results)
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    generated = generated_at or datetime.now()
    counts = Counter(str(getattr(item, "status", "SKIP")).upper() for item in rows)
    total = len(rows)
    fail_count = counts.get("FAIL", 0)
    warn_count = counts.get("WARN", 0)

    cards = [
        ("total", "检查项总数", total),
        ("pass", "PASS", counts.get("PASS", 0)),
        ("warn", "WARN", warn_count),
        ("fail", "FAIL", fail_count),
        ("skip", "SKIP", counts.get("SKIP", 0)),
        ("time", "巡检耗时", _format_elapsed(elapsed)),
    ]
    cards_html = "".join(
        f'<div class="card {css_class}"><div class="label">{_safe_text(label)}</div>'
        f'<div class="num">{_safe_text(value)}</div></div>'
        for css_class, label, value in cards
    )

    alert_html = ""
    if fail_count:
        alert_html = (
            '<div class="alert">检测到 '
            f"{fail_count} 项 FAIL，请优先处理。告警邮件已写入 outbox/（dry-run，未实际发送）。"
            "</div>"
        )

    row_parts: list[str] = []
    for item in rows:
        status = str(getattr(item, "status", "SKIP")).upper()
        css_class = STATUS_CLASS.get(status, "skip")
        row_parts.append(
            "<tr>"
            f'<td class="name">{_safe_text(getattr(item, "name", ""))}</td>'
            f'<td class="target">{_safe_text(getattr(item, "target", ""))}</td>'
            f'<td class="mono">{_safe_text(getattr(item, "value", ""))}</td>'
            f'<td class="threshold">{_safe_text(getattr(item, "threshold", ""))}</td>'
            f'<td><span class="badge {css_class}">{_safe_text(status)}</span></td>'
            f'<td class="detail">{_detail(getattr(item, "detail", ""))}</td>'
            "</tr>"
        )

    if row_parts:
        table_body = "".join(row_parts)
        table_html = (
            '<div class="table-wrap"><table>'
            "<thead><tr>"
            "<th>检查项</th><th>对象</th><th>实际值</th><th>阈值</th><th>状态</th><th>详情</th>"
            "</tr></thead>"
            f"<tbody>{table_body}</tbody></table></div>"
        )
    else:
        table_html = '<div class="empty">本轮没有产生检查结果。</div>'

    hostname = socket.gethostname() or "unknown"
    system_name = platform.system() or "unknown"
    config_note = (
        f"端口 {_count_config(config, 'ports')} · 进程 {_count_config(config, 'processes')} · "
        f"目录 {_count_config(config, 'dirs')} · 文件 {_count_config(config, 'log_files')}"
    )

    document = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="ops-check-script">
<title>巡检报表 {_safe_text(generated.strftime("%Y-%m-%d %H:%M:%S"))}</title>
<style>{CSS}</style>
</head>
<body>
<main class="page">
  <header class="hero">
    <div class="eyebrow">OPS CHECK · HEALTH REPORT</div>
    <h1>服务器巡检报表</h1>
    <div class="subtitle">
      <span>{_safe_text(hostname)}</span> · {_safe_text(system_name)} ·
      生成于 {_safe_text(generated.strftime("%Y-%m-%d %H:%M:%S"))} ·
      配置规模：{_safe_text(config_note)}
    </div>
    {alert_html}
  </header>

  <section class="cards" aria-label="巡检摘要">
    {cards_html}
  </section>

  <section class="panel">
    <div class="panel-head">
      <h2>检查明细</h2>
      <div class="hint">PASS 正常 · WARN 注意 · FAIL 异常 · SKIP 跳过/无法检查</div>
    </div>
    {table_html}
  </section>

  <footer>
    <span>ops-check-script · dry-run 巡检报告 · 本文件不引用任何外部资源</span>
    <span>退出码约定：存在 FAIL 时为 1，否则为 0</span>
  </footer>
</main>
</body>
</html>
"""
    destination.write_text(document, encoding="utf-8", newline="\n")
    return destination.resolve()


def write_csv(results: Iterable[Any], out_path: str | Path) -> Path:
    """写出带 UTF-8 BOM 的 CSV，Excel 直接打开中文不乱码。"""
    destination = Path(out_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADERS)
        for item in results:
            writer.writerow(
                [
                    getattr(item, "name", ""),
                    getattr(item, "target", ""),
                    getattr(item, "value", ""),
                    getattr(item, "threshold", ""),
                    getattr(item, "status", ""),
                    getattr(item, "detail", ""),
                ]
            )
    return destination.resolve()
