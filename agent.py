"""Jarvis starter: a minimal agent loop driven by DeepSeek.

This is the whole "agent" in ~80 lines:

    user message
        -> model (with tool schemas)
        -> model either answers, or asks to call a tool
        -> run the tool, feed the result back
        -> repeat until the model answers, or MAX_STEPS runs out

That loop -- think, act, observe, repeat -- is the agent loop.
Everything else in a "personal agent" (memory, planning, a harness
around the loop) is built on top of it.

Model name note (verified 2026-11-21):
    DeepSeek's official API serves DeepSeek-V4.1-Flash-1017 under the
    model id `deepseek-flash`. The old id `deepseek-v4-flash` still works
    but routes to the same weights. Do NOT send `deepseek-v4.1-1017` --
    that is the weights release name, not an API model id.
"""

import json
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

from tools import TOOLS, TOOL_FUNCS

load_dotenv()

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "10"))

SYSTEM_PROMPT = (
    "你是 Jarvis，一个个人助手。你可以用工具获取信息、做计算、读文件。"
    "遇到需要事实或计算的问题，先调用工具，不要凭空编造数字和时间。"
    "回答用中文，简洁直接。"
)


def build_client() -> OpenAI:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit(
            "未找到 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入你的 key，"
            "或 export DEEPSEEK_API_KEY=sk-..."
        )
    return OpenAI(api_key=api_key, base_url=BASE_URL)


def run_agent(client, user_message: str, model: str = MODEL,
              max_steps: int = MAX_STEPS, verbose: bool = True) -> str:
    """Run one agent loop. Returns the model's final answer as text."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    for step in range(1, max_steps + 1):
        response = client.chat.completions.create(
            model=model, messages=messages, tools=TOOLS, tool_choice="auto",
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            if verbose:
                print(f"[step {step}] 模型直接回答，循环结束")
            return msg.content or "(空回答)"

        # The model wants to act: run each requested tool, feed results back.
        messages.append(msg)
        for call in msg.tool_calls:
            name = call.function.name
            args = json.loads(call.function.arguments or "{}")
            if verbose:
                print(f"[step {step}] 调用工具 {name}({args})")
            func = TOOL_FUNCS.get(name)
            result = func(**args) if func else f"未知工具: {name}"
            if verbose:
                print(f"[step {step}] 工具返回: {str(result)[:200]}")
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": str(result),
            })

    return "达到最大步数，任务未完成。"


def main() -> None:
    if len(sys.argv) < 2:
        print('用法: python agent.py "你的问题"')
        print('示例: python agent.py "现在几点？12*(3+4) 等于多少？"')
        raise SystemExit(1)
    client = build_client()
    print(f"模型: {MODEL} @ {BASE_URL}")
    answer = run_agent(client, " ".join(sys.argv[1:]))
    print("\n--- Jarvis ---")
    print(answer)


if __name__ == "__main__":
    main()
