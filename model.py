"""模型适配层（P0-3 / 计划第 4 节“模型可换，默认 DeepSeek”）。

这一层是 Jarvis 和模型之间唯一的接触点：
- `build_client(api_key)`：建 DeepSeek 客户端（OpenAI 兼容接口）。
  调用方给的 key 优先，否则读环境变量 `DEEPSEEK_API_KEY`。
- `call_model(...)`：调一次模型，带重试（429 / 超时 / 5xx 时指数退避
  1s → 2s → 4s，最多按配置重试），并把用量（token 数）带回来。
  流式时把碎片拼成一条完整消息（逻辑沿用 agent.py 原来的拼法）。

以后要换模型（比如再接一个别的厂商），只改这一层：加一个
`build_xxx_client` 和对应的 `call_model` 分支，循环不用动。

用量说明：非流式调用直接读 API 返回的 usage；流式调用时 API 不一
定给 usage（取决于服务端是否支持 stream_options），这时按字符数
估算，并在 UsageTracker 里标成“估算”。费用永远只是估算，见 usage.py。
"""

import os
import time

from dotenv import load_dotenv
from openai import OpenAI

from config import load_config

load_dotenv()

# 模型名验证（2026-11-21 已核实）：API 用的 id 是 `deepseek-flash`；
# `deepseek-v4.1-1017` 是权重名，不是 API id，发过去会报错。
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

_RETRYABLE = ("RateLimit", "Timeout", "Connection", "APIError",
              "InternalServerError", "ServiceUnavailable")


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


def _is_retryable(exc: Exception) -> bool:
    """能不能重试：看异常类型名和状态码，不依赖 openai 的具体类，
    这样测试用的假 client 抛同名异常也能走重试。"""
    name = type(exc).__name__
    if any(key in name for key in _RETRYABLE):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    return status in (429, 500, 502, 503, 504)


def _accumulate_stream(chunks, on_token) -> dict:
    """把流式返回的碎片拼成一条完整消息。

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


def _message_to_dict(msg) -> dict:
    """把一次模型回复统一成 dict（历史里只存 dict，两种调用模式通用）。"""
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


def call_model(client, model, messages, tools, stream=False, on_token=None,
               timeout=None, retry_times=None, sleep=time.sleep):
    """调一次模型，返回 (message_dict, usage_dict)。

    usage_dict: {"prompt_tokens": int, "completion_tokens": int,
    "estimated": bool}；拿不到真实用量时（流式）按字符估算，
    estimated=True。重试耗尽后把最后一次异常原样抛给调用方。
    """
    cfg = load_config()
    timeout = cfg.timeout if timeout is None else timeout
    retry_times = cfg.retry_times if retry_times is None else retry_times
    on_token = on_token or (lambda text: None)

    last_exc = None
    for attempt in range(retry_times + 1):
        try:
            if not stream:
                response = client.chat.completions.create(
                    model=model, messages=messages, tools=tools,
                    tool_choice="auto", timeout=timeout,
                )
                msg = _message_to_dict(response.choices[0].message)
                raw = getattr(response, "usage", None)
                if raw is not None:
                    usage = {
                        "prompt_tokens": getattr(raw, "prompt_tokens", 0) or 0,
                        "completion_tokens": getattr(raw, "completion_tokens", 0) or 0,
                        "estimated": False,
                    }
                else:
                    usage = _estimate_usage(messages, msg)
                return msg, usage
            chunks = client.chat.completions.create(
                model=model, messages=messages, tools=tools,
                tool_choice="auto", stream=True, timeout=timeout,
            )
            msg = _accumulate_stream(chunks, on_token)
            return msg, _estimate_usage(messages, msg)
        except Exception as exc:  # noqa: BLE001 - 可重试的才重试，其余原样抛出
            last_exc = exc
            if attempt < retry_times and _is_retryable(exc):
                sleep(2 ** attempt)  # 1s → 2s → 4s
                continue
            raise
    raise last_exc  # pragma: no cover - 循环一定会 return 或 raise


def _estimate_usage(messages, msg) -> dict:
    """按字符数估算 token（流式拿不到 usage 时的 fallback）。

    估算规则见 usage.estimate_tokens：中文约 1 字 1 token，
    英文约 4 字符 1 token。标 estimated=True，费用只是估算。
    """
    from usage import estimate_tokens  # 延迟导入，避免循环引用

    prompt = sum(estimate_tokens(str(m.get("content") or ""))
                 for m in messages if isinstance(m, dict))
    completion = estimate_tokens(str(msg.get("content") or ""))
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "estimated": True}
