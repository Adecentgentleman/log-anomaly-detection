# 顾东洲 · 大数据 / 数据分析项目集

广州华夏职业学院 · 大数据技术（2024.09 入学）

四个独立练手/课程项目，数据和参数全部可复现。每个项目目录下有自己的 README，写清了背景、方法、结果和运行命令。

---

## 项目索引

| 目录 | 项目 | 一句话 | 技术栈 |
|---|---|---|---|
| `log-anomaly-detection/` | 无监督日志异常检测 | 自造 nginx 日志，用自实现孤立森林给异常打分 | Python / NumPy / pandas |
| `hadoop-nginx-logs/` | Hadoop 网站日志分析 | 伪分布式集群跑 MapReduce，出 PV/UV 和热门 URL | Hadoop / HDFS / MapReduce / Shell |
| `llm-ops-qa/` | 智能运维问答助手 | 运维手册向量化入库，检索 + 大模型回答 | Python / LangChain / 向量库 |
| `ops-check-script/` | 自动化巡检脚本 | 定时巡检资源使用率，超阈值邮件告警 | Python / pandas / Shell |

---

## 快速上手

每个目录独立运行，互不依赖。

```bash
cd log-anomaly-detection
pip install -r requirements.txt
python src/main.py --input data/sample_access.log
```

```bash
cd hadoop-nginx-logs
# 有 Hadoop 环境：提交 MapReduce 作业
bash run_hadoop.sh
# 没有 Hadoop：本地用 Python 复现同样结果
python run_local.py
```

```bash
cd llm-ops-qa
pip install -r requirements.txt
python src/main.py
```

```bash
cd ops-check-script
python src/main.py --config config.json
```

---

## 数据说明

仓库里的日志数据全部是**自己造的模拟数据**，不是生产环境日志，不含任何真实 IP、域名或用户信息。

- `log-anomaly-detection/data/`：600 行 nginx 访问日志 + 对应标签，其中注入 18 条异常
- `hadoop-nginx-logs/data/`：模拟访问日志，用于 MapReduce 统计

---

## 结果汇总

| 项目 | 结果 |
|---|---|
| log-anomaly-detection | TP 17 / FP 1 / FN 1 / TN 581，precision 0.944，recall 0.944（18 条注入异常） |
| hadoop-nginx-logs | 输出 PV/UV、分小时流量、访问量最高的 URL 三张结果表到 `results/` |
| llm-ops-qa | 固定评测问题集对比改进前后的命中情况，报告在 `reports/eval_report.md` |
| ops-check-script | 巡检 CPU / 内存 / 磁盘阈值告警与趋势报表 |

---

## 关于我

- 联系方式：13250599662 ｜ a202213822182300@163.com
- 求职方向：大数据开发 / 数据分析 实习，广州，可立即到岗
