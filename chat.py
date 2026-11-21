"""Jarvis 多轮对话入口：带记忆的聊天。

和 agent.py 的区别：agent.py 是一问一答（每次都是全新的上下文），
这里是持续对话——历史一直攒在 ConversationMemory 里，长期事实
存在 memory.json，模型每轮都能看到。

用法：
    python chat.py

命令（以 / 开头）：
    /记住 <内容>   把一条事实存进长期记忆，例如：/记住 我最喜欢的颜色是蓝色
    /记忆          列出长期记忆里的全部事实
    /清空          清空本轮对话历史（长期记忆不受影响）
    /退出          退出（exit / quit 也行）

也可以不用命令，直接在对话里说“记住，我最喜欢蓝色”——模型会自己
调 remember_fact 工具记下来。
"""

import sys

from agent import SYSTEM_PROMPT, build_client, run_agent
from memory import ConversationMemory, FactStore, memory_tools


def build_system_prompt(store: FactStore) -> str:
    """system prompt = 基础人设 + 当前长期记忆。每次调用前都重新拼一遍，
    这样本轮新记住的事实，下一轮模型立刻就能看到。"""
    facts = store.render_for_prompt()
    return f"{SYSTEM_PROMPT}\n{facts}" if facts else SYSTEM_PROMPT


def main() -> None:
    client = build_client()
    store = FactStore()
    memory = ConversationMemory(build_system_prompt(store))
    schemas, funcs = memory_tools(store)

    print("Jarvis 已启动（多轮对话 + 长期记忆）。输入 /退出 结束。")
    print("命令：/记住 <内容>  /记忆  /清空  /退出")
    if store.all_texts():
        print(f"已载入 {len(store.all_texts())} 条长期记忆。")

    while True:
        try:
            text = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。")
            break
        if not text:
            continue

        if text in ("/退出", "/quit", "exit", "quit"):
            print("再见。")
            break
        if text.startswith("/记住"):
            fact = text[len("/记住"):].strip()
            print(store.add(fact))
            continue
        if text == "/记忆":
            facts = store.all_texts()
            print("长期记忆：" if facts else "长期记忆还是空的。")
            for fact in facts:
                print(f"- {fact}")
            continue
        if text == "/清空":
            memory.clear()
            print("本轮对话已清空，长期记忆还在。")
            continue
        if text.startswith("/"):
            print(f"未知命令：{text}，可用：/记住 /记忆 /清空 /退出")
            continue

        # 长期记忆可能刚变过，刷新 system prompt 再跑循环
        memory.messages[0] = {
            "role": "system",
            "content": build_system_prompt(store),
        }
        answer = run_agent(
            client, text, history=memory.messages,
            extra_tools=schemas, extra_funcs=funcs, verbose=False,
        )
        memory.trim()
        print(f"\nJarvis：{answer}")


if __name__ == "__main__":
    main()
