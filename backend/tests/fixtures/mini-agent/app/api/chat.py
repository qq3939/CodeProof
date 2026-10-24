from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.agent.loop import ChatAgent
from app.api.deps import get_db, verify_token
from app.llm import get_chat_model

router = APIRouter(prefix="/chat", dependencies=[Depends(verify_token)])


class AskIn(BaseModel):
    question: str


@router.post("/ask")
async def ask(body: AskIn, db=Depends(get_db)) -> dict:
    agent = ChatAgent(llm=get_chat_model(), db=db)
    answer = await agent.run(body.question)
    return {"answer": answer}
