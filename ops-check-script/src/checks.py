"""巡检项实现：磁盘、内存、CPU、端口、进程、目录与大文件。

设计原则：
* 只用标准库（以及 config 驱动），不依赖 psutil；
* 每个单项检查单独捕获异常，任何一项失败都不会中断整轮巡检；
* Windows 与 Linux 分别使用系统原生接口。
"""

from __future__ import annotations

import csv
import ctypes
import errno
import io
import os
import shutil
import socket
import string
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

STATUSES = ("PASS", "WARN", "FAIL", "SKIP")


@dataclass
class CheckResult:
    """统一检查结果，供终端、HTML、CSV 和邮件复用。"""

    name: str
    target: str
    value: str
    threshold: str
    status: str
    detail: str = ""

    def __post_init__(self) -> None:
        self.status = str(self.status).upper()
        if self.status not in STATUSES:
            self.status = "SKIP"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _threshold_pair(
    config: dict[str, Any],
    key: str,
    default_warn: float,
    default_fail: float,
) -> tuple[float, float]:
    """从 config["thresholds"][key] 读取阈值，缺失或非法时回退默认值。"""
    raw = (config.get("thresholds") or {}).get(key) or {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        warn = float(raw.get("warn", default_warn))
        fail = float(raw.get("fail", default_fail))
    except (TypeError, ValueError):
        warn, fail = float(default_warn), float(default_fail)
    return warn, fail


def _level(value: float, warn: float, fail: float) -> str:
    if value >= fail:
        return "FAIL"
    if value >= warn:
        return "WARN"
    return "PASS"


def _threshold_text(warn: float, fail: float, suffix: str = "%") -> str:
    return f"WARN ≥ {warn:g}{suffix} / FAIL ≥ {fail:g}{suffix}"


def _human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def _gib(size_bytes: int) -> str:
    return f"{size_bytes / (1024 ** 3):.2f} GiB"


def _crash_result(name: str, target: str, exc: BaseException) -> CheckResult:
    """把未预期的单项异常变成一条可见的 FAIL，而不是 traceback。"""
    return CheckResult(
        name=name,
        target=target,
        value="检查异常",
        threshold="—",
        status="FAIL",
        detail=f"{type(exc).__name__}: {exc}",
    )


# ---------------------------------------------------------------------------
# 1. 磁盘
# ---------------------------------------------------------------------------


def _disk_roots() -> list[str]:
    """Windows 试探 C:\\..Z:\\，Linux 只看根分区。"""
    if os.name == "nt":
        roots: list[str] = []
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            try:
                if os.path.exists(root):
                    roots.append(root)
            except OSError:
                # 某些空光驱 / 断开的网络盘在探测时会抛异常，跳过即可。
                continue
        return roots
    return ["/"]


def check_disks(config: dict[str, Any]) -> list[CheckResult]:
    warn, fail = _threshold_pair(config, "disk_used_pct", 80, 90)
    threshold = _threshold_text(warn, fail)
    results: list[CheckResult] = []

    try:
        roots = _disk_roots()
    except Exception as exc:  # 极端情况下盘符枚举失败
        return [_crash_result("磁盘使用率", "盘符枚举", exc)]

    if not roots:
        return [
            CheckResult(
                "磁盘使用率",
                "未发现可检查的磁盘",
                "—",
                threshold,
                "SKIP",
                "盘符枚举结果为空。",
            )
        ]

    for root in roots:
        try:
            usage = shutil.disk_usage(root)
            if usage.total <= 0:
                results.append(
                    CheckResult(
                        "磁盘使用率", root, "0 字节", threshold, "SKIP", "磁盘总容量为 0。"
                    )
                )
                continue
            used_pct = (usage.total - usage.free) / usage.total * 100
            results.append(
                CheckResult(
                    name="磁盘使用率",
                    target=root,
                    value=f"{used_pct:.1f}%",
                    threshold=threshold,
                    status=_level(used_pct, warn, fail),
                    detail=(
                        f"总容量 {_gib(usage.total)}，已用 {_gib(usage.used)}，"
                        f"可用 {_gib(usage.free)}"
                    ),
                )
            )
        except Exception as exc:
            # 单个不可访问的盘符不影响其他盘符。
            results.append(
                CheckResult(
                    "磁盘使用率",
                    root,
                    "读取失败",
                    threshold,
                    "SKIP",
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return results


# ---------------------------------------------------------------------------
# 2. 内存
# ---------------------------------------------------------------------------


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _windows_memory() -> tuple[int, int]:
    status = _MemoryStatusEx()
    status.dwLength = ctypes.sizeof(_MemoryStatusEx)
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx 调用失败")
    total = int(status.ullTotalPhys)
    available = int(status.ullAvailPhys)
    if total <= 0:
        raise OSError("GlobalMemoryStatusEx 返回的总内存为 0")
    return total, available


def _linux_memory() -> tuple[int, int]:
    values: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as handle:
        for line in handle:
            key, _, rest = line.partition(":")
            fields = rest.split()
            if fields:
                try:
                    values[key.strip()] = int(fields[0]) * 1024
                except ValueError:
                    continue
    total = values.get("MemTotal", 0)
    if total <= 0:
        raise OSError("/proc/meminfo 中没有有效的 MemTotal")
    available = values.get("MemAvailable")
    if available is None:
        available = (
            values.get("MemFree", 0)
            + values.get("Buffers", 0)
            + values.get("Cached", 0)
        )
    return total, max(0, min(total, available))


def check_memory(config: dict[str, Any]) -> list[CheckResult]:
    warn, fail = _threshold_pair(config, "mem_used_pct", 85, 95)
    threshold = _threshold_text(warn, fail)
    try:
        if os.name == "nt":
            total, available = _windows_memory()
            target = "物理内存 (GlobalMemoryStatusEx)"
        elif os.path.exists("/proc/meminfo"):
            total, available = _linux_memory()
            target = "物理内存 (/proc/meminfo)"
        else:
            return [
                CheckResult(
                    "内存使用率",
                    "物理内存",
                    "无法读取",
                    threshold,
                    "SKIP",
                    "当前系统既不是 Windows，也没有 /proc/meminfo。",
                )
            ]

        used = total - available
        used_pct = used / total * 100
        return [
            CheckResult(
                name="内存使用率",
                target=target,
                value=f"{used_pct:.1f}%",
                threshold=threshold,
                status=_level(used_pct, warn, fail),
                detail=f"总计 {_gib(total)}，已用 {_gib(used)}，可用 {_gib(available)}",
            )
        ]
    except Exception as exc:
        return [
            CheckResult(
                "内存使用率",
                "物理内存",
                "读取失败",
                threshold,
                "SKIP",
                f"无法采集内存数据：{type(exc).__name__}: {exc}",
            )
        ]


# ---------------------------------------------------------------------------
# 3. CPU（两次采样，间隔 0.3 秒）
# ---------------------------------------------------------------------------

CPU_SAMPLE_SECONDS = 0.3


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_ulong),
        ("dwHighDateTime", ctypes.c_ulong),
    ]


def _filetime_to_int(value: _FileTime) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _windows_cpu_sample() -> tuple[int, int]:
    """返回 (idle, total)；Windows 的 kernel time 已包含 idle time。"""
    idle = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    if not kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    ):
        raise OSError(ctypes.get_last_error(), "GetSystemTimes 调用失败")
    idle_ticks = _filetime_to_int(idle)
    total_ticks = _filetime_to_int(kernel) + _filetime_to_int(user)
    return idle_ticks, total_ticks


