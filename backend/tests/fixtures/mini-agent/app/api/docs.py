from fastapi import APIRouter, Depends, UploadFile

from app.api.deps import get_db
from app.rag import retriever

router = APIRouter(prefix="/docs")


@router.post("/upload")
async def upload(file: UploadFile, db=Depends(get_db)) -> dict:
    text = (await file.read()).decode("utf-8")
    n = retriever.ingest(db, file.filename, text)
    return {"chunks": n}
