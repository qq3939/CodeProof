# mini-agent

一个很小的 FastAPI + Agent 项目，只用来测试 CodeProof 的 Python 支持和 AI 应用画像，从不运行。

- `app/main.py`：FastAPI 入口，挂载两个路由器
- `app/api/`：问答接口和文档上传接口，用 `Depends` 做鉴权和数据库会话
- `app/agent/`：工具（`@tool`）、手写的 Agent 循环、LangGraph 状态图
- `app/rag/`：分块、嵌入、pgvector 检索、带引用的回答
- `app/mcp_server.py`：MCP Server，把问答暴露成工具
- `app/llm.py`：模型调用封装（超时、重试、记账）
