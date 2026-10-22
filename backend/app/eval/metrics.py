"""评测指标（纯函数，便于单测）。SPEC 15.3。"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence


def agreement(a: Sequence[str], b: Sequence[str]) -> float:
    """逐项一致率。"""
    if not a:
        return 0.0
    return sum(1 for x, y in zip(a, b, strict=True) if x == y) / len(a)


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float:
    """Cohen's kappa：扣除"碰巧一致"之后的一致程度。

    κ = (p_o - p_e) / (1 - p_e)，p_o 是实际一致率，p_e 是按两边各自的类别分布随机打标签时的期望一致率。
    1 表示完全一致，0 表示和瞎猜一样，负数表示比瞎猜还差。比单纯的一致率更能说明阅卷是否可靠。
    """
    n = len(a)
    if n == 0:
        return 0.0
    po = agreement(a, b)
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if pe == 1:
        return 1.0 if po == 1 else 0.0
    return (po - pe) / (1 - pe)


def mae(pred: Sequence[float], gold: Sequence[float]) -> float:
    if not pred:
        return 0.0
    return statistics.mean(abs(p - g) for p, g in zip(pred, gold, strict=True))


def precision_recall(pred: Sequence[bool], gold: Sequence[bool]) -> tuple[float, float]:
    """pred/gold 里 True 表示"这是假说法"。"""
    tp = sum(1 for p, g in zip(pred, gold, strict=True) if p and g)
    fp = sum(1 for p, g in zip(pred, gold, strict=True) if p and not g)
    fn = sum(1 for p, g in zip(pred, gold, strict=True) if not p and g)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return precision, recall


def overlaps(hit: tuple[str, int, int], gold: tuple[str, int, int], slack: int = 3) -> bool:
    """检索命中：同一文件，行范围有交集（允许几行误差）。"""
    return hit[0] == gold[0] and hit[1] <= gold[2] + slack and gold[1] <= hit[2] + slack


def recall_at_k(hits: Sequence[tuple[str, int, int]], golds: Sequence[tuple[str, int, int]], k: int) -> float:
    """标准证据位置中有多少比例出现在 top-k 结果里。"""
    if not golds:
        return 0.0
    top = hits[:k]
    return sum(1 for g in golds if any(overlaps(h, g) for h in top)) / len(golds)


def stability(runs: Sequence[Sequence[str]]) -> float:
    """同一个回答判多次：每个要点所有次结论都相同的比例。"""
    if not runs or not runs[0]:
        return 0.0
    n = len(runs[0])
    return sum(1 for i in range(n) if len({r[i] for r in runs}) == 1) / n
