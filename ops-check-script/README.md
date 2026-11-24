# Python 自动化巡检脚本

一条命令完成磁盘、内存、CPU、端口、进程、关键目录和文件大小巡检，按阈值输出 PASS / WARN / FAIL / SKIP，生成自包含 HTML + CSV 报表，并在出现 FAIL 时生成 dry-run 告警邮件。

## 背景

服务器日常巡检通常靠手工敲一串命令：`df`、`free`、`top`、`netstat`、`ps`、`ls`。重复劳动多、容易漏项，检查结果也很难留档或交给 CI。这个项目把常见巡检项收敛成一份 JSON 配置，一次执行完成全部检查，输出可直接发给同事或面试官看的报表；退出码可以直接接入 CI / crontab / Task Scheduler 告警。

项目只依赖 Python 标准库，**不需要 pip install，不使用 psutil、PyYAML、requests，也不会联网**。

## 巡检项一览

| 检查项 | 判定方式 | 阈值来源 |
| --- | --- | --- |
| 磁盘使用率 | `shutil.disk_usage`；Windows 试探 C:\–Z:\ 已存在的盘符，Linux 检查 `/` | `thresholds.disk_used_pct.warn / fail`，达到阈值即 WARN / FAIL |
| 内存使用率 | Windows 调 `GlobalMemoryStatusEx`；Linux 解析 `/proc/meminfo`（优先 `MemAvailable`） | `thresholds.mem_used_pct.warn / fail` |
| CPU 使用率 | Windows 用 `GetSystemTimes`、Linux 解析 `/proc/stat`；间隔 0.3 秒采样两次算忙时比例 | `thresholds.cpu_used_pct.warn / fail` |
| 端口连通性 | `socket.create_connection(..., timeout=1)`；成功 PASS、拒绝 FAIL、超时 WARN | config 中的 `ports` |
| 进程存活 | Windows 优先 `tasklist /FO CSV /NH`，受限环境回退 Win32 Toolhelp32；Linux 用 `ps -eo comm=` | config 中的 `processes`；匹配数 ≥ 1 PASS，0 FAIL |
| 关键目录 | 目录存在 + `os.access(path, os.W_OK)` | config 中的 `dirs` |
| 关键文件大小 | 普通文件大小对比 MB 阈值 | config 中的 `log_files[].max_mb_warn / max_mb_fail` |

阈值比较统一使用“大于等于”：例如磁盘使用率 `90.3%` 在 `FAIL ≥ 90%` 下判 FAIL，而不是 WARN。

每个检查项独立 `try/except`，单项读不到数据时记录 FAIL 或 SKIP，不会让整轮巡检 traceback。端口没人监听、进程不存在都属于正常的 FAIL 结果，不是脚本异常。

## 怎么跑

要求 Python 3.10+（实测 Python 3.12.14，Windows 11）。没有第三方依赖。

```bash
cd ops-check-script
python src/main.py
```

常用参数：

```bash
python src/main.py --config config.json --out reports
python src/main.py --no-mail          # 有 FAIL 也不生成 .eml
python src/main.py --quiet            # 不打印表格，只输出摘要和报表路径（适合定时任务）
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--config` | `config.json` | 巡检配置文件 |
| `--out` | `reports` | HTML / CSV 输出目录；`.eml` 固定写入仓库根目录的 `outbox/` |
| `--no-mail` | 关闭 | 跳过 dry-run 告警邮件 |
| `--quiet` | 关闭 | 不打印标题和明细表格 |

> 请在仓库根目录执行，或确保工作目录正确：`dirs.path` 和 `log_files.path` 的相对路径按当前工作目录解析，例如本项目的 `.` 和 `config.json` 依赖从仓库根目录启动。

退出码约定：

| 退出码 | 含义 |
| --- | --- |
| `0` | 所有检查为 PASS / WARN / SKIP（没有 FAIL） |
| `1` | 至少一项 FAIL，可直接用于 CI / 定时任务告警 |
| `2` | 参数、配置文件或报表写入错误 |

运行结束时会在终端打印总耗时和报表绝对路径，并在 `reports/` 生成同时间戳的 `.html` 与 `.csv`。

## 报表长什么样

以下为 2026-11-24 在本机 Windows 11 上使用上述 Python 解释器实跑的真实终端输出，原样粘贴（为保留终端表格，长行没有手动换行；本机 G: 盘为 90.3%，达到 FAIL ≥ 90% 所以退出码为 1；两个端口在当前环境超时，因此是 WARN。**下方输出中的本机绝对路径已替换为 <仓库根目录>，其余逐字未改**）：

