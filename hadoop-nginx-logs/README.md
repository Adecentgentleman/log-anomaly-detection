# Hadoop Streaming 网站日志分析

用 Hadoop Streaming（Python mapper / reducer）对 nginx 访问日志做两个统计作业——**每小时 PV / UV** 和 **Top 10 热门 URL**；同一套脚本既能直接提交到 Hadoop 集群，也能在没有集群的本机上用 `cat | mapper | sort | reducer` 完整跑通。

---

## 背景

网站每天产生的 `access.log` 一旦到 GB 级，单机 `awk` / `grep` 就越来越吃力，典型解法是 MapReduce：把「按行解析」摊到很多台机器上（map），再由框架把相同 key 的数据聚到一起做汇总（reduce）。

这个项目想解决的问题是：**很多 Hadoop 简历项目的代码根本跑不起来**——要么依赖一个不存在的集群，要么只能贴一段截图。所以这里做了两件事：

1. 用最轻的 Hadoop Streaming + Python 实现，不用编译 Java、不用装 pyspark / mrjob；
2. **逻辑只有一份**：mapper / reducer 只认 stdin/stdout，本地跑的和提交到集群的是同样两个文件，唯一区别是「谁来做 shuffle 排序」——集群里是 Hadoop，本机是 `run_local.py` 里的一次 `sorted()`。

这样面试官不用装 Hadoop，`python run_local.py` 就能看到结果表。

## 数据

`data/sample_access.log`：**2000 行合成日志**，不是真实生产日志。

| 项 | 说明 |
|---|---|
| 生成方式 | `src/gen_fake_logs.py`，随机种子固定为 `20261124`，同一份代码每次生成的内容逐字节一致 |
| 完整性校验 | 文件 sha256 `2a396fb186e624f80ce1687c9df28c42aabf7f1d28abe932cb78ac55cee7537e` |
| IP | 42 个不同 IP，全部落在 `10.0.0.0/8`（RFC 1918 私网）、`192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24`（RFC 5737 文档示例地址） |
| 域名 | 只用 `example.com`，路径是虚构业务路径（`/product/detail`、`/api/v1/orders`、`/static/app.js` …） |
| 时间 | 全部在 `2026-11-24` 一天内，24 个小时都有数据；09:00–18:00 是高峰，凌晨很低，且按时间升序排列（和真实 access.log 一致） |
| URL | 74 个不同 URL，请求量服从齐夫（Zipf）分布：少数 URL 占大头，剩下是 `/product/detail/<id>` 这类长尾 |
| 状态码 | `200:1895  304:38  404:53  500:14` |

样例行（nginx combined 格式的子集）：

```text
10.12.3.44 - - [24/Nov/2026:09:12:33 +0800] "GET /api/v1/orders HTTP/1.1" 200 532 "-" "curl/8.4.0"
```

> 合规说明：仓库里不包含任何真实服务器日志、真实公网 IP、内网拓扑或公司域名。`10.0.0.0/8` 是 RFC 1918 私网段，另外三段是 RFC 5737 专门留给文档示例的地址，都不指向任何真实主机。想换一份数据：`python src/gen_fake_logs.py --lines 5000 --seed 7`。

## 作业设计

两个作业都遵守 Hadoop Streaming 的约定：**从 stdin 按行读、按行写 stdout、字段用 `\t` 分隔**，每行第一个 `\t` 左边是 key、右边是 value。

### 作业一：每小时 PV / UV

| | |
|---|---|
| 输入 | 原始 access.log（每行一条） |
| map 输出 | `2026-11-24 09\t1\t10.12.3.44` — key 是小时，value 是 `1` + TAB + 客户端 IP |
| shuffle | 按 key（小时）排序并分区，同一小时的记录进同一个 reducer |
| reduce 输出 | `2026-11-24 09\t137\t29` — `hour<TAB>PV<TAB>UV` |

