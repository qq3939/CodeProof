import time

from langchain_openai import ChatOpenAI

MAX_RETRIES = 3
_cost = {"tokens": 0}


def get_chat_model() -> ChatOpenAI:
    # 超时交给 SDK，重试在 call_with_retry 里统一做，避免两层都重试
    return ChatOpenAI(model="gpt-4o-mini", timeout=30, max_retries=0)


def call_with_retry(fn, *args):
    delay = 0.5
    for attempt in range(MAX_RETRIES):
        try:
            resp = fn(*args)
            _cost["tokens"] += resp.usage_metadata.get("total_tokens", 0)
            return resp
        except TimeoutError:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(delay)
            delay *= 2