```text
====================================================================
Python 自动化巡检脚本
====================================================================
配置文件：<仓库根目录>\config.json
输出目录：<仓库根目录>\reports

+------------+--------------------------+----------------+--------------------------+------+-------------------------------------+
| 检查项     | 对象                     | 实际值         | 阈值                     | 状态 | 详情                                |
+------------+--------------------------+----------------+--------------------------+------+-------------------------------------+
| 磁盘使用率 | C:\                      | 39.4%          | WARN ≥ 80% / FAIL ≥ 9… | PASS | 总容量 335.58 GiB，已用 132.23 GiB… |
| 磁盘使用率 | F:\                      | 0.2%           | WARN ≥ 80% / FAIL ≥ 9… | PASS | 总容量 617.22 GiB，已用 1.09 GiB，… |
| 磁盘使用率 | G:\                      | 90.3%          | WARN ≥ 80% / FAIL ≥ 9… | FAIL | 总容量 465.76 GiB，已用 420.47 GiB… |
| 磁盘使用率 | H:\                      | 77.4%          | WARN ≥ 80% / FAIL ≥ 9… | PASS | 总容量 465.76 GiB，已用 360.42 GiB… |
| 磁盘使用率 | I:\                      | 14.2%          | WARN ≥ 80% / FAIL ≥ 9… | PASS | 总容量 111.79 GiB，已用 15.91 GiB… |
| 磁盘使用率 | J:\                      | 9.4%           | WARN ≥ 80% / FAIL ≥ 9… | PASS | 总容量 979.61 GiB，已用 92.45 GiB… |
| 磁盘使用率 | K:\                      | 5.9%           | WARN ≥ 80% / FAIL ≥ 9… | PASS | 总容量 883.40 GiB，已用 52.19 GiB… |
| 内存使用率 | 物理内存 (GlobalMemoryS… | 69.1%          | WARN ≥ 85% / FAIL ≥ 9… | PASS | 总计 15.84 GiB，已用 10.95 GiB，可… |
| CPU 使用率 | CPU 总利用率 (GetSystem… | 32.2%          | WARN ≥ 80% / FAIL ≥ 9… | PASS | 采样间隔 0.30s，忙时 12812500 tick… |
| 端口连通性 | 127.0.0.1:80 (nginx)     | 连接超时（1s） | TCP 可连接；超时=WARN；… | WARN | 1 秒内没有响应，可能是防火墙丢包或… |
| 端口连通性 | 127.0.0.1:3306 (mysql)   | 连接超时（1s） | TCP 可连接；超时=WARN；… | WARN | 1 秒内没有响应，可能是防火墙丢包或… |
| 进程存活   | python / python.exe      | 2 个匹配进程   | 匹配进程数 ≥ 1          | PASS | 命中进程：python.exe × 2。         |
| 关键目录   | . (工作目录)             | 存在且可写     | 目录存在且可写           | PASS | 绝对路径：<仓库根目录>… |
| 关键文件… | config.json              | 0.001 MB       | WARN ≥ 100 MB / FAIL … | PASS | 实际 616 B（616 字节），路径：<仓库根目录>… |
+------------+--------------------------+----------------+--------------------------+------+-------------------------------------+

告警邮件（dry-run）：<仓库根目录>\outbox\inspect_20261124_100055.eml

巡检完成：总计 14 项 | PASS 11 | WARN 2 | FAIL 1 | SKIP 0
总耗时：2.42s
HTML 报表：<仓库根目录>\reports\inspect_20261124_100055.html
CSV 报表：<仓库根目录>\reports\inspect_20261124_100055.csv
退出码：1
```

实际生成的 `reports/inspect_20261124_100055.html` 为 10,752 字节、14 条明细；同时间戳 CSV 为 UTF-8 with BOM，6 列 14 行数据，Excel 直接打开中文不乱码。HTML 是**单文件自包含报表**：CSS 全内联，不引用外部字体、脚本、图片，也不联网；含总数 / PASS / WARN / FAIL / SKIP / 耗时摘要卡片、颜色状态标记、完整阈值与详情，所有动态值都经过 `html.escape`。

HTML 报表打开后即可看到完整界面；界面截图统一放在 `screenshots/`。当前生成环境没有可用的图形浏览器，无法截取真实浏览器画面，因此该目录只保留了 `.gitkeep`，**没有用合成图冒充截图**；在有浏览器的机器上打开 HTML 截图放入即可。

## 告警邮件

只要存在 FAIL，`src/mailer.py` 就会生成一封 `.eml` 到 `outbox/`，文件名形如：

```text
outbox/inspect_20261124_100055.eml
```

主题含日期和 FAIL 数量，例如：

```text
[巡检告警] 2026-11-24 巡检发现 1 个 FAIL
```

正文会列出摘要，以及所有 FAIL 和 WARN 项的实际值、阈值、详情。收件人、发件人和主题前缀在 `config.json` 的 `mail` 配置里：

```json
"mail": {
  "from": "ops-check@example.local",
  "to": ["oncall@example.local"],
  "subject_prefix": "[巡检告警]"
}
```

这是 **dry-run**：脚本只把邮件序列化写到本地，不连接 SMTP，不会误发。可以直接用邮件客户端打开 `.eml` 检查内容。

需要接入真实 SMTP 时，改 `src/mailer.py` 末尾的注释区即可。核心替换只有写入文件那一行：