**mapper**（`src/mapper_pv_uv.py`）

1. 用一条正则从 `[24/Nov/2026:09:12:33 +0800]` 里取出日期和时间，转成 `2026-11-24 09`；
2. 校验 IP 是否为合法点分十进制、月/日/时/分/秒是否越界、状态码是否 100–599；
3. 输出 `小时 \t 1 \t IP`。

为什么 value 要带一个 IP，而不是只输出 `小时 \t 1`？因为 PV 和 UV 需要的信息不同：PV 只数条数，UV 要 `distinct IP`。把 `1` 和 IP 塞进同一个 value，**一次 map 就能同时供两个指标使用**，不用再起第二个作业。`1` 放在 value 的第一位，是为了让 reducer 解析简单，也方便将来加 combiner。

非法行（截断、乱码、IP 越界、时间戳坏了）**静默跳过并计数到 stderr**，绝不抛异常——一行脏数据不该让整个作业挂掉。样例数据里没有非法行，`skipped=0`；用混了脏行的文件测过：9 行输入里 2 行合法、7 行被跳过，reducer 只收到 2 条（见文末「已验证」）。

**reducer**（`src/reducer_pv_uv.py`）

Streaming 保证**同一个 key 的记录连续到达**，所以 reducer 只需要在内存里保留「当前小时」的状态：

- 遇到新 key：输出上一个小结，清空累加器；
- 同一 key：`PV += 1`，IP 塞进 `set`，最后 `UV = len(set)`。

峰值内存只有「一个小时的去重 IP 数」，和一天有多少条日志无关——就算一天几十亿条也不会 OOM。

### 作业二：Top 10 热门 URL

| | |
|---|---|
| 输入 | 原始 access.log |
| map 输出 | `/api/v1/orders\t1` — key 是 URL 路径，value 固定为 1 |
| shuffle | 按 URL 排序，相同 URL 进同一个 reducer |
| reduce 输出 | `/api/v1/orders\t428` — `url<TAB>count`，count 从大到小，只输出前 10 行 |

**mapper**（`src/mapper_top_url.py`）：正则解析出请求路径，**丢掉 `?query=string`**，输出 `URL\t1`。丢 query 是有意的：真实日志里 query 取值几乎无限，带上它「热门 URL」会退化成一堆只出现一次的键。想按完整 URL 统计，把文件里的 `PARSE_FULL_URL` 改成 `True`。这个 mapper 复用 `mapper_pv_uv.py` 里的正则（`from mapper_pv_uv import LOG_RE`），避免同一份日志格式写两遍解析逻辑——所以提交作业二时 `-files` 必须把 `mapper_pv_uv.py` 一起带上。

**reducer**（`src/reducer_top_url.py`）：流式累加所有 URL 的计数到一个 dict，然后用 `heapq.nlargest(10, ...)` 取 Top 10（O(n log k)），输出前按 `(-count, url)` 排序，保证并列时顺序稳定、每次跑出来的结果完全一致。

**这里的取舍必须讲清楚**：Top-N 是**全局聚合**，和「分片 + 流式」天生冲突。

- 本实现选了最直接的方案：**单 reducer、全量内存累加**。2000 行 / 74 个 URL 毫无压力，代码也最短；
- 代价是：真实场景下如果 URL 有上亿个（长尾），单个 reducer 的堆内存会爆；而且所有数据挤到一个 reduce task 上，并行度退化成 1，shuffle 也会成为瓶颈。所以 `run_hadoop.sh` 里作业二必须写 `-D mapreduce.job.reduces=1`——开 3 个 reducer 只会得到「每个 reducer 各自的前 10」，不是全局 Top 10。

真实数据量下正确的做法（按代价排序）：

