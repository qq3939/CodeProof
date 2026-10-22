"""git 子进程封装。

安全要点（SPEC 18.2：绝不运行被分析仓库的代码）：
- 禁用 hooks（core.hooksPath=/dev/null），clone/checkout 不会触发仓库里的脚本；
- 不拉子模块、跳过 LFS 下载；
- GIT_TERMINAL_PROMPT=0：私有仓库要密码时立刻失败，而不是卡住等输入；
- 所有调用都有超时。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import AppError

SAFE_CONFIG = ["-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=always", "-c", "advice.detachedHead=false"]
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GitError(AppError):
    code = "git_error"


@dataclass
class GitResult:
    code: int
    out: str
    err: str


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_ASKPASS": "/bin/true"})
    return env


async def run_git(args: list[str], cwd: str | Path | None = None, timeout: float = 60, check: bool = True) -> GitResult:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *SAFE_CONFIG,
        *args,
        cwd=str(cwd) if cwd else None,
        env=_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitError(f"git {args[0]} 超时（{timeout}s）") from None
    res = GitResult(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))
    if check and res.code != 0:
        raise GitError(f"git {args[0]} 失败: {res.err.strip()[:500]}")
    return res


async def resolve_ref(url: str, ref: str | None) -> tuple[str, str | None]:
    """把分支/标签解析成提交 SHA。返回 (sha, 默认分支名)。

    用 ls-remote 而不是先 clone：只需要一次很轻的网络请求，
    如果这个 SHA 已经分析过，就完全不用克隆（去重的前提）。
    """
    default_branch: str | None = None
    res = await run_git(["ls-remote", "--symref", url, "HEAD"], timeout=30)
    for line in res.out.splitlines():
        if line.startswith("ref: refs/heads/"):
            default_branch = line.split("refs/heads/", 1)[1].split()[0]
    if ref and SHA_RE.match(ref.lower()):
        return ref.lower(), default_branch
    if not ref:
        for line in res.out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "HEAD" and SHA_RE.match(parts[0]):
                return parts[0], default_branch
        raise GitError("无法解析默认分支")
    res = await run_git(
        ["ls-remote", url, f"refs/heads/{ref}", f"refs/tags/{ref}", f"refs/tags/{ref}^{{}}"], timeout=30
    )
    found: dict[str, str] = {}
    for line in res.out.splitlines():
        sha, name = line.split()
        found[name] = sha
    # 附注标签要取 ^{} 指向的提交，而不是标签对象本身
    for key in (f"refs/tags/{ref}^{{}}", f"refs/heads/{ref}", f"refs/tags/{ref}"):
        if key in found:
            return found[key], default_branch
    if re.fullmatch(r"[0-9a-f]{7,39}", ref.lower()):
        raise GitError("短 SHA 无法远程解析，请提供完整的 40 位 SHA")
    raise GitError(f"找不到分支或标签: {ref}")


async def shallow_fetch(url: str, sha: str, dest: Path, timeout: float, ref: str | None = None, depth: int = 1) -> None:
    """浅克隆到 dest 并检出 sha。

    分支/标签：git clone --depth 1 --single-branch；
    指定 SHA（或分支已前进导致 HEAD 不等于 sha）：单独 fetch 那个提交。
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        if ref and not SHA_RE.match(ref):
            await run_git(
                ["clone", "--depth", str(depth), "--single-branch", "--no-tags", "--branch", ref, url, str(tmp)],
                timeout=timeout,
            )
            head = (await run_git(["rev-parse", "HEAD"], cwd=tmp)).out.strip()
            if head != sha:
                await run_git(["fetch", "--depth", str(depth), "origin", sha], cwd=tmp, timeout=timeout)
                await run_git(["checkout", "--force", sha], cwd=tmp)
        else:
            await run_git(["init", "-q", str(tmp)])
            await run_git(["remote", "add", "origin", url], cwd=tmp)
            await run_git(["fetch", "--depth", str(depth), "origin", sha], cwd=tmp, timeout=timeout)
            await run_git(["checkout", "--force", "FETCH_HEAD"], cwd=tmp)
        tmp.rename(dest)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


async def fetch_commit(repo_dir: Path, ref: str, timeout: float, depth: int = 50) -> str:
    """在已有克隆里再拉一个提交/引用（改动理解模式要拿到 base 和 head）。返回 SHA。"""
    await run_git(["fetch", "--depth", str(depth), "origin", ref], cwd=repo_dir, timeout=timeout)
    return (await run_git(["rev-parse", "FETCH_HEAD"], cwd=repo_dir)).out.strip()


async def merge_base(repo_dir: Path, a: str, b: str) -> str | None:
    res = await run_git(["merge-base", a, b], cwd=repo_dir, check=False)
    return res.out.strip() or None if res.code == 0 else None


async def diff(repo_dir: Path, base: str, head: str) -> str:
    res = await run_git(["diff", "--no-color", "--unified=3", "--no-ext-diff", base, head], cwd=repo_dir, timeout=60)
    return res.out


def dir_size(path: Path, skip_dirs: set[str]) -> tuple[int, int]:
    """统计 (字节数, 文件数)，跳过 .git 等目录。"""
    total, count = 0, 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for f in files:
            fp = Path(root) / f
            if fp.is_symlink():
                continue
            try:
                total += fp.stat().st_size
                count += 1
            except OSError:
                continue
    return total, count
