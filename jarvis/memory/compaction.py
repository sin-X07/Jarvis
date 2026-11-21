"""上下文压缩（P3-4）：上下文快满时，把旧轮次摘要成一段。

为什么不能只裁剪：memory.py 的 trim 是直接删旧轮次，删了就没了。
压缩是把旧轮次“浓缩”后留下来——模型还记得早期关键事实，对话不断。

规则（和 trim 一样严格）：
- 只按“整轮”切：一条 user 消息 + 它后面跟着的 assistant/tool 消息
  算一轮，整轮一起摘要，绝不拆散 tool 调用链。
- system 消息永不动；最近 keep_turns 轮永不动。
- 摘要本身是一次模型调用（summarizer），也可以传假的 summarizer
  做测试。摘要写成一条 user 消息“【此前对话摘要】…”，追加进
  会话日志当 summary 事件，压缩前不需要额外备份——原事件还在
  JSONL 里，只是本次调用不再发给模型。

触发：调用方在每轮循环前算一下 estimate_tokens，超过
threshold（默认 70% 上下文窗口）就调 maybe_compact。
"""

from jarvis.core.usage import estimate_tokens


def estimate_messages_tokens(messages: list) -> int:
    """估算整个消息列表的 token 数（内容 + 工具参数都算）。"""
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        total += estimate_tokens(str(msg.get("content") or ""))
        for call in msg.get("tool_calls") or []:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            total += estimate_tokens(str(fn.get("arguments") or ""))
    return total


def _turn_boundaries(messages: list) -> list:
    """返回每轮的起始下标：system 跳过，每条 user 消息开一轮。"""
    starts = []
    for i, msg in enumerate(messages):
        if i > 0 and isinstance(msg, dict) and msg.get("role") == "user":
            # 摘要消息不算新轮的开始（它是上一轮的替代品）
            content = str(msg.get("content") or "")
            if content.startswith("【此前对话摘要】"):
                continue
            starts.append(i)
    return starts


def summarize_with_model(client, model: str, messages: list) -> str:
    """调一次模型，把一段旧消息摘要成几句话（只保留事实、决定、待办）。"""
    from jarvis.core.model import call_model

    prompt = ("把下面这段对话摘要成 5-10 行中文要点，只保留：用户说过的"
              "事实与偏好、做过的决定、没做完的事。不要复述寒暄。\n\n")
    text = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = str(msg.get("content") or "")
        if role == "tool":
            content = "工具返回：" + content[:300]
        if content:
            text.append(role + "：" + content)
    summary_messages = [
        {"role": "system", "content": "你是一个对话摘要助手。"},
        {"role": "user", "content": prompt + "\n".join(text)},
    ]
    reply, _usage = call_model(client, model, summary_messages, tools=[])
    return (reply.get("content") or "").strip() or "(这段对话没有可摘要的内容)"


def compact(messages: list, summarizer, keep_turns: int = 4):
    """压缩一次：返回 (new_messages, summary_text)。

    把除最近 keep_turns 轮之外的旧轮次摘要掉；可摘要的轮次不足
    1 轮时原样返回，summary 为空字符串。
    """
    starts = _turn_boundaries(messages)
    if len(starts) <= keep_turns:
        return messages, ""
    cut = starts[len(starts) - keep_turns]
    if cut <= 1:  # 只剩 system + 不足一轮可压
        return messages, ""
    old = messages[1:cut]
    summary = summarizer(old)
    summary_msg = {"role": "user",
                   "content": "【此前对话摘要】\n" + summary}
    return [messages[0], summary_msg] + messages[cut:], summary


def maybe_compact(messages: list, summarizer, limit_tokens: int,
                  threshold: float = 0.7, keep_turns: int = 4):
    """用量超过阈值才压缩；返回 (new_messages, did_compact, summary)。"""
    if estimate_messages_tokens(messages) <= limit_tokens * threshold:
        return messages, False, ""
    new_messages, summary = compact(messages, summarizer, keep_turns)
    return new_messages, bool(summary), summary
