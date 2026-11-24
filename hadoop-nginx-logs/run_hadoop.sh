#!/usr/bin/env bash
#
# Hadoop Streaming 提交脚本 —— 网站日志分析（两个作业：每小时 PV/UV、Top 10 热门 URL）
#
# 用法：
#   export HADOOP_HOME=/opt/hadoop-3.3.6
#   chmod +x run_hadoop.sh
#   ./run_hadoop.sh
#
# ============================================================================
#  需要你按自己集群改的地方只有 3 处，都在下面的「配置区」里，用 ★ 标出：
#    ★1 INPUT_DIR   —— 日志上传到哪个 HDFS 目录
#    ★2 OUTPUT_DIR  —— 两个作业的输出根目录（各自再分子目录）
#    ★3 PYTHON_BIN  —— 集群里 Python 解释器的命令/绝对路径
#                       （python3 / /usr/bin/python3 / conda 环境的绝对路径）
#
#  另外两个集群环境相关的坑，脚本里也标了：
#    * 所有 NodeManager 节点上都必须有同一个 Python（Streaming 是在节点上起进程跑脚本）；
#    * 若集群用 `-mapper mapper.py` 直接执行脚本（而不是 `python3 mapper.py`），
#      需要脚本有可执行位并带 shebang：chmod +x src/*.py
# ============================================================================
#
# 注意：mapper_top_url.py 里 `from mapper_pv_uv import LOG_RE` 复用了日志解析正则，
# 所以作业二的 -files 必须同时带上 mapper_pv_uv.py，否则 task 会 ImportError。
#
set -euo pipefail

# ------------------------------- 配置区 ------------------------------- #
: "${HADOOP_HOME:?请先 export HADOOP_HOME=/path/to/hadoop}"

INPUT_DIR="/user/$(whoami)/nginx-logs/input"        # ★1 HDFS 输入目录
OUTPUT_DIR="/user/$(whoami)/nginx-logs/output"      # ★2 HDFS 输出根目录
PYTHON_BIN="python3"                                # ★3 集群里的 Python

# Streaming jar：留空 = 自动到 $HADOOP_HOME/share/hadoop/tools/lib 下找 hadoop-streaming-*.jar；
# 也可以直接写死，例如 STREAMING_JAR="/opt/hadoop-3.3.6/share/hadoop/tools/lib/hadoop-streaming-3.3.6.jar"
STREAMING_JAR=""

LOCAL_LOG="data/sample_access.log"                  # 本地待上传的日志
JOB1_OUT="${OUTPUT_DIR}/pv_uv"                      # 作业一输出
JOB2_OUT="${OUTPUT_DIR}/top_url"                    # 作业二输出
LOCAL_RESULT_DIR="results/hdfs"                     # 结果拉回本地的目录

# hadoop-streaming-*.jar 的文件名随版本变化（3.1.4 / 3.3.6 / 3.4.0 ...），
# STREAMING_JAR 留空时自动找第一个。
# 这里用 for 循环而不是数组/管道：老集群的 bash 4.2 在 set -u 下取空数组长度会报 unbound variable。
if [ -z "${STREAMING_JAR}" ]; then
  for jar in "${HADOOP_HOME}"/share/hadoop/tools/lib/hadoop-streaming-*.jar; do
    if [ -f "${jar}" ]; then
      STREAMING_JAR="${jar}"
      break
    fi
  done
fi
if [ -z "${STREAMING_JAR}" ]; then
  echo "[ERROR] 在 ${HADOOP_HOME}/share/hadoop/tools/lib 下找不到 hadoop-streaming-*.jar" >&2
  echo "        请检查 HADOOP_HOME 是否正确，或手动指定 STREAMING_JAR。" >&2
  exit 1
fi

# ------------------------------- 0. 前置检查 ------------------------------- #
command -v hadoop >/dev/null 2>&1 || { echo "[ERROR] PATH 里没有 hadoop 命令" >&2; exit 1; }
command -v hdfs   >/dev/null 2>&1 || { echo "[ERROR] PATH 里没有 hdfs 命令" >&2; exit 1; }
[ -f "${LOCAL_LOG}" ] || { echo "[ERROR] 找不到 ${LOCAL_LOG}，先在本地跑：python src/gen_fake_logs.py" >&2; exit 1; }
[ -f "src/mapper_pv_uv.py" ] || { echo "[ERROR] 请在仓库根目录执行本脚本" >&2; exit 1; }

