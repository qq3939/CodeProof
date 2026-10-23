"""子进程调用（git、ripgrep）。

为什么不用 asyncio.create_subprocess_exec：psycopg 的异步模式在 Windows 上只能跑在
SelectorEventLoop 里，而 Windows 的 SelectorEventLoop 不支持 asyncio 子进程（P-014）。
放进线程里跑 subprocess.run，哪种事件循环都能用；Linux 上行为不变。
"""

from __future__ import annotations

import asyncio
import subprocess


async def run(
    args: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None, timeout: float
) -> tuple[int, bytes, bytes]:
    """运行命令，返回 (退出码, stdout, stderr)。

    超时抛 subprocess.TimeoutExpired：subprocess.run 会先杀掉子进程再抛，不会留下孤儿进程。
    """
    p = await asyncio.to_thread(subprocess.run, args, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr
