import asyncio
import sys

if sys.platform == "win32":
    # psycopg 的异步模式（LangGraph 检查点在用）不支持 Windows 默认的 ProactorEventLoop，
    # 在 Windows 本机跑测试或 worker 时换成 SelectorEventLoop（P-014）。容器里是 Linux，这段不执行
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
