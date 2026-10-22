"""RRF（Reciprocal Rank Fusion）融合。

score(d) = Σ 1 / (k + rank_i(d))。只用名次不用原始分数，
所以 BM25 分数和余弦相似度这种量纲完全不同的结果也能直接融合。k=60 是论文里的常用值。
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence


def rrf[K: Hashable](rankings: Sequence[Sequence[K]], k: int = 60, top_n: int | None = None) -> list[tuple[K, float]]:
    scores: dict[K, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, 1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    out = sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return out[:top_n] if top_n else out
