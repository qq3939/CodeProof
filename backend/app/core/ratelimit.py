"""限流：Redis 固定窗口计数（SPEC 9.7）。

为什么用固定窗口而不是令牌桶：实现只要 INCR + EXPIRE 两条命令，足够挡住脚本刷接口；
窗口边界处可能放过 2 倍流量，对这个项目可以接受（见 D-010）。
"""

from __future__ import annotations

import time

from app.core.redis import get_redis


async def hit(key: str, limit: int, window_s: int = 60) -> bool:
    """返回 True 表示放行。"""
    bucket = int(time.time() // window_s)
    k = f"rl:{key}:{bucket}"
    r = get_redis()
    n = await r.incr(k)
    if n == 1:
        await r.expire(k, window_s + 1)
    return n <= limit