def _linux_cpu_sample() -> tuple[int, int]:
    with open("/proc/stat", encoding="utf-8") as handle:
        line = handle.readline()
    fields = line.split()
    if not fields or fields[0] != "cpu":
        raise OSError("/proc/stat 第一行不是 cpu 汇总行")
    values = [int(item) for item in fields[1:9]]
    # user nice system idle iowait irq softirq steal；缺项补 0。
    values += [0] * (8 - len(values))
    idle = values[3] + values[4]
    total = sum(values)
    return idle, total


def check_cpu(config: dict[str, Any]) -> list[CheckResult]:
    warn, fail = _threshold_pair(config, "cpu_used_pct", 80, 95)
    threshold = _threshold_text(warn, fail)
    try:
        if os.name == "nt":
            sampler = _windows_cpu_sample
            target = "CPU 总利用率 (GetSystemTimes)"
        elif os.path.exists("/proc/stat"):
            sampler = _linux_cpu_sample
            target = "CPU 总利用率 (/proc/stat)"
        else:
            return [
                CheckResult(
                    "CPU 使用率",
                    "CPU",
                    "无法采样",
                    threshold,
                    "SKIP",
                    "当前系统没有可用的 CPU 时间采样接口。",
                )
            ]

        idle_start, total_start = sampler()
        sampled_at = time.perf_counter()
        time.sleep(CPU_SAMPLE_SECONDS)
        idle_end, total_end = sampler()
        interval = time.perf_counter() - sampled_at

        idle_delta = idle_end - idle_start
        total_delta = total_end - total_start
        if total_delta <= 0 or idle_delta < 0:
            return [
                CheckResult(
                    "CPU 使用率",
                    target,
                    "采样无增量",
                    threshold,
                    "SKIP",
                    "两次采样的 CPU 时间没有增长，无法计算利用率。",
                )
            ]

        busy_delta = max(0, total_delta - idle_delta)
        used_pct = min(100.0, busy_delta / total_delta * 100)
        return [
            CheckResult(
                name="CPU 使用率",
                target=target,
                value=f"{used_pct:.1f}%",
                threshold=threshold,
                status=_level(used_pct, warn, fail),
                detail=f"采样间隔 {interval:.2f}s，忙时 {busy_delta} ticks / 总增量 {total_delta} ticks。",
            )
        ]
    except Exception as exc:
        return [
            CheckResult(
                "CPU 使用率",
                "CPU",
                "采样失败",
                threshold,
                "SKIP",
                f"无法计算 CPU 使用率：{type(exc).__name__}: {exc}",
            )
        ]