echo "==> Streaming jar : ${STREAMING_JAR}"
echo "==> 输入目录      : ${INPUT_DIR}"
echo "==> 输出目录      : ${OUTPUT_DIR}"

# ------------------------------- 1. 上传日志 ------------------------------- #
echo
echo "==> [1/4] 建 HDFS 目录并上传日志"
hdfs dfs -mkdir -p "${INPUT_DIR}"
hdfs dfs -put -f "${LOCAL_LOG}" "${INPUT_DIR}/sample_access.log"
hdfs dfs -ls -h "${INPUT_DIR}"

# ------------------------------- 2. 作业一：每小时 PV / UV ------------------------------- #
# key = "2026-11-24 09"（小时），value = "1\t<IP>"
# 3 个 reduce task（默认，可按数据量调）；同一小时的记录必然进同一个 reducer，
# 因为 Streaming 默认用 key 的 hash 分区 —— 这是 PV/UV 能算对的前提。
echo
echo "==> [2/4] 作业一：每小时 PV / UV"
hdfs dfs -rm -r -f "${JOB1_OUT}" >/dev/null 2>&1 || true     # Streaming 要求输出目录不存在

hadoop jar "${STREAMING_JAR}" \
  -D mapreduce.job.name="nginx-pv-uv" \
  -D mapreduce.job.reduces=3 \
  -D stream.num.map.output.key.fields=1 \
  -files "src/mapper_pv_uv.py,src/reducer_pv_uv.py" \
  -mapper "${PYTHON_BIN} mapper_pv_uv.py" \
  -reducer "${PYTHON_BIN} reducer_pv_uv.py" \
  -input "${INPUT_DIR}/sample_access.log" \
  -output "${JOB1_OUT}"

# ------------------------------- 3. 作业二：Top 10 热门 URL ------------------------------- #
# 注意 -D mapreduce.job.reduces=1：Top-N 是全局聚合，必须 **只有一个 reducer** 才能得到
# 全局 Top10；如果开 3 个 reducer，你会得到「每个 reducer 各自的前 10」，不是全局结果。
# 这是单 reducer 方案的硬约束，也是它不可扩展的地方（见 README「局限」）。
echo
echo "==> [3/4] 作业二：Top 10 热门 URL（单 reducer）"
hdfs dfs -rm -r -f "${JOB2_OUT}" >/dev/null 2>&1 || true

hadoop jar "${STREAMING_JAR}" \
  -D mapreduce.job.name="nginx-top-url" \
  -D mapreduce.job.reduces=1 \
  -D stream.num.map.output.key.fields=1 \
  -files "src/mapper_top_url.py,src/mapper_pv_uv.py,src/reducer_top_url.py" \
  -mapper "${PYTHON_BIN} mapper_top_url.py" \
  -reducer "${PYTHON_BIN} reducer_top_url.py" \
  -input "${INPUT_DIR}/sample_access.log" \
  -output "${JOB2_OUT}"

# ------------------------------- 4. 查看 / 拉回结果 ------------------------------- #
echo
echo "==> [4/4] 结果"
echo "--- 每小时 PV / UV（hour <TAB> pv <TAB> uv）---"
hdfs dfs -cat "${JOB1_OUT}/part-*"
echo "--- Top 10 热门 URL（url <TAB> count）---"
hdfs dfs -cat "${JOB2_OUT}/part-*"

mkdir -p "${LOCAL_RESULT_DIR}"
hdfs dfs -getmerge "${JOB1_OUT}" "${LOCAL_RESULT_DIR}/pv_hourly.tsv"
hdfs dfs -getmerge "${JOB2_OUT}" "${LOCAL_RESULT_DIR}/top_url.tsv"
echo
echo "==> 已拉回本地：${LOCAL_RESULT_DIR}/pv_hourly.tsv, ${LOCAL_RESULT_DIR}/top_url.tsv"
echo "==> 完成。"
