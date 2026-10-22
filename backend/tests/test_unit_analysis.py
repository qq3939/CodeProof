"""单元测试：tree-sitter 抽取、Spring 画像、链路、候选考点、URL 规范化、diff 解析。"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.agents.change import affected_symbols, parse_diff
from app.analysis.candidates import build_candidates
from app.analysis.code_model import CodeModel, FileEntry
from app.analysis.java_index import parse_java
from app.analysis.spring_profile import TextFile, build_profile, parse_config_keys
from app.core.errors import AppError
from app.repos.url import normalize_repo_url

FIX = Path(__file__).parent / "fixtures" / "mini-feed"


def _model() -> tuple[CodeModel, dict[str, str]]:
    files, src = [], {}
    for p in sorted(FIX.rglob("*.java")):
        rel = str(p.relative_to(FIX))
        src[rel] = p.read_text()
        files.append(FileEntry(id=uuid.uuid4(), path=rel, index=parse_java(p.read_bytes())))
    return CodeModel(files), src


def _profile() -> tuple[CodeModel, dict]:
    m, src = _model()
    prof = build_profile(
        m,
        [TextFile("pom.xml", (FIX / "pom.xml").read_text())],
        [TextFile("src/main/resources/application.yml", (FIX / "src/main/resources/application.yml").read_text())],
        src,
        {"java": len(src)},
    )
    return m, prof


def test_java_symbols_and_annotations() -> None:
    idx = parse_java((FIX / "src/main/java/com/example/feed/service/impl/FeedServiceImpl.java").read_bytes())
    assert idx.package == "com.example.feed.service.impl"
    names = {s.name: s for s in idx.symbols if s.kind != "constructor"}
    assert names["FeedServiceImpl"].kind == "class"
    assert names["FeedServiceImpl"].supertypes == ["FeedService"]
    tx = names["saveLikeCount"]
    assert tx.kind == "method"
    assert [(a.name, "REQUIRES_NEW" in a.args) for a in tx.annotations] == [("Transactional", True)]
    assert tx.start_line == 68 and tx.end_line == 71
    # 字段类型用于解析接收者：redissonClient.getLock → RedissonClient
    calls = {(c.name, c.receiver_type) for c in idx.calls}
    assert ("getLock", "RedissonClient") in calls
    assert ("updateLikeCount", "FeedMapper") in calls


def test_record_and_inner_and_sync() -> None:
    idx = parse_java(b"""package a;
public record P(Long id) {}
class Outer { class Inner { synchronized void f() { synchronized(this) { g(); } } void g() {} } }
""")
    kinds = {(s.kind, s.qualified_name) for s in idx.symbols}
    assert ("record", "a.P") in kinds
    assert ("class", "a.Outer.Inner") in kinds
    assert idx.sync_blocks and idx.sync_blocks[0][1] == 3


def test_call_resolution_interface_to_impl() -> None:
    m, _ = _model()
    like = next(s for s in m.symbols if s.short == "FeedController.like")
    targets = [t.short for rc in m.calls_from(like) for t in rc.targets]
    assert "FeedService.like" in targets
    impl = [x.short for t in (t for rc in m.calls_from(like) for t in rc.targets) for x in m.expand_impl(t)]
    assert "FeedServiceImpl.like" in impl


def test_profile_endpoints_usages_chains() -> None:
    _, prof = _profile()
    eps = {(e["method"], e["path"]) for e in prof["endpoints"]}
    assert eps == {("GET", "/api/feed"), ("GET", "/api/feed/{id}"), ("POST", "/api/feed/{id}/like")}
    assert "Redis" in prof["stack"]["tech"] and "RabbitMQ" in prof["stack"]["tech"]
    u = prof["usages"]
    assert u["transaction"][0]["propagation"] == "REQUIRES_NEW"
    assert {c["annotation"] for c in u["cache"]} == {"Cacheable", "CacheEvict"}
    assert any(x["kind"] == "distributed_lock" for x in u["concurrency"])
    assert any(x["kind"] == "synchronized_block" for x in u["concurrency"])
    assert any(x["kind"] == "thread_pool" for x in u["concurrency"])
    assert any(x["kind"] == "listener" for x in u["mq"]) and any(x["kind"] == "producer" for x in u["mq"])
    like_chain = next(c for c in prof["chains"] if c["endpoint"] == "POST /api/feed/{id}/like")
    symbols = [s["symbol"] for s in like_chain["steps"]]
    assert symbols[0] == "FeedController.like" and "FeedServiceImpl.like" in symbols
    assert any(s["layer"] == "middleware" for s in like_chain["steps"])
    detail = next(c for c in prof["chains"] if c["endpoint"] == "GET /api/feed/{id}")
    assert detail["steps"][-1]["layer"] == "data"


def test_config_values_redacted() -> None:
    keys = parse_config_keys([TextFile("application.yml", (FIX / "src/main/resources/application.yml").read_text())])
    assert {"key": "spring.datasource.password", "value": "***"}.items() <= next(
        k for k in keys if k["key"] == "spring.datasource.password"
    ).items()
    assert all(k["value"] == "***" for k in keys)
    _, prof = _profile()
    assert "super-secret-password" not in str(prof)


def test_candidates_find_self_invocation_risk() -> None:
    m, prof = _profile()
    cands = build_candidates(m, prof)
    keys = {c["topic_key"] for c in cands}
    assert "risk.self-invoke-async:LikeListener.notifyAuthor" in keys
    assert "chain:POST /api/feed/{id}/like" in keys
    assert "design.redis-batch-flush" in keys
    for c in cands:
        for ev in c["evidence"]:
            assert ev["end_line"] - ev["start_line"] < 40


@pytest.mark.parametrize(
    "raw,url",
    [
        (
            "https://github.com/Spring-Projects/Spring-Petclinic.git",
            "https://github.com/spring-projects/spring-petclinic",
        ),
        ("git@github.com:A/B.git", "https://github.com/a/b"),
        ("github.com/a/b/", "https://github.com/a/b"),
        ("http://github.com/a/b", "https://github.com/a/b"),
    ],
)
def test_normalize_url(raw: str, url: str) -> None:
    assert normalize_repo_url(raw).url == url


def test_normalize_pr_and_reject() -> None:
    r = normalize_repo_url("https://github.com/a/b/pull/12/files")
    assert r.url == "https://github.com/a/b" and r.pr_number == 12
    with pytest.raises(AppError):
        normalize_repo_url("file:///etc")
    with pytest.raises(AppError):
        normalize_repo_url("not a url")


DIFF = """diff --git a/src/A.java b/src/A.java
index 1..2 100644
--- a/src/A.java
+++ b/src/A.java
@@ -1,4 +1,5 @@
 class A {
-  void f() { }
+  void f() {
+    g();
+  }
   void g() { }
 }
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+hello
"""


def test_parse_diff_and_affected() -> None:
    files = parse_diff(DIFF)
    assert [f.path for f in files] == ["src/A.java", "new.txt"]
    assert files[0].added_lines == [2, 3, 4] and files[0].hunks[0].removed == [2]
    assert files[1].status == "added"
    src = b"class A {\n  void f() {\n    g();\n  }\n  void g() { }\n}\n"
    names = [s["name"] for s in affected_symbols(files[0], src)]
    assert names == ["f"]
