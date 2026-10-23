"""单元测试：沙箱、证据校验、RRF、掌握度排期、提示防泄露、知识库切块、模型重试与降级。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.agents.interviewer import leaks
from app.analysis import evidence
from app.core.config import Settings
from app.kb.service import split_markdown
from app.llm.client import LLMClient
from app.llm.providers import OpenAICompatibleProvider
from app.llm.types import LLMFatalError
from app.mastery.service import apply_score, ema, next_interval, pick
from app.retrieval.rrf import rrf
from app.tools.sandbox import Sandbox

FIX = Path(__file__).parent / "fixtures" / "mini-feed"


@pytest.fixture()
def sandbox(tmp_path: Path) -> Sandbox:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "A.java").write_text("\n".join(f"line {i} foo()" for i in range(1, 501)))
    (root / ".env").write_text("SECRET=abc")
    (root / "key.pem").write_text("-----BEGIN")
    (tmp_path / "outside.txt").write_text("outside secret")
    (root / "sub").mkdir()
    return Sandbox(root)


async def test_sandbox_path_traversal(sandbox: Sandbox) -> None:
    assert "拒绝" in await sandbox.call("read_file", {"path": "../outside.txt"})
    assert "拒绝" in await sandbox.call("read_file", {"path": "/etc/passwd"})
    assert "拒绝" in await sandbox.call("list_dir", {"path": "sub/../.."})


async def test_sandbox_symlink_escape(sandbox: Sandbox, tmp_path: Path) -> None:
    # 单独成一个测试：Windows 没开"开发者模式"时普通用户不能建符号链接（WinError 1314），
    # 放在夹具里会连累上面几条测试一起报错。生产环境和 CI 都是 Linux，这条照常执行
    try:
        os.symlink(tmp_path / "outside.txt", tmp_path / "repo" / "link.txt")
    except OSError as e:
        pytest.skip(f"当前系统不能创建符号链接：{e}")
    assert "拒绝" in await sandbox.call("read_file", {"path": "link.txt"})  # 符号链接指向根目录外


async def test_sandbox_sensitive_files(sandbox: Sandbox) -> None:
    assert "拒绝" in await sandbox.call("read_file", {"path": ".env"})
    assert "拒绝" in await sandbox.call("read_file", {"path": "key.pem"})
    listing = await sandbox.call("list_dir", {"path": "."})
    assert ".env" not in listing and "key.pem" not in listing
    out = await sandbox.call("grep", {"pattern": "SECRET|BEGIN"})
    assert "abc" not in out and "BEGIN" not in out


async def test_sandbox_limits(sandbox: Sandbox) -> None:
    out = await sandbox.read_file("A.java", 1, 1000)
    assert "300: line 300" in out and "301: line 301" not in out
    g = await sandbox.grep("foo")
    assert "只显示前 100 条" in g
    assert len(g.splitlines()) == 101


def test_evidence_check() -> None:
    sb = Sandbox(FIX)
    path = "src/main/java/com/example/feed/service/impl/FeedServiceImpl.java"
    assert evidence.check(sb, [{"path": path, "start_line": 68, "end_line": 71}], ["saveLikeCount"]) is None
    assert "不存在" in (evidence.check(sb, [{"path": path, "start_line": 68, "end_line": 120}], ["x"]) or "")
    assert "没有出现" in (evidence.check(sb, [{"path": path, "start_line": 1, "end_line": 3}], ["saveLikeCount"]) or "")
    assert "不存在" in (evidence.check(sb, [{"path": "nope.java", "start_line": 1, "end_line": 1}], []) or "")


def test_rrf() -> None:
    fused = rrf([["a", "b", "c"], ["b", "c", "d"]], k=60)
    assert [k for k, _ in fused][:2] == ["b", "c"]
    assert fused[0][1] == pytest.approx(1 / 62 + 1 / 61)


def test_mastery_schedule() -> None:
    assert ema(None, 40) == 40 and ema(40, 80) == 60
    assert next_interval(50, 0) == timedelta(days=1)
    assert next_interval(70, 0) == timedelta(days=3)
    assert next_interval(90, 1) == timedelta(days=7)
    assert next_interval(90, 2) == timedelta(days=21)
    now = datetime.now(UTC)
    v1 = apply_score(None, 90, now, "s1")
    assert v1["high_streak"] == 1 and v1["next_review_at"] == now + timedelta(days=7)

    class M:
        score_ema, attempts, high_streak, history, title = v1["score_ema"], 1, 1, v1["history"], None

    v2 = apply_score(M(), 95, now, "s2")  # type: ignore[arg-type]
    assert v2["high_streak"] == 2 and v2["next_review_at"] == now + timedelta(days=21)


def test_pick_prefers_weak_and_due() -> None:
    now = datetime.now(UTC)

    class M:
        def __init__(self, score: float, due: bool) -> None:
            self.score_ema, self.attempts = score, 1
            self.next_review_at = now - timedelta(hours=1) if due else now + timedelta(days=5)

    items = [{"topic_key": k, "kind": "mechanism", "importance": 3} for k in ("good", "weak", "new", "due")]
    ms = {"good": M(95, False), "weak": M(30, False), "due": M(70, True)}
    chosen = [c["topic_key"] for c in pick(items, 3, ms, "medium", now)]  # type: ignore[arg-type]
    assert set(chosen[:2]) == {"weak", "due"} and "good" not in chosen


def test_hint_leak_detection() -> None:
    kps = [{"point": "点赞计数先写入 Redis 的 ZSet，再由定时任务 CountFlushJob 批量刷回数据库", "evidence": []}]
    assert leaks("答案是：点赞计数先写入 Redis 的 ZSet，再由定时任务 CountFlushJob 批量刷回数据库", kps)
    assert not leaks("想想计数最后是怎么写回数据库的？", kps)


def test_kb_split() -> None:
    md = (
        "# Redis\n\n## 数据结构\n\n"
        + "ZSet 是有序集合。" * 200
        + "\n\n### 跳表\n\n跳表很快。\n\n## 持久化\n\nRDB 和 AOF。\n"
    )
    chunks = split_markdown(md)
    heads = [h for h, _ in chunks]
    assert heads[0] == "Redis > 数据结构" and "Redis > 数据结构 > 跳表" in heads and heads[-1] == "Redis > 持久化"
    big = [c for h, c in chunks if h == "Redis > 数据结构"]
    assert len(big) >= 2 and all(len(c) <= 800 for c in big)
    assert big[0][-100:] == big[1][:100]  # 相邻块重叠 100 字


def _embed_client(returned_dim: int, requests: list[dict], **overrides: object) -> LLMClient:
    """接一个假的嵌入服务：记下每次请求体，返回 returned_dim 维的向量。不发真实请求。"""

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        requests.append({"path": req.url.path, **body})
        data = [
            {"object": "embedding", "index": i, "embedding": [0.1] * returned_dim} for i in range(len(body["input"]))
        ]
        return httpx.Response(200, json={"object": "list", "data": data, "model": body["model"], "usage": {}})

    conf: dict[str, object] = {
        "llm_provider": "openai_compatible",
        "embedding_base_url": "https://embedding.example/v1",
        "embedding_api_key": "test-key",
        "embedding_model": "text-embedding-v4",
        "embedding_dim": 1024,
        **overrides,
    }
    s = Settings(_env_file=None, **conf)  # type: ignore[call-arg, arg-type]  # 不读本机 .env，避免受真实配置影响
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMClient(s, OpenAICompatibleProvider(s, embed_http_client=http))


async def test_embed_request_matches_provider_limits() -> None:
    # 阿里云百炼单次最多 10 条：23 条要拆成 10 + 10 + 3；每次都带 dimensions 和 encoding_format=float
    reqs: list[dict] = []
    vecs = await _embed_client(1024, reqs).embed([f"text {i}" for i in range(23)])
    assert len(vecs) == 23 and all(len(v) == 1024 for v in vecs)
    assert [len(r["input"]) for r in reqs] == [10, 10, 3]
    assert all(r["path"] == "/v1/embeddings" and r["model"] == "text-embedding-v4" for r in reqs)
    assert all(r["dimensions"] == 1024 and r["encoding_format"] == "float" for r in reqs)


async def test_embed_without_dimensions_and_dim_mismatch() -> None:
    # 维度固定的模型：关掉 dimensions 参数；服务商返回的维度和 EMBEDDING_DIM 不一致时要报清楚原因
    reqs: list[dict] = []
    client = _embed_client(4096, reqs, embedding_send_dimensions=False, embedding_batch_size=32)
    with pytest.raises(LLMFatalError, match="4096 维.*EMBEDDING_DIM=1024"):
        await client.embed(["a", "b"])
    assert len(reqs) == 1 and "dimensions" not in reqs[0]


async def test_embed_requires_configured_model() -> None:
    # 对话模型接了真实服务、嵌入模型还是默认的假模型：直接报"没配置"，不把请求发给对话模型的服务商
    reqs: list[dict] = []
    with pytest.raises(LLMFatalError, match="还没有配置嵌入模型"):
        await _embed_client(1024, reqs, embedding_model="fake-embedding").embed(["a"])
    assert reqs == []


def test_embedding_dim_limit() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, embedding_dim=4096)  # type: ignore[call-arg]
