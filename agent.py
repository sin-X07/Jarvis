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


def build_client(api_key=None) -> OpenAI:
    """Build the DeepSeek client. A caller-provided key wins (web BYOK);
    otherwise fall back to DEEPSEEK_API_KEY from the environment."""
    api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit(
            "未找到 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入你的 key，"
            "或 export DEEPSEEK_API_KEY=sk-..."
        )
    return OpenAI(api_key=api_key, base_url=BASE_URL)


def _message_to_dict(msg) -> dict:
    """把一次模型回复统一成 dict（历史里只存 dict，两种调用模式通用）。

    真实 SDK 返回的是 ChatCompletionMessage（pydantic 对象），流式拼出来
    的和测试用的假消息是 SimpleNamespace，这里只取 API 需要的三个字段
    （role/content/tool_calls），不多带 SDK 的多余字段。
    """
    if isinstance(msg, dict):
        return msg
    content = getattr(msg, "content", None)
    tool_calls = None
    raw_calls = getattr(msg, "tool_calls", None)
    if raw_calls:
        tool_calls = []
        for call in raw_calls:
            if isinstance(call, dict):
                fn = call.get("function", {})
                tool_calls.append({
                    "id": call.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}") or "{}",
                    },
                })
                continue
            func = getattr(call, "function", None)
            if isinstance(func, dict):
                name, args = func.get("name", ""), func.get("arguments", "{}") or "{}"
            else:
                name = getattr(func, "name", "")
                args = getattr(func, "arguments", "{}") or "{}"
            tool_calls.append({
                "id": getattr(call, "id", ""),
                "type": "function",
                "function": {"name": name, "arguments": args},
            })
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def _accumulate_stream(chunks, on_token) -> dict:
    """把流式返回的碎片拼成一条完整消息。

    流式模式下，一段文字、一次工具调用都可能拆成多个 chunk 到达：
    文本按顺序拼，每到一段就调 on_token 发出去；工具调用按 index 归位，
    id/name 只取第一次出现的，arguments 碎片按顺序拼接。
    """
    content_parts = []
    tool_calls = {}
    for chunk in chunks:
        delta = chunk.choices[0].delta
        text = getattr(delta, "content", None)
        if text:
            content_parts.append(text)
            on_token(text)
        for call in getattr(delta, "tool_calls", None) or []:
            slot = tool_calls.setdefault(
                call.index, {"id": "", "name": "", "arguments": ""})
            if getattr(call, "id", None):
                slot["id"] = call.id
            func = getattr(call, "function", None)
            if func is not None:
                if getattr(func, "name", None):
                    slot["name"] = func.name
                if getattr(func, "arguments", None):
                    slot["arguments"] += func.arguments
    calls = None
    if tool_calls:
        calls = [{
            "id": slot["id"],
            "type": "function",
            "function": {
                "name": slot["name"],
                "arguments": slot["arguments"] or "{}",
            },
        } for _, slot in sorted(tool_calls.items())]
    return {
        "role": "assistant",
        "content": "".join(content_parts) or None,
        "tool_calls": calls,
    }


def _call_model(client, model, messages, tools, stream, on_token) -> dict:
    """调一次模型，返回 dict 形式的消息。stream=True 时边收边发 token 事件。"""
    if not stream:
        response = client.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice="auto",
        )
        return _message_to_dict(response.choices[0].message)
    chunks = client.chat.completions.create(
        model=model, messages=messages, tools=tools, tool_choice="auto",
        stream=True,
    )
    return _accumulate_stream(chunks, on_token)


def run_agent(client, user_message: str, model: str = MODEL,
              max_steps: int = MAX_STEPS, verbose: bool = True,
              history: list | None = None,
              extra_tools: list | None = None,
              extra_funcs: dict | None = None,
              on_event=None, stream: bool = False) -> str:
    """Run one agent loop. Returns the model's final answer as text.

    history: 传一个 ConversationMemory.messages 列表进来，就变成多轮对话
        —— 本轮的问题追加在历史末尾，循环里产生的消息直接写进这个列表，
        下次调用还接着用。不传则和原来一样：每次都是全新的一问一答。
    extra_tools / extra_funcs: 追加的工具（比如记忆工具），和 tools.py
        里原有的三个合并后一起交给模型。
    on_event: 可选回调 on_event(kind, payload)，循环里有进展就调它：
        ("token", text) 流式吐出的每段文字；("tool_call", {...}) 调工具前；
        ("tool_result", {...}) 工具返回后。不传时退化成 verbose 打印。
    stream: True 时用流式调用，回答边生成边通过 ("token", ...) 事件发出。
        历史里存的消息统一是 dict，两种模式都一样。
    """
    if history is None:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
    else:
        messages = history
        messages.append({"role": "user", "content": user_message})
    tools = list(TOOLS) + list(extra_tools or [])
    funcs = dict(TOOL_FUNCS)
    funcs.update(extra_funcs or {})

    def report(kind, payload=None):
        if on_event is not None:
            on_event(kind, payload)
        elif verbose:
            if kind == "tool_call":
                print(f"[step {payload['step']}] 调用工具 {payload['name']}({payload['args']})")
            elif kind == "tool_result":
                print(f"[step {payload['step']}] 工具返回: {str(payload['result'])[:200]}")
            elif kind == "direct":
                print(f"[step {payload['step']}] 模型直接回答，循环结束")

    for step in range(1, max_steps + 1):
        msg = _call_model(client, model, messages, tools, stream,
                          lambda text: report("token", text))
        if not msg.get("tool_calls"):
            report("direct", {"step": step})
            # 多轮对话时，模型的回答也要留进历史，否则下次它就看不到自己说过什么
            messages.append(msg)
            return msg.get("content") or "(空回答)"

        # The model wants to act: run each requested tool, feed results back.
        messages.append(msg)
        for call in msg["tool_calls"]:
            call_id = call["id"]
            name = call["function"]["name"]
            args = json.loads(call["function"]["arguments"] or "{}")
            report("tool_call", {"step": step, "name": name, "args": args})
            func = funcs.get(name)
            result = func(**args) if func else f"未知工具: {name}"
            report("tool_result", {"step": step, "name": name, "result": str(result)})
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
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
