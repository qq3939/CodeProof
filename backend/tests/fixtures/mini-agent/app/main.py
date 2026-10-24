from fastapi import FastAPI

from app.api import chat
from app.api.docs import router as docs_router

app = FastAPI(title="mini-agent")
app.include_router(chat.router, prefix="/api")
app.include_router(docs_router, prefix="/api")


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
