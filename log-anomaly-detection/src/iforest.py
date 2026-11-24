#!/usr/bin/env python3
"""纯 NumPy 实现的 Isolation Forest。

算法来源
--------
Liu, F. T., Ting, K. M., & Zhou, Z.-H. (2008). *Isolation Forest*.
Proceedings of the 8th IEEE International Conference on Data Mining (ICDM),
413-422. https://doi.org/10.1109/ICDM.2008.17

核心思想
--------
异常点数量少、特征取值与多数点不同，因此在随机切分中更容易被“孤立”：
用少量随机特征和随机切分点递归划分数据，异常点通常落在较浅的叶节点。
对每棵树记录样本的路径长度 h(x)，则异常分数为

    s(x, n) = 2 ** (-E[h(x)] / c(n))
    c(n) = 2 * H(n - 1) - 2 * (n - 1) / n

其中 H 为调和数，代码用 ln(n - 1) + Euler-Mascheroni 常数近似；n 是每棵
树的子采样大小。s 越接近 1 越异常，越接近 0.5 越正常。

本文件不依赖 scikit-learn，只用 numpy 完成子采样、建树、路径长度和打分。
"""

from __future__ import annotations

import sys
from typing import Optional

import numpy as np

_EULER_GAMMA = 0.5772156649015329


def _average_path_length(n_samples: int) -> float:
    """c(n) = 2 * H(n - 1) - 2 * (n - 1) / n 的数值近似。"""
    if n_samples <= 1:
        return 0.0
    harmonic = np.log(n_samples - 1) + _EULER_GAMMA
    return float(2.0 * harmonic - 2.0 * (n_samples - 1) / n_samples)


class _Node:
    """孤立树节点；feature < 0 表示叶节点。"""

    __slots__ = ("feature", "threshold", "left", "right", "size")

    def __init__(
        self,
        feature: int = -1,
        threshold: float = 0.0,
        left: Optional["_Node"] = None,
        right: Optional["_Node"] = None,
        size: int = 0,
    ) -> None:
        self.feature = feature
        self.threshold = threshold
        self.left = left
        self.right = right
        self.size = size

    @property
    def is_leaf(self) -> bool:
        return self.feature < 0