```python
# 当前（dry-run）：
eml_path.write_bytes(message.as_bytes(policy=SMTP))

# 接真实 SMTP 时替换为：
import os
import smtplib
with smtplib.SMTP(mail_config["smtp_host"], int(mail_config.get("smtp_port", 25)), timeout=15) as smtp:
    smtp.starttls()
    smtp.login(mail_config["smtp_user"], os.environ["SMTP_PASSWORD"])
    smtp.send_message(message)
```

在 `config.json` 的 `mail` 中补充 `smtp_host`、`smtp_port`、`smtp_user` 三个非敏感字段；密码从环境变量 `SMTP_PASSWORD` 读取，不要写进 `config.json`。接 SMTP 时还要确认公司邮件网关允许发信。

FAIL 路径也做过真实验证：临时把 `disk_used_pct.fail` 改为 0，退出码变为 `1` 并生成 `outbox/inspect_20261124_095951.eml`；随后从备份恢复 `config.json`，恢复前后 SHA-256 一致。

## 定时执行

Windows（Task Scheduler，通过 `schtasks` 每天 09:00 执行；下面的路径是本机实际路径，换机器时改仓库和 Python 两处路径）：

```bat
schtasks /Create /SC DAILY /TN "OpsCheck-Daily" /ST 09:00 /TR "cmd /c cd /d <仓库根目录> && C:\Python312\python.exe src\main.py --quiet >> reports\scheduled.log 2>&1" /F
```

- `--quiet` 只保留摘要与路径，日志不会塞满表格。
- 日志追加到 `reports/scheduled.log`；失败时脚本退出码为 1，Task Scheduler 的“上次运行结果”也会显示 1。
- 需要机器未登录也运行时，可在任务计划程序中把该任务改为“不管用户是否登录都要运行”。
- 删除任务：`schtasks /Delete /TN "OpsCheck-Daily" /F`（确认不再需要时再执行）。

Linux（crontab，每天 09:00）：

```cron
0 9 * * * cd /opt/ops-check-script && /usr/bin/python3 src/main.py --quiet >> reports/scheduled.log 2>&1
```

Linux 上建议先手动执行一次，确认 `python3` 路径、`/proc` 可读以及当前用户对目录有写权限。邮件仍是 dry-run，`.eml` 会落在项目的 `outbox/`。

## 技术栈

- **Python 3.10+，零第三方运行时依赖**；标准库用 `shutil`、`socket`、`subprocess`、`ctypes`、`csv`、`email`、`argparse`、`dataclasses`。
- **Windows**：`ctypes` 调 `GlobalMemoryStatusEx`、`GetSystemTimes`；进程列表优先 `tasklist`，在 `tasklist` 被安全策略拒绝时回退到 Toolhelp32 快照（仍是标准库 ctypes）。
- **Linux**：解析 `/proc/meminfo`、`/proc/stat`，进程用 `ps -eo comm=`，磁盘检查 `/`。
- **报表**：手写自包含 HTML + 内联 CSS，`csv` 模块输出 UTF-8 BOM CSV，没有前端构建步骤。
- **邮件**：`email.message.EmailMessage` 生成 UTF-8 `.eml`；dry-run 与真实 SMTP 的边界在 `mailer.py` 中明确注释。

## 局限

- **单机巡检**：没有 agent、中心端、历史数据库或集中上报；多机需要逐台执行或后续接 SSH / Ansible / Prometheus。
- **Windows / Linux 分支没有都实测**：本项目只在 Windows 11 + Python 3.12.14 上真实跑过；Linux 的 `/proc/meminfo`、`/proc/stat`、`ps`、根分区路径是按标准接口实现并做静态检查的，**尚未在真实 Linux 主机上跑过**，上线前需实测。
- **阈值写死在配置里**：没有按业务时段动态调整，也没有趋势和基线；阈值改了必须改配置文件。
- **CPU 是瞬时采样**：0.3 秒只代表采样窗口，短时抖动可能造成误判，不适合作为长期容量指标。
- **端口检查只验证 TCP 可连接**：不检查 HTTP 状态码、TLS 证书、应用层健康；超时判 WARN 是为了区分“被防火墙丢弃”和“明确拒绝”。
- **进程检查依赖系统命令/API**：Linux 的 `ps -eo comm=` 名称可能被截断；Windows 在 `tasklist` 与 Toolhelp32 都失败时会记 SKIP，而不是伪造一个 PASS。
- **告警邮件不重试**：dry-run 只落盘；接真实 SMTP 后还需要自己补重试、去重和发送审计。
- **自动枚举所有盘符**：Windows 会把所有可见的本地盘/挂载盘都纳入检查，外接盘或备份盘可能触发与本机服务无关的 FAIL；如需只查指定盘，可在后续版本加入 `disk_paths` 配置。
- **截图未生成**：如“报表长什么样”一节所述，执行环境没有图形浏览器，`screenshots/` 目前只有 `.gitkeep`，没有伪造截图。
