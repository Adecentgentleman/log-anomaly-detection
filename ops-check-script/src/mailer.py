"""告警邮件（dry-run）。

本模块只把告警写成 .eml 文件，**不建立任何网络连接，也绝不真的发信**。
这样本地开发、演示和 CI 都能安全运行；需要真实发信时，按文件底部注释替换写文件
那几行即可（smtplib.SMTP + send_message）。
"""

from __future__ import annotations

import socket
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Iterable

DEFAULT_FROM = "ops-check@example.local"
DEFAULT_TO = "oncall@example.local"


def _header_safe(value: Any) -> str:
    """防止配置里的换行混入邮件头。"""
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def _recipients(mail_config: dict[str, Any]) -> list[str]:
    raw = mail_config.get("to", DEFAULT_TO)
    if isinstance(raw, str):
        candidates = raw.replace(";", ",").split(",")
    elif isinstance(raw, (list, tuple)):
        candidates = list(raw)
    else:
        candidates = [DEFAULT_TO]
    cleaned = [_header_safe(item) for item in candidates if _header_safe(item)]
    return cleaned or [DEFAULT_TO]


def _format_result(item: Any, index: int) -> str:
    lines = [
        f"{index}. [{getattr(item, 'name', '')}] {getattr(item, 'target', '')}",
        f"   实际值：{getattr(item, 'value', '')}",
        f"   阈值：{getattr(item, 'threshold', '')}",
    ]
    detail = str(getattr(item, "detail", "") or "").strip()
    if detail:
        lines.append(f"   详情：{detail}")
    return "\n".join(lines)


def build_mail(
    results: Iterable[Any],
    config: dict[str, Any],
    out_dir: str | Path,
) -> Path | None:
    """存在 FAIL 时生成 .eml 并返回路径；没有 FAIL 时返回 None。"""
    rows = list(results)
    fails = [item for item in rows if str(getattr(item, "status", "")).upper() == "FAIL"]
    if not fails:
        return None

    warns = [item for item in rows if str(getattr(item, "status", "")).upper() == "WARN"]
    mail_config = config.get("mail") if isinstance(config.get("mail"), dict) else {}
    sender = _header_safe(mail_config.get("from", DEFAULT_FROM)) or DEFAULT_FROM
    recipients = _recipients(mail_config)
    prefix = _header_safe(mail_config.get("subject_prefix", "[巡检告警]"))

    now = datetime.now()
    counts: dict[str, int] = {}
    for item in rows:
        status = str(getattr(item, "status", "")).upper()
        counts[status] = counts.get(status, 0) + 1

    subject = f"{prefix} {now:%Y-%m-%d} 巡检发现 {len(fails)} 个 FAIL"
    summary = (
        f"检查项总数：{len(rows)}\n"
        f"PASS：{counts.get('PASS', 0)}　WARN：{len(warns)}　"
        f"FAIL：{len(fails)}　SKIP：{counts.get('SKIP', 0)}\n"
        f"巡检主机：{socket.gethostname()}\n"
        f"生成时间：{now:%Y-%m-%d %H:%M:%S}"
    )

    fail_body = "\n\n".join(_format_result(item, i) for i, item in enumerate(fails, 1))
    warn_body = (
        "\n\n".join(_format_result(item, i) for i, item in enumerate(warns, 1))
        if warns
        else "（无）"
    )
    body = (
        "服务器自动化巡检发现异常，以下为 dry-run 告警邮件。\n\n"
        f"【巡检摘要】\n{summary}\n\n"
        f"【FAIL 项（{len(fails)}）】\n{fail_body}\n\n"
        f"【WARN 项（{len(warns)}）】\n{warn_body}\n\n"
        "【处理建议】\n"
        "请优先确认 FAIL 项对应的服务、端口或磁盘状态；完整 HTML 报表见 reports/ 目录。\n\n"
        "---\n"
        "本邮件由 ops-check-script 生成。当前为 dry-run 模式，仅写入 outbox/，未真实发送。\n"
    )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="ops-check.local")
    message["X-Ops-Check-Mode"] = "dry-run"
    message.set_content(body, subtype="plain", charset="utf-8")

    destination = Path(out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    eml_path = destination / f"inspect_{now:%Y%m%d_%H%M%S}.eml"

    # ===== DRY-RUN =====
    # 这一行只把邮件序列化到本地文件，不连接 SMTP，不可能误发。
    eml_path.write_bytes(message.as_bytes(policy=SMTP))

    # ===== 接真实 SMTP：删除上面的 eml_path.write_bytes(...) 一行，替换为下面 6 行即可 =====
    # import os
    # import smtplib
    # with smtplib.SMTP(mail_config["smtp_host"], int(mail_config.get("smtp_port", 25)), timeout=15) as smtp:
    #     smtp.starttls()  # 如果服务器要求 TLS
    #     smtp.login(mail_config["smtp_user"], os.environ["SMTP_PASSWORD"])  # 密码不要写进仓库
    #     smtp.send_message(message)  # 发送的就是上面构建好的同一个 EmailMessage
    return eml_path.resolve()