1. **两阶段作业**：作业 A 先按 URL 全量聚合成 `url\tcount`（hash 分区，可并行），作业 B 再对小得多的结果集求 Top-N；
2. **每个 reducer 先算局部 Top-N，再用一个作业合并**（map 端加 combiner 预聚合，shuffle 数据量能降一到两个数量级）；
3. 用 `TotalOrderPartitioner` + 采样做全局排序，直接取头 N 条；
4. 换 Spark / Flink 的 `topK`，让框架做局部预聚合。

## 本地怎么跑（不需要 Hadoop 集群）

**前置**：只要 Python 3.8+，**不需要 Hadoop，也不需要任何第三方库**（标准库足够，numpy / pandas 都没用上）。

```bash
# 1)（可选）重新生成样例日志，固定种子，结果与仓库里的一致
python src/gen_fake_logs.py

# 2) 跑完整流水线：两个作业依次执行，写 results/*.csv
python run_local.py
```

`run_local.py` 干的事，就是集群里 shuffle 干的事：

```text
cat data/sample_access.log
  | python src/mapper_pv_uv.py      # map
  | sorted(..., key=每行第一个 TAB 前的部分)   # shuffle：按 key 排序
  | python src/reducer_pv_uv.py     # reduce
  > results/pv_hourly.csv
```

> 为什么不用系统的 `sort` 命令：Windows 上没有 coreutils 的 `sort`；PowerShell 的 `Sort-Object` 按区域设置和大小写规则排序，和 Hadoop 的按字节排序并不等价，中文或大小写混排时结果会不一样。所以中间结果在 Python 里排序（`sorted` 是稳定排序，同 key 的记录保持原顺序），三个平台上结果一致。

实际运行输出（本机 Windows 11 + Python 3.12.14，真实输出；只把解释器绝对路径缩写成了 `C:\...`，其余逐字照抄）：

```text
========================================================================
Hadoop Streaming 本地模拟（不需要集群，subprocess 串管道）
========================================================================
  python      C:\...\python.exe
  输入日志    data\sample_access.log
  日志行数    2000
  计时口径    每个 mapper/reducer 都是一次独立进程，耗时含解释器启动（Windows 上受杀毒软件影响波动较大）

========================================================================
作业 1/2   每小时 PV / UV
========================================================================
  [1/5] 读日志        2000 行                             0.2 ms
  [2/5] map           2000 条中间记录                   111.2 ms   | [mapper_pv_uv] read=2000 emitted=2000 skipped=0
  [3/5] shuffle/sort  2000 条 -> 24 个 key                0.2 ms
  [4/5] reduce        24 行                              38.0 ms   | [reducer_pv_uv] hours=24 total_pv=2000 bad_records=0
  [5/5] 写 CSV        results\pv_hourly.csv               0.9 ms

  结果前 5 行:
    hour,pv,uv
    2026-11-24 00,10,8
    2026-11-24 01,7,5
    2026-11-24 02,9,7
    2026-11-24 03,5,5
    2026-11-24 04,8,8
  [OK] PV 合计 2000 == mapper 有效行数 2000
  [OK] 每一行都满足 1 <= UV <= PV
  [OK] 输出 24 行 == shuffle 后的 24 个 key

========================================================================
作业 2/2   Top 10 热门 URL
========================================================================
  [1/4] map           2000 条中间记录                   105.3 ms   | [mapper_top_url] read=2000 emitted=2000 skipped=0
  [2/4] shuffle/sort  2000 条                             0.5 ms
  [3/4] reduce        10 行                              39.9 ms   | [reducer_top_url] distinct_urls=74 total_requests=2000 emitted=10 bad_records=0
  [4/4] 写 CSV        results\top_url.csv                 0.7 ms

  结果前 5 行:
    url,count
    /api/v1/orders,428
    /product/list,212
    /api/v1/products,121
    /static/app.js,104
    /,73
  [OK] 输出正好 10 行（Top 10）
  [OK] count 从大到小排列
  [OK] Top 10 命中的请求数 1197 <= 总请求数 2000

========================================================================
汇总
========================================================================
  PV 合计            2000（= 合法日志行数）
  有数据的小时数     24
  Top1 URL           /api/v1/orders（428 次，占全部请求 59.9%）
  结果文件           results/pv_hourly.csv, results/top_url.csv
  总耗时             299.4 ms
  全部自检通过，退出码 0
```