class IsolationForest:
    """孤立森林：无监督异常检测器。

    参数
    ----
    n_estimators:
        孤立树数量。
    max_samples:
        每棵树的子采样上限；实际值取 min(max_samples, n_samples)。
    contamination:
        预期的异常比例，用于在训练分数上确定判定阈值（0, 0.5]。
    random_state:
        随机种子。固定后结果可复现。

    拟合后属性
    ----------
    decision_scores_:
        训练集每个样本的异常分数，越大越异常。
    threshold_:
        由 contamination 分位数得到的判定阈值，分数 >= 阈值为异常。
    """

    def __init__(
        self,
        n_estimators: int = 100,
        max_samples: int = 256,
        contamination: float = 0.03,
        random_state: Optional[int] = 42,
    ) -> None:
        if not isinstance(n_estimators, (int, np.integer)) or n_estimators <= 0:
            raise ValueError("n_estimators 必须是正整数")
        if not isinstance(max_samples, (int, np.integer)) or max_samples < 2:
            raise ValueError("max_samples 必须是 >= 2 的整数")
        if not 0.0 < float(contamination) <= 0.5:
            raise ValueError("contamination 必须在 (0, 0.5] 区间内")

        self.n_estimators = int(n_estimators)
        self.max_samples = int(max_samples)
        self.contamination = float(contamination)
        self.random_state = random_state
        self._trees: list[_Node] = []
        self._rng = np.random.default_rng(random_state)
        self._mean: Optional[np.ndarray] = None
        self._scale: Optional[np.ndarray] = None
        self._normalizer: float = 1.0
        self._fitted = False

    @staticmethod
    def _validate_matrix(X: np.ndarray, *, allow_empty: bool = False) -> np.ndarray:
        matrix = np.asarray(X, dtype=np.float64)
        if matrix.ndim != 2:
            raise ValueError(f"X 必须是二维数组，实际维度为 {matrix.ndim}")
        if matrix.shape[0] == 0 and not allow_empty:
            raise ValueError("X 不能为空")
        if matrix.shape[1] == 0:
            raise ValueError("X 至少需要一列特征")
        if not np.isfinite(matrix).all():
            raise ValueError("X 包含 NaN 或 inf")
        return np.ascontiguousarray(matrix)

    def _grow(self, X: np.ndarray, depth: int) -> _Node:
        """递归建树；每层重新计算当前节点的特征范围。"""
        n_samples = X.shape[0]
        if n_samples <= 1 or depth >= self._max_depth:
            return _Node(size=n_samples)

        minimums = X.min(axis=0)
        maximums = X.max(axis=0)
        spans = maximums - minimums
        candidates = np.flatnonzero(spans > 1e-12)
        if candidates.size == 0:
            # 所有特征都相同，无法继续切分。
            return _Node(size=n_samples)

        feature = int(self._rng.choice(candidates))
        lower = float(minimums[feature])
        upper = float(maximums[feature])
        threshold = float(self._rng.uniform(lower, upper))

        # 在连续值上 < 和 <= 等价；下面保证两侧都非空。
        left_mask = X[:, feature] < threshold
        if left_mask.all() or not left_mask.any():
            unique_values = np.unique(X[:, feature])
            gap = int(self._rng.integers(0, unique_values.size - 1))
            threshold = float(
                unique_values[gap] + (unique_values[gap + 1] - unique_values[gap]) / 2.0
            )
            left_mask = X[:, feature] <= unique_values[gap]

        if left_mask.all() or not left_mask.any():  # pragma: no cover - 数值兜底
            return _Node(size=n_samples)

        node = _Node(feature=feature, threshold=threshold, size=n_samples)
        node.left = self._grow(X[left_mask], depth + 1)
        node.right = self._grow(X[~left_mask], depth + 1)
        return node

    def fit(self, X: np.ndarray) -> "IsolationForest":
        """训练多棵孤立树。"""
        matrix = self._validate_matrix(X)
        n_samples, n_features = matrix.shape
        if n_samples < 2:
            raise ValueError("至少需要 2 个样本才能训练孤立森林")

        self.n_features_in_ = n_features
        self._mean = matrix.mean(axis=0)
        raw_scale = matrix.std(axis=0)
        self._scale = np.where(raw_scale < 1e-12, 1.0, raw_scale)
        standardized = (matrix - self._mean) / self._scale

        self.max_samples_ = int(min(self.max_samples, n_samples))
        if self.max_samples_ < 2:
            raise ValueError("有效子采样数必须 >= 2")
        self._max_depth = int(np.ceil(np.log2(self.max_samples_)))
        self._normalizer = _average_path_length(self.max_samples_)

        self._rng = np.random.default_rng(self.random_state)
        self._trees = []
        for _ in range(self.n_estimators):
            sample_indices = self._rng.choice(
                n_samples, size=self.max_samples_, replace=False
            )
            self._trees.append(self._grow(standardized[sample_indices], depth=0))

        scores = self._score_standardized(standardized)
        self.decision_scores_ = scores

        # 用经验分位数确定阈值，保证预测异常数约为 ceil(contamination * n)。
        n_anomalies = max(1, int(np.ceil(self.contamination * n_samples)))
        cut_index = n_samples - n_anomalies
        self.threshold_ = float(np.partition(scores, cut_index)[cut_index])
        self._fitted = True
        return self

    def _path_length(self, sample: np.ndarray, tree: _Node) -> float:
        """样本在一棵树中的路径长度 h(x) = 深度 + c(叶节点样本数)。"""
        node = tree
        depth = 0
        while not node.is_leaf:
            depth += 1
            if sample[node.feature] < node.threshold:
                node = node.left
            else:
                node = node.right
        return float(depth) + _average_path_length(node.size)

    def _score_standardized(self, standardized: np.ndarray) -> np.ndarray:
        path_sum = np.zeros(standardized.shape[0], dtype=np.float64)
        for tree in self._trees:
            for row_index in range(standardized.shape[0]):
                path_sum[row_index] += self._path_length(standardized[row_index], tree)
        mean_path = path_sum / len(self._trees)
        return np.power(2.0, -mean_path / self._normalizer)

    def _check_fitted(self) -> None:
        if not self._fitted or self._mean is None or self._scale is None:
            raise RuntimeError("模型尚未训练，请先调用 fit(X)")

    def score_samples(self, X: np.ndarray) -> np.ndarray:
        """返回异常分数，越大越异常。"""
        self._check_fitted()
        matrix = self._validate_matrix(X)
        if matrix.shape[1] != self.n_features_in_:
            raise ValueError(
                f"特征数不匹配：训练时为 {self.n_features_in_}，预测时为 {matrix.shape[1]}"
            )
        standardized = (matrix - self._mean) / self._scale
        return self._score_standardized(standardized)

    def predict(self, X: np.ndarray) -> np.ndarray:
        """返回 1（异常）或 -1（正常）。"""
        self._check_fitted()
        scores = self.score_samples(X)
        return np.where(scores >= self.threshold_, 1, -1).astype(int)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    # 一个最小的自检：把明显远离原点的点排到前面。
    rng = np.random.default_rng(2026)
    normal = rng.normal(0.0, 1.0, size=(200, 2))
    outliers = np.array([[8.0, 8.0], [-9.0, 7.0]])
    data = np.vstack([normal, outliers])
    detector = IsolationForest(
        n_estimators=100, max_samples=128, contamination=0.02, random_state=42
    )
    detector.fit(data)
    scores = detector.score_samples(data)
    predictions = detector.predict(data)
    print(f"异常分数均值（正常/异常）: {scores[:200].mean():.4f} / {scores[200:].mean():.4f}")
    print(f"疑似异常预测: {np.flatnonzero(predictions == 1).tolist()}")
