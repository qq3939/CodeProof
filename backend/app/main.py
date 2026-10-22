"""FastAPI 入口：uv run uvicorn app.main:app --reload"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.errors import install_error_handlers
from app.core.logging import log_extra, request_id_var, setup_logging
from app.core.ratelimit import hit
from app.core.redis import get_redis
from app.core.security import origin_allowed

logger = logging.getLogger("app.access")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_logging(get_settings().log_level)
    from app.sessions.graph import close_checkpointer, setup_checkpointer

    await setup_checkpointer()
    yield
    await close_checkpointer()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="project-b", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json"
    )
    install_error_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request_id_var.set(rid)
        t0 = time.perf_counter()
        # CSRF：修改数据的请求校验 Origin（SPEC 9.7）
        if request.method in ("POST", "PUT", "DELETE", "PATCH") and not origin_allowed(
            request.headers.get("origin"), request.headers.get("host")
        ):
            return JSONResponse(
                status_code=403, content={"error": {"code": "bad_origin", "message": "请求来源不被允许"}}
            )
        # 限流：按 IP（登录后再按用户，见 deps）
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            ip = request.client.host if request.client else "unknown"
            try:
                ok = await hit(f"ip:{ip}", s.rate_limit_per_minute)
            except Exception:  # noqa: BLE001 —— Redis 挂了不应该让所有接口都不可用
                ok = True
            if not ok:
                return JSONResponse(
                    status_code=429,
                    content={"error": {"code": "too_many_requests", "message": "请求太频繁，请稍后再试"}},
                )
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        logger.info(
            "request",
            extra=log_extra(
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                ms=int((time.perf_counter() - t0) * 1000),
            ),
        )
        return response

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        """健康检查：数据库和 Redis 都通才算健康（Compose healthcheck 用）。"""
        async with get_engine().connect() as conn:
            await conn.execute(text("select 1"))
        await get_redis().ping()
        return {"status": "ok"}

    from app.auth.router import router as auth_router
    from app.kb.router import router as kb_router
    from app.mastery.router import router as mastery_router
    from app.ops.router import router as ops_router
    from app.reports.router import router as reports_router
    from app.repos.router import router as repos_router
    from app.resume.router import router as resume_router
    from app.sessions.router import router as sessions_router

    for r in (
        auth_router,
        repos_router,
        sessions_router,
        reports_router,
        mastery_router,
        resume_router,
        kb_router,
        ops_router,
    ):
        app.include_router(r, prefix="/api")
    return app


app = create_app()
