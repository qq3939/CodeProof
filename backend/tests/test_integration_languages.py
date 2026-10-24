"""集成测试：Java 以外的语言（SPEC 7.3、7.4，D-021、D-022）。

- Go 没有专门的符号索引和画像：v1 会显示"分析完成"却一道题都出不了。
  现在要么由通用兜底出题并标明"通用模式"，要么分析失败并写明原因；
- Python 有专门的符号索引和 FastAPI 画像：按接口链路出题，不走通用模式。
"""

from __future__ import annotations

from pathlib import Path

import httpx

from app.core.queue import drain
from tests.conftest import _TMP, login, make_repo

FIXTURES = Path(__file__).parent / "fixtures"


async def _submit(client: httpx.AsyncClient, repo: Path) -> dict:
    r = await client.post("/api/repos", json={"url": f"file://{repo}"})
    assert r.status_code == 200, r.text
    await drain()
    return (await client.get(f"/api/snapshots/{r.json()['snapshot_id']}")).json()


async def test_generic_mode_go_repo(app_ready: None) -> None:
    # 换一个用户，免得和其他测试共用"进行中的会话"上限
    client = await login(None, "gopher")
    try:
        snap = await _submit(client, make_repo("mini-go", FIXTURES / "mini-go"))
        assert snap["status"] == "ready", snap
        assert snap["stats"]["analysis_mode"] == "generic" and snap["stats"]["questions"] > 0

        prof = (await client.get(f"/api/snapshots/{snap['id']}/profile")).json()
        assert prof["profile"]["analysis_mode"] == "generic"
        assert prof["profile"]["primary_language"] == "go"
        assert "通用模式" in prof["summary_md"]

        r = await client.post(
            "/api/sessions", json={"snapshot_id": snap["id"], "mode": "deep_dive", "question_count": 2}
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["turns"][0]["role"] == "examiner" and data["state"]["awaiting_answer"]
        # 出题计划带上主语言，面试官的提示词按它填写（"Go 技术面试官"）
        from app.core.db import session_scope
        from app.models import ExamSession

        async with session_scope() as db:
            sess = await db.get(ExamSession, __import__("uuid").UUID(data["session"]["id"]))
            assert sess is not None and {p["lang"] for p in sess.config["plan"]} == {"go"}
        await client.post(f"/api/sessions/{data['session']['id']}/finish")
        await drain()
    finally:
        await client.aclose()


async def test_no_questions_fails_with_reason(app_ready: None) -> None:
    # 只有 README 的仓库：通用兜底也找不到考点。v1 显示"分析完成"、题库为空；现在要失败并说明原因
    src = _TMP / "fixtures-readme-only"
    src.mkdir(parents=True, exist_ok=True)
    (src / "README.md").write_text("# notes\n\n只有一份说明文档，没有代码。\n", encoding="utf-8")
    client = await login(None, "reader")
    try:
        snap = await _submit(client, make_repo("readme-only", src))
        assert snap["status"] == "failed", snap
        assert "没能生成题目" in (snap["error"] or "")
    finally:
        await client.aclose()


async def test_python_repo_uses_profile(app_ready: None) -> None:
    client = await login(None, "pythonista")
    try:
        snap = await _submit(client, make_repo("mini-agent", FIXTURES / "mini-agent"))
        assert snap["status"] == "ready", snap
        st = snap["stats"]
        assert st["analysis_mode"] == "profile" and st["primary_language"] == "python"
        assert st["python_files"] > 0 and st["questions"] > 0

        prof = (await client.get(f"/api/snapshots/{snap['id']}/profile")).json()["profile"]
        assert "POST /api/chat/ask" in {f"{e['method']} {e['path']}" for e in prof["endpoints"]}
        # 符号表里有 Python 的函数和方法，代码浏览、面试官查代码都能用
        r = await client.post(
            "/api/sessions", json={"snapshot_id": snap["id"], "mode": "deep_dive", "question_count": 1}
        )
        assert r.status_code == 200, r.text
        await client.post(f"/api/sessions/{r.json()['session']['id']}/finish")
        await drain()
    finally:
        await client.aclose()
