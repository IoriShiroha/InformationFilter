# -*- coding: utf-8 -*-
"""粗召回用的本地 embedding（无外部依赖）

实现：分词 -> 亚线性 TF 加权 -> 固定维度 hashing 桶 -> L2 归一化，余弦相似度即内积。

为什么不用真 embedding 模型：
    * 部署环境（Windows 单机脚本 + 现有依赖栈）没有 GPU / 模型文件；
    * 粗召回只需要「主题是否沾边」，词法相似度足够把人挡在 LLM 之外；
    * 配置里 embedding_model 仍是可调的，将来换成服务化 embedding 只需替换本类实现。
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Iterable

from pipeline.preprocess import tokenize

DEFAULT_DIM = 512


class HashingTfEmbedder:
    """确定性 hashing-TF 向量化器。

    同一段文本永远得到同一个向量，因此意图向量可以缓存、结果可复现。
    """

    def __init__(self, model: str = "local-hashing", dim: int = DEFAULT_DIM) -> None:
        self.model = (model or "local-hashing").strip()
        self.dim = max(16, int(dim))
        self._cache: dict[str, dict[int, float]] = {}

    # ---------------------------------------------------------------- 向量
    def _bucket(self, token: str) -> int:
        digest = hashlib.blake2b(token.encode("utf-8", "ignore"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dim

    def vector(self, text: str, *, use_cache: bool = True) -> dict[int, float]:
        key = (text or "")[:6000]
        if use_cache and key in self._cache:
            return self._cache[key]
        counts = Counter(tokenize(key))
        vec: dict[int, float] = {}
        for token, tf in counts.items():
            weight = 1.0 + math.log(tf) if tf > 0 else 0.0
            bucket = self._bucket(token)
            vec[bucket] = vec.get(bucket, 0.0) + weight
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm > 0:
            for bucket in vec:
                vec[bucket] /= norm
        if use_cache:
            self._cache[key] = vec
            if len(self._cache) > 2048:  # 防止长跑内存膨胀
                self._cache.clear()
                self._cache[key] = vec
        return vec

    def similarity(self, a: str | dict[int, float], b: str | dict[int, float]) -> float:
        va = a if isinstance(a, dict) else self.vector(a)
        vb = b if isinstance(b, dict) else self.vector(b)
        if not va or not vb:
            return 0.0
        if len(va) > len(vb):
            va, vb = vb, va
        return sum(weight * vb.get(bucket, 0.0) for bucket, weight in va.items())

    # ---------------------------------------------------------------- 意图文本
    @staticmethod
    def compose_intent_text(
        description: str,
        examples_include: Iterable[str] = (),
        examples_exclude: Iterable[str] = (),
    ) -> str:
        """把意图描述与正例拼成用于粗召回的文本（反例不进相似度，避免反向拉低）。"""
        parts = [description or ""]
        parts.extend(examples_include or [])
        return "\n".join(part for part in parts if part)
