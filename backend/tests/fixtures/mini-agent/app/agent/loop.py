from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from app.agent.tools import TOOL_MAP, TOOLS
from app.llm import call_with_retry

MAX_STEPS = 6
SYSTEM_PROMPT = "你是一个文档问答助手，只根据工具返回的内容回答，找不到就说不知道。"


class ChatAgent:
    def __init__(self, llm, db) -> None:
        self.llm = llm
        self.db = db

    async def run(self, question: str) -> str:
        messages = [SystemMessage(SYSTEM_PROMPT), HumanMessage(question)]
        model = self.llm.bind_tools(TOOLS)
        # 最多走 MAX_STEPS 步：模型一直要求调工具时不能无限循环
        for _ in range(MAX_STEPS):
            resp = call_with_retry(model.invoke, messages)
            messages.append(resp)
            if not resp.tool_calls:
                return resp.content
            for call in resp.tool_calls:
                out = self.run_tool(call["name"], call["args"])
                messages.append(ToolMessage(content=out, tool_call_id=call["id"]))
        return "步数用完了，没能得出答案。"

    def run_tool(self, name: str, args: dict) -> str:
        try:
            return TOOL_MAP[name].invoke(args)
        except Exception as e:
            return f"工具出错：{e}"

    def summarize(self, url: str) -> str:
        # 工具抓回来的网页原文直接拼进系统提示词：网页里写一句"忽略之前的指令"就能改掉助手的行为
        prompt = SYSTEM_PROMPT + "\n参考资料：" + fetch_page(url)
        return self.llm.invoke(prompt).content


def fetch_page(url: str) -> str:
    return TOOL_MAP["fetch_url"].invoke({"url": url})


def poll_until_done(job) -> str:
    while True:
        status = job.refresh()
        if status == "done":
            return job.result