`run_local.py` 会做自检（两个作业各 3 条，任何一项不通过就以退出码 1 结束，防止「跑完但结果是错的」）：

1. 作业一：PV 合计 == mapper 报的 `emitted`（有效行数）；
2. 作业一：每行 `1 <= UV <= PV`；
3. 作业一：reduce 输出行数 == shuffle 后的 key 数量；
4. 作业二：正好 10 行（Top 10）、count 单调不增、Top 10 合计不超过总请求数。

也可以单独测 mapper（在 cmd / bash 里用重定向；PowerShell 不支持 `<`，见下）：

```bash
python src/mapper_pv_uv.py < data/sample_access.log | head -5
```

```text
2026-11-24 00	1	10.211.88.173
2026-11-24 00	1	10.244.1.12
2026-11-24 00	1	192.0.2.218
2026-11-24 00	1	10.211.88.173
2026-11-24 00	1	10.173.70.225
```

> Windows PowerShell 里 `<` 不是重定向，等价写法：`Get-Content data/sample_access.log | python src/mapper_pv_uv.py | Select-Object -First 5`。

## 集群怎么跑

`run_hadoop.sh` 是可直接提交的 Streaming 脚本（`set -euo pipefail`，变量集中在开头）。

**需要改的 3 个地方**（脚本里用 ★ 标出）：

| # | 变量 | 改成什么 |
|---|---|---|
| ★1 | `INPUT_DIR` | 日志上传到哪个 HDFS 目录，例如 `/user/hadoop/nginx-logs/input` |
| ★2 | `OUTPUT_DIR` | 两个作业的输出根目录，作业会写到 `$OUTPUT_DIR/pv_uv`、`$OUTPUT_DIR/top_url` |
| ★3 | `PYTHON_BIN` | 集群里的解释器，例如 `python3`、`/usr/bin/python3` 或某个 conda 环境的绝对路径 |

另外两个集群环境的坑（脚本注释里也写了）：

- **所有 NodeManager 上都要有同一个 Python**——Streaming 是在数据所在的节点上起进程执行 `mapper.py`，不是在一台机器上跑；
- 如果你不用 `python3 mapper.py` 而是直接 `-mapper mapper.py`，脚本得有可执行位和 shebang：`chmod +x src/*.py`。

跑起来：

```bash
export HADOOP_HOME=/opt/hadoop-3.3.6
chmod +x run_hadoop.sh
./run_hadoop.sh
```

核心提交命令长这样（作业一），作业二只改 `-mapper/-reducer/-output` 并把 `reduces` 设成 1：

```bash
hadoop jar "$STREAMING_JAR" \
  -D mapreduce.job.name="nginx-pv-uv" \
  -D mapreduce.job.reduces=3 \
  -files "src/mapper_pv_uv.py,src/reducer_pv_uv.py" \
  -mapper "python3 mapper_pv_uv.py" \
  -reducer "python3 reducer_pv_uv.py" \
  -input  "${INPUT_DIR}/sample_access.log" \
  -output "${JOB1_OUT}"
```

脚本里还包括：`hdfs dfs -mkdir -p` / `-put` 上传日志、跑之前 `-rm -r -f` 清掉旧输出目录（Streaming 要求输出目录不存在）、`hdfs dfs -cat part-*` 直接看结果、`hdfs dfs -getmerge` 把结果拉回 `results/hdfs/`。

## 结果

`results/pv_hourly.csv`（完整 24 行，本地真实跑出来的）：

