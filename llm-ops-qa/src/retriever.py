# -*- coding: utf-8 -*-
"""离线检索模块：中文友好分词 + TF-IDF + 余弦相似度。

设计取舍（为什么不用 jieba / sklearn）：
- 中文分词器会引入词表、词典更新和 OOV 问题，运维文本里还混着大量英文与命令，
  分词边界很难对齐。这里改用**字符级 bigram** 表示中文（"磁盘满了" -> 磁盘/盘满/满了），
  英文和数字按**词**切分（nginx、502、proxy_read_timeout 保持完整）。
  这种做法的召回是可靠的，语义泛化能力有限，代价见 README 的「局限」一节。
- 向量化只用 numpy：先统计词频，再乘 IDF，最后做 L2 归一化，
  查询向量与文档矩阵做一次点积即得全部余弦相似度。

IDF 采用平滑公式：idf(t) = log((1 + N) / (1 + df(t))) + 1
（+1 保证出现在所有文档里的词权重也不为 0，避免整句被抹平）
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np

# 中文统一表意文字基本区。命中连续的汉字串后按 2-gram 切分。
_CJK_RANGE = ("\u4e00", "\u9fff")

# 一个 token 要么是汉字串，要么是以字母/数字开头结尾的英文数字串
# （允许中间出现 . _ + # - ，这样 192.0.2.10、proxy_read_timeout、c++ 不会被切碎）。
_TOKEN_RE = re.compile(r"[a-z0-9](?:[a-z0-9._+#-]*[a-z0-9])?|[\u4e00-\u9fff]+")

# 权重调整：标题和标签比正文更能代表文档主题，重复计入（类似 BM25F 的字段加权）。
TITLE_WEIGHT = 2
TAG_WEIGHT = 2

# 停用词：这些词几乎出现在每个问句里（"怎么排查""什么原因""怎么办"），
# 对区分文档没有任何贡献，反而会给不相关文档刷出一点点相似度。
# 只保留一张极小的表，避免误伤"排查""原因"这类真正有信息量的词。
STOPWORDS = frozenset(
    {
        "怎么", "么办", "什么", "么样", "如何", "为什", "怎样", "可以", "能不",
        "不能", "是否", "这个", "那个", "一下", "请问", "我们", "你们",
        "the", "and", "for", "with", "how", "what", "why", "is", "are", "to",
        "of", "a", "an", "in", "on", "do", "does",
    }
)


def tokenize(text: str) -> list[str]:
    """把一段文本切成检索用的 token 列表。

    规则：
    1. 全部转小写；
    2. 汉字串 -> 字符 bigram（单字串保留单字）；
    3. 英文/数字串 -> 整词一个 token；
    4. 去掉停用词。
    """
    if not text:
        return []

    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text.lower()):
        piece = match.group(0)
        if _CJK_RANGE[0] <= piece[0] <= _CJK_RANGE[1]:
            if len(piece) == 1:
                tokens.append(piece)
            else:
                tokens.extend(piece[i : i + 2] for i in range(len(piece) - 1))
        else:
            tokens.append(piece)
    return [token for token in tokens if token not in STOPWORDS]


def _doc_text(doc: dict[str, Any]) -> str:
    """把一条知识库记录拼成可索引的文本（标题/标签重复计权）。"""
    title = str(doc.get("title", ""))
    question = str(doc.get("question", ""))
    tags = " ".join(str(t) for t in doc.get("tags", []) or [])
    answer = str(doc.get("answer", ""))
    return " ".join(
        [
            (title + " ") * TITLE_WEIGHT,
            question,
            (tags + " ") * TAG_WEIGHT,
            answer,
        ]
    )


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """按行做 L2 归一化；全零行保持全零，不做除法。"""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


class Retriever:
    """TF-IDF 检索器。

    用法::

        r = Retriever("data/kb.jsonl")
        for doc, score in r.search("nginx 502 怎么排查", top_k=3):
            print(doc["id"], round(score, 3), doc["title"])
    """

    def __init__(self, kb_path: str | Path) -> None:
        self.kb_path = Path(kb_path)
        self.docs: list[dict[str, Any]] = self._load(self.kb_path)
        if not self.docs:
            raise ValueError(f"知识库为空：{self.kb_path}")

        self.vocab: dict[str, int] = {}
        self.idf: np.ndarray = np.zeros(0, dtype=np.float64)
        self.doc_matrix: np.ndarray = np.zeros((0, 0), dtype=np.float64)
        self._build_index()

    # ------------------------------------------------------------------ 加载
    @staticmethod
    def _load(path: Path) -> list[dict[str, Any]]:
        """逐行读取 JSON Lines，出错时带上行号，方便定位坏数据。"""
        docs: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    docs.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path} 第 {lineno} 行不是合法 JSON：{exc}") from exc
        return docs

    # ------------------------------------------------------------------ 建索引
    def _build_index(self) -> None:
        term_counts: list[dict[str, int]] = []
        for doc in self.docs:
            counts: dict[str, int] = {}
            for token in tokenize(_doc_text(doc)):
                counts[token] = counts.get(token, 0) + 1
            term_counts.append(counts)
            for token in counts:
                if token not in self.vocab:
                    self.vocab[token] = len(self.vocab)

        n_docs = len(self.docs)
        n_terms = len(self.vocab)

        df = np.zeros(n_terms, dtype=np.float64)
        for counts in term_counts:
            for token in counts:
                df[self.vocab[token]] += 1.0

        # 平滑 IDF：log((1 + N) / (1 + df)) + 1
        self.idf = np.log((1.0 + n_docs) / (1.0 + df)) + 1.0

        matrix = np.zeros((n_docs, n_terms), dtype=np.float64)
        for row, counts in enumerate(term_counts):
            for token, count in counts.items():
                matrix[row, self.vocab[token]] = float(count)
        matrix *= self.idf  # 广播：(N, V) * (V,)
        self.doc_matrix = _l2_normalize(matrix)

    # ------------------------------------------------------------------ 检索
    def _query_vector(self, query: str) -> np.ndarray | None:
        counts: dict[str, int] = {}
        for token in tokenize(query):
            idx = self.vocab.get(token)
            if idx is None:  # 查询里出现词表外的词，直接忽略
                continue
            counts[idx] = counts.get(idx, 0) + 1
        if not counts:
            return None

        vec = np.zeros(len(self.vocab), dtype=np.float64)
        for idx, count in counts.items():
            vec[idx] = float(count)
        vec *= self.idf
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            return None
        return vec / norm

    def search(self, query: str, top_k: int = 3) -> list[tuple[dict[str, Any], float]]:
        """返回 [(文档, 余弦相似度), ...]，按相似度降序，最多 top_k 条。

        相似度为 0 的文档不会返回：它和查询没有任何共同 token，列进「参考」只会误导人。
        """
        if not query or not query.strip() or top_k <= 0:
            return []

        vec = self._query_vector(query)
        if vec is None:
            return []

        scores = self.doc_matrix @ vec
        order = np.argsort(-scores, kind="stable")[:top_k]

        hits: list[tuple[dict[str, Any], float]] = []
        for idx in order:
            score = float(scores[int(idx)])
            if score <= 0.0:
                continue
            hits.append((self.docs[int(idx)], score))
        return hits

    # ------------------------------------------------------------------ 辅助
    @property
    def vocab_size(self) -> int:
        return len(self.vocab)

    def __len__(self) -> int:
        return len(self.docs)
