"""仓库地址规范化（SPEC 7.1）。

同一个仓库可能被写成 git@github.com:A/B.git、https://github.com/a/b/、http://github.com/A/B 等，
不规范化的话去重会失效，同一份代码被分析好几遍。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.errors import AppError


@dataclass(frozen=True)
class RepoUrl:
    url: str  # 规范化后的地址，用作 repositories.url 的唯一键
    owner: str
    name: str
    host: str
    pr_number: int | None = None  # 输入的是 PR 地址时


_SSH = re.compile(r"^git@([^:]+):([^/]+)/(.+?)(?:\.git)?/?$")
_PR = re.compile(r"^(https?://[^/]+/[^/]+/[^/]+)/pull/(\d+)(?:/.*)?$")


def normalize_repo_url(raw: str, *, allow_file: bool = False) -> RepoUrl:
    s = raw.strip()
    if not s:
        raise AppError("仓库地址不能为空", code="invalid_url")
    if s.startswith("file://"):
        if not allow_file:
            raise AppError("只支持 https 仓库地址", code="invalid_url")
        path = s[len("file://") :].rstrip("/")
        name = path.rsplit("/", 1)[-1].lower()
        return RepoUrl(url=f"file://{path}", owner="local", name=name, host="local")
    pr_number: int | None = None
    m = _PR.match(s)
    if m:
        s, pr_number = m.group(1), int(m.group(2))
    m = _SSH.match(s)
    if m:
        host, owner, name = m.group(1), m.group(2), m.group(3)
    else:
        m2 = re.match(r"^(?:https?://)?(?:[^@/]+@)?([^/]+)/([^/]+)/([^/?#]+?)(?:\.git)?/?(?:[?#].*)?$", s)
        if not m2:
            raise AppError(f"无法识别的仓库地址: {raw}", code="invalid_url")
        host, owner, name = m2.group(1), m2.group(2), m2.group(3)
    host = host.lower()
    if "." not in host:
        raise AppError(f"无法识别的仓库地址: {raw}", code="invalid_url")
    owner, name = owner.lower(), name.lower()
    if not re.fullmatch(r"[a-z0-9_.-]+", owner) or not re.fullmatch(r"[a-z0-9_.-]+", name):
        raise AppError("仓库 owner 或名称包含非法字符", code="invalid_url")
    return RepoUrl(url=f"https://{host}/{owner}/{name}", owner=owner, name=name, host=host, pr_number=pr_number)