| hour | pv | uv | | hour | pv | uv |
|---|---:|---:|---|---|---:|---:|
| 2026-11-24 00 | 10 | 8 | | 2026-11-24 12 | 125 | 36 |
| 2026-11-24 01 | 7 | 5 | | 2026-11-24 13 | 137 | 37 |
| 2026-11-24 02 | 9 | 7 | | 2026-11-24 14 | 155 | 36 |
| 2026-11-24 03 | 5 | 5 | | 2026-11-24 15 | 164 | 40 |
| 2026-11-24 04 | 8 | 8 | | 2026-11-24 16 | 153 | 37 |
| 2026-11-24 05 | 9 | 7 | | 2026-11-24 17 | 133 | 37 |
| 2026-11-24 06 | 31 | 17 | | 2026-11-24 18 | 132 | 32 |
| 2026-11-24 07 | 52 | 26 | | 2026-11-24 19 | 106 | 30 |
| 2026-11-24 08 | 89 | 29 | | 2026-11-24 20 | 96 | 32 |
| 2026-11-24 09 | 98 | 33 | | 2026-11-24 21 | 78 | 29 |
| 2026-11-24 10 | 148 | 39 | | 2026-11-24 22 | 60 | 24 |
| 2026-11-24 11 | 167 | 37 | | 2026-11-24 23 | 28 | 17 |

`results/top_url.csv`（完整 10 行）：

| url | count |
|---|---:|
| /api/v1/orders | 428 |
| /product/list | 212 |
| /api/v1/products | 121 |
| /static/app.js | 104 |
| / | 73 |
| /product/detail | 73 |
| /api/v1/users | 59 |
| /api/v1/cart | 49 |
| /api/v1/login | 40 |
| /api/v1/search | 38 |

从结果里能读出来的东西：

- **PV 合计 2000 = 日志行数 2000**，说明没有漏行、没有重复计数（`skipped=0`，样例数据是干净的）；
- 流量形状符合预期：**09:00–18:00 共 1412 次请求，占 70.6%**；峰值在 11:00（167 PV），低谷在 03:00（5 PV）；
- UV 在高峰期约 36–40，一共只有 42 个 IP，说明大部分 IP 当天反复访问——这也正是「UV 用 IP 去重」会低估真实用户数的原因（见「局限」）；
- Top 10 合计 1197 次，**占全部请求的 59.9%**，`/api/v1/orders` 一个就占 21.4%，是典型的齐夫/幂律形状，说明样例数据的长尾造得像那么回事；
- 404 的扫描路径（`/wp-login.php` 等）没有挤进 Top 10，和直觉一致。

## 技术栈

| 层 | 用了什么 | 说明 |
|---|---|---|
| 计算 | **Hadoop Streaming**（Hadoop 3.x）+ Python 3 | 不写 Java，mapper/reducer 就是两个只认 stdin/stdout 的脚本 |
| 语言 | Python 3.12（代码兼容 3.8+） | `re` / `heapq` / `subprocess` / `csv` / `hashlib`，全标准库 |
| 提交 | bash（`run_hadoop.sh`） | `set -euo pipefail`，变量集中配置 |
| 本地验证 | `run_local.py` | 用 `subprocess` 串管道模拟 shuffle |
| 依赖 | **无** | 没有 numpy / pandas / pyspark / mrjob，`pip install` 不需要 |

> 说明：2000 行的结果用 `csv` 模块写就够了，所以没有引入 pandas——不想为了显得「用了数据分析工具」而加依赖。真实场景要把结果接到报表/可视化时再接。

## 局限

老实说，这个项目是「教学尺度」的实现，以下几点在真实场景里都必须改：