# ---------------------------------------------------------------------------
# 4. 端口
# ---------------------------------------------------------------------------

PORT_THRESHOLD = "TCP 可连接；超时=WARN；拒绝=FAIL"


def check_ports(config: dict[str, Any]) -> list[CheckResult]:
    entries = config.get("ports") or []
    results: list[CheckResult] = []
    if not isinstance(entries, list):
        return [
            CheckResult(
                "端口连通性", "ports", "配置格式错误", PORT_THRESHOLD, "FAIL", "ports 必须是数组。"
            )
        ]

    for index, entry in enumerate(entries):
        try:
            if not isinstance(entry, dict):
                raise ValueError("端口配置项必须是包含 host/port 的对象")
            host = str(entry.get("host", "127.0.0.1")).strip() or "127.0.0.1"
            port = int(entry["port"])
            desc = str(entry.get("desc", "")).strip()
            if not 0 < port < 65536:
                raise ValueError(f"非法端口号：{port}")
            target = f"{host}:{port}" + (f" ({desc})" if desc else "")
        except Exception as exc:
            results.append(
                CheckResult(
                    "端口连通性",
                    f"ports[{index}]",
                    "配置错误",
                    PORT_THRESHOLD,
                    "FAIL",
                    f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=1.0):
                elapsed_ms = (time.perf_counter() - started) * 1000
            results.append(
                CheckResult(
                    "端口连通性",
                    target,
                    f"TCP 可达（{elapsed_ms:.0f} ms）",
                    PORT_THRESHOLD,
                    "PASS",
                    f"{desc or host} 端口监听正常。" if desc else "TCP 连接建立成功。",
                )
            )
        except (socket.timeout, TimeoutError):
            results.append(
                CheckResult(
                    "端口连通性",
                    target,
                    "连接超时（1s）",
                    PORT_THRESHOLD,
                    "WARN",
                    "1 秒内没有响应，可能是防火墙丢包或服务无响应。",
                )
            )
        except ConnectionRefusedError as exc:
            results.append(
                CheckResult(
                    "端口连通性",
                    target,
                    "连接被拒绝",
                    PORT_THRESHOLD,
                    "FAIL",
                    f"端口未监听：{exc}",
                )
            )
        except socket.gaierror as exc:
            results.append(
                CheckResult(
                    "端口连通性",
                    target,
                    "主机解析失败",
                    PORT_THRESHOLD,
                    "FAIL",
                    f"无法解析主机 {host}：{exc}",
                )
            )
        except OSError as exc:
            refused = getattr(exc, "winerror", None) == 10061 or exc.errno in {
                errno.ECONNREFUSED,
                10061,
            }
            status = "FAIL" if refused else "WARN"
            value = "连接被拒绝" if refused else "连接异常"
            results.append(
                CheckResult(
                    "端口连通性",
                    target,
                    value,
                    PORT_THRESHOLD,
                    status,
                    f"{type(exc).__name__}: {exc}",
                )
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    "端口连通性",
                    target,
                    "检查异常",
                    PORT_THRESHOLD,
                    "FAIL",
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return results


# ---------------------------------------------------------------------------
# 5. 进程
# ---------------------------------------------------------------------------


def _decode_console(raw: bytes) -> str:
    encodings = ["utf-8"]
    if os.name == "nt":
        encodings.extend(["mbcs", "gbk"])
    encodings.append("latin-1")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _windows_tasklist_processes() -> list[str]:
    completed = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        error = _decode_console(completed.stderr).strip()
        raise RuntimeError(f"tasklist 退出码 {completed.returncode}: {error}")
    text = _decode_console(completed.stdout)
    names: list[str] = []
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        name = row[0].strip()
        if name and not name.upper().startswith("INFO:"):
            names.append(name)
    return names


def _windows_snapshot_processes() -> list[str]:
    """tasklist 被安全策略拒绝时的只读兜底：Toolhelp32 快照。

    仍然只使用标准库 ctypes，不依赖 psutil。任何调用失败都会向上抛出，
    由 check_processes 记录为 SKIP，而不是中断整轮巡检。
    """
    from ctypes import wintypes

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    ]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    ]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    TH32CS_SNAPPROCESS = 0x00000002
    ERROR_NO_MORE_FILES = 18
    invalid_handle = wintypes.HANDLE(-1).value
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or int(snapshot) == int(invalid_handle):
        raise OSError(
            ctypes.get_last_error(), "CreateToolhelp32Snapshot 调用失败"
        )

    names: list[str] = []
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            error = ctypes.get_last_error()
            if error == ERROR_NO_MORE_FILES:
                return []
            raise OSError(error, "Process32FirstW 调用失败")
        while True:
            name = str(entry.szExeFile).strip()
            if name:
                names.append(name)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return names


