from fastapi import Header, HTTPException

from app.db import SessionLocal


def get_db():
    # 每个请求一个数据库会话，请求结束就关掉
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def verify_token(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing token")
    return authorization.removeprefix("Bearer ")