1. **单 reducer 全量排序的内存问题**：作业二把所有 URL 的计数堆在一个 reducer 的内存里。样例只有 74 个 URL，无所谓；真实日志的长尾 URL 可能上千万，直接 OOM，而且并行度退化成 1。正确做法是「两阶段作业」或「局部 Top-N + 合并」，见上面「作业设计」。
2. **UV 用 IP 去重不等于真实用户数**：同一出口 IP（公司 NAT、校园网、运营商 CGNAT）会合并成一个人，手机换基站又会变成多个人；爬虫也会被算成访客。真实业务里要用埋点里的 `uuid` / 登录态 `uid` / cookie，IP 只是最便宜的近似值。另外时区是按日志里的 `+0800` 直接切的，跨时区部署时要先统一到 UTC。
3. **样例数据规模太小**：2000 行、74 个 URL，本地几百毫秒就跑完了（其中大头还是 4 次 Python 进程启动），这个数字**说明不了任何性能**，只证明逻辑正确。要体现性能得拿 GB 级日志上真集群，测 map/reduce 时长、shuffle 字节数、数据倾斜。
4. **没做 combiner**：作业一完全可以在 map 端先按 `(小时)` 做局部聚合再发出去（`小时 \t 局部PV \t 局部UV`，UV 用 HyperLogLog 近似），能显著减少 shuffle 数据量。这里为了代码可读性省了。
5. **mapper 的解析依赖固定格式**：正则假设 combined 格式、`ident`/`authuser` 是 `-`、HTTP 版本写成 `HTTP/x.y`。日志格式一改（比如自定义 `log_format`）就要同步改正则；生产上更稳的做法是把 `log_format` 和解析器一起版本化。
6. **按路径统计，丢掉了 query string**：`/product/detail?id=1` 和 `?id=2` 算同一个 URL。这是有意的取舍（见作业二），但要按「页面」维度分析时就得保留。
7. **没有正式测试**：只有 `run_local.py` 里的 3 条自检，没有 pytest 单测。真要长期维护，应该给 `parse_line()` 补参数化单测（合法行、截断行、越界 IP、坏月份、BOM、CRLF）。

后续可以做的：加 combiner、用 `HyperLogLog` 近似 UV、把两个作业合成一个 DAG（Oozie / Airflow）、结果接一个简单的可视化页面。

---

## 仓库结构

```text
hadoop-nginx-logs/
├── README.md
├── .gitignore
├── data/
│   └── sample_access.log        # 2000 行合成日志（固定种子，可复现）
├── src/
│   ├── gen_fake_logs.py         # 生成样例数据
│   ├── mapper_pv_uv.py          # 作业一 mapper：小时 -> "1\tIP"
│   ├── reducer_pv_uv.py         # 作业一 reducer：小时 -> PV, UV
│   ├── mapper_top_url.py        # 作业二 mapper：URL -> 1
│   └── reducer_top_url.py       # 作业二 reducer：Top 10
├── run_local.py                 # 本地模拟整套 MapReduce（跨平台）
├── run_hadoop.sh                # 真集群提交脚本（Streaming）
├── results/
│   ├── pv_hourly.csv            # 真实跑出来的结果（hour,pv,uv）
│   ├── top_url.csv              # 真实跑出来的结果（url,count）
│   └── .gitkeep
└── screenshots/                 # 终端截图占位
    └── .gitkeep
```

## 已验证

- `python run_local.py` 退出码 0，两个 CSV 生成且非空、自检全过（见上面的真实输出）；
- `python src/mapper_pv_uv.py < data/sample_access.log` 单独跑：2000 行输入、2000 行输出、`skipped=0`，格式 `小时\t1\tIP`；
- 脏数据容错：构造 9 行混合文件（BOM + CRLF + 垃圾行 + 非法 IP + 坏月份 + 越界小时 + 4 位状态码 + 截断行 + 空行），mapper 输出 `read=9 emitted=2 skipped=7`，reducer 得到 `2026-11-24 09<TAB>2<TAB>1`（同一 IP 访问两次 → PV=2、UV=1，去重正确）；
- 数据合规：`data/sample_access.log` 里出现的 IP 前缀只有 `10.`、`192.0.2.`、`198.51.100.`、`203.0.113.` 四种，没有任何真实公网地址。