def _list_process_names() -> list[str]:
    """Windows 优先 tasklist CSV，受限环境回退 Toolhelp；Linux 用 ps -eo comm=。"""
    if os.name == "nt":
        errors: list[str] = []
        try:
            names = _windows_tasklist_processes()
            if names:
                return names
            errors.append("tasklist 返回空列表")
        except Exception as exc:
            errors.append(f"tasklist: {type(exc).__name__}: {exc}")
        try:
            names = _windows_snapshot_processes()
            if names:
                return names
            errors.append("Toolhelp32 返回空列表")
        except Exception as exc:
            errors.append(f"Toolhelp32: {type(exc).__name__}: {exc}")
        raise RuntimeError("；".join(errors))

    completed = subprocess.run(
        ["ps", "-eo", "comm="],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        error = _decode_console(completed.stderr).strip()
        raise RuntimeError(f"ps 退出码 {completed.returncode}: {error}")
    return [
        line.strip()
        for line in _decode_console(completed.stdout).splitlines()
        if line.strip()
    ]


def _expected_process_names(names: list[str]) -> set[str]:
    expected: set[str] = set()
    for raw in names:
        name = str(raw).strip().casefold()
        if not name:
            continue
        expected.add(name)
        if os.name == "nt" and not name.endswith(".exe"):
            expected.add(f"{name}.exe")
        if os.name != "nt" and name.endswith(".exe"):
            expected.add(name[:-4])
    return expected


def check_processes(config: dict[str, Any]) -> list[CheckResult]:
    entries = config.get("processes") or []
    if not isinstance(entries, list):
        return [
            CheckResult(
                "进程存活",
                "processes",
                "配置格式错误",
                "至少 1 个匹配进程",
                "FAIL",
                "processes 必须是数组。",
            )
        ]
    names = [str(item).strip() for item in entries if str(item).strip()]
    if not names:
        return []

    target = " / ".join(names)
    threshold = "匹配进程数 ≥ 1"
    try:
        running = _list_process_names()
        expected = _expected_process_names(names)
        matched = Counter(
            name for name in running if name.strip().casefold() in expected
        )
        count = sum(matched.values())
        status = "PASS" if count > 0 else "FAIL"
        if count:
            breakdown = "，".join(f"{name} × {num}" for name, num in matched.items())
            detail = f"命中进程：{breakdown}。"
        else:
            detail = f"进程列表中未找到 {target}。"
        return [
            CheckResult(
                "进程存活",
                target,
                f"{count} 个匹配进程",
                threshold,
                status,
                detail,
            )
        ]
    except Exception as exc:
        return [
            CheckResult(
                "进程存活",
                target,
                "进程列表读取失败",
                threshold,
                "SKIP",
                f"{type(exc).__name__}: {exc}",
            )
        ]


# ---------------------------------------------------------------------------
# 6. 目录
# ---------------------------------------------------------------------------


def check_dirs(config: dict[str, Any]) -> list[CheckResult]:
    entries = config.get("dirs") or []
    results: list[CheckResult] = []
    threshold = "目录存在且可写"
    if not isinstance(entries, list):
        return [
            CheckResult(
                "关键目录", "dirs", "配置格式错误", threshold, "FAIL", "dirs 必须是数组。"
            )
        ]

    for index, entry in enumerate(entries):
        try:
            if isinstance(entry, str):
                path_text, desc = entry, ""
            elif isinstance(entry, dict):
                path_text = str(entry.get("path", "")).strip()
                desc = str(entry.get("desc", "")).strip()
            else:
                raise ValueError("目录配置项必须是字符串或对象")
            if not path_text:
                raise ValueError("目录 path 不能为空")
            target = path_text + (f" ({desc})" if desc else "")
            path = Path(path_text).expanduser()
            absolute = path.resolve(strict=False)

            if not path.exists():
                results.append(
                    CheckResult(
                        "关键目录",
                        target,
                        "不存在",
                        threshold,
                        "FAIL",
                        f"路径不存在：{absolute}",
                    )
                )
            elif not path.is_dir():
                results.append(
                    CheckResult(
                        "关键目录",
                        target,
                        "不是目录",
                        threshold,
                        "FAIL",
                        f"该路径是文件而不是目录：{absolute}",
                    )
                )
            else:
                writable = os.access(str(path), os.W_OK)
                results.append(
                    CheckResult(
                        "关键目录",
                        target,
                        "存在且可写" if writable else "存在但不可写",
                        threshold,
                        "PASS" if writable else "FAIL",
                        f"绝对路径：{absolute}",
                    )
                )
        except Exception as exc:
            results.append(
                CheckResult(
                    "关键目录",
                    f"dirs[{index}]",
                    "检查异常",
                    threshold,
                    "FAIL",
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return results


# ---------------------------------------------------------------------------
# 7. 关键文件大小
# ---------------------------------------------------------------------------


def check_log_files(config: dict[str, Any]) -> list[CheckResult]:
    entries = config.get("log_files") or []
    results: list[CheckResult] = []
    if not isinstance(entries, list):
        return [
            CheckResult(
                "关键文件大小",
                "log_files",
                "配置格式错误",
                "—",
                "FAIL",
                "log_files 必须是数组。",
            )
        ]

    for index, entry in enumerate(entries):
        try:
            if isinstance(entry, str):
                path_text = entry
                desc = ""
                warn_mb, fail_mb = 100.0, 500.0
            elif isinstance(entry, dict):
                path_text = str(entry.get("path", "")).strip()
                desc = str(entry.get("desc", "")).strip()
                warn_mb = float(entry.get("max_mb_warn", 100))
                fail_mb = float(entry.get("max_mb_fail", 500))
            else:
                raise ValueError("文件配置项必须是字符串或对象")
            if not path_text:
                raise ValueError("文件 path 不能为空")
            if warn_mb < 0 or fail_mb < 0:
                raise ValueError("max_mb_warn / max_mb_fail 不能为负数")

            target = path_text + (f" ({desc})" if desc else "")
            threshold = _threshold_text(warn_mb, fail_mb, " MB")
            path = Path(path_text).expanduser()
            absolute = path.resolve(strict=False)
            if not path.exists():
                results.append(
                    CheckResult(
                        "关键文件大小",
                        target,
                        "文件不存在",
                        threshold,
                        "FAIL",
                        f"路径不存在：{absolute}",
                    )
                )
                continue
            if not path.is_file():
                results.append(
                    CheckResult(
                        "关键文件大小",
                        target,
                        "不是普通文件",
                        threshold,
                        "FAIL",
                        f"该路径不是普通文件：{absolute}",
                    )
                )
                continue

            size = path.stat().st_size
            size_mb = size / (1024 * 1024)
            results.append(
                CheckResult(
                    "关键文件大小",
                    target,
                    f"{size_mb:.3f} MB",
                    threshold,
                    _level(size_mb, warn_mb, fail_mb),
                    f"实际 {_human_size(size)}（{size:,} 字节），路径：{absolute}",
                )
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    "关键文件大小",
                    f"log_files[{index}]",
                    "检查异常",
                    "—",
                    "FAIL",
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return results


# ---------------------------------------------------------------------------
# 汇总入口
# ---------------------------------------------------------------------------


def run_all_checks(config: dict[str, Any]) -> list[CheckResult]:
    """按固定顺序执行全部巡检项；任一模块异常都只影响自己的结果。"""
    runners = [
        ("磁盘", check_disks),
        ("内存", check_memory),
        ("CPU", check_cpu),
        ("端口", check_ports),
        ("进程", check_processes),
        ("目录", check_dirs),
        ("文件大小", check_log_files),
    ]
    results: list[CheckResult] = []
    for label, runner in runners:
        try:
            results.extend(runner(config))
        except Exception as exc:
            results.append(_crash_result(f"{label}巡检", label, exc))
    return results


def summarize(results: list[CheckResult]) -> dict[str, int]:
    counts = Counter(result.status for result in results)
    return {
        "total": len(results),
        "PASS": counts.get("PASS", 0),
        "WARN": counts.get("WARN", 0),
        "FAIL": counts.get("FAIL", 0),
        "SKIP": counts.get("SKIP", 0),
    }
