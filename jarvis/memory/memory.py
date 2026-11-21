"""Jarvis 记忆层：短期记忆（多轮对话）+ 长期记忆（事实）。

为什么记忆要分两层：

  短期记忆 = 这次对话里发生过什么。模型每次调用都要把整段对话
  历史发过去，它才能接上文。历史存在内存的 messages 列表里，
  但上下文窗口是有限的（deepseek-flash 是 1M token，听着大，
  对话一长、工具结果一多还是会满），所以要裁剪。

  长期记忆 = 跨对话、跨天还记得的事，比如“用户最喜欢的颜色是蓝色”。
  它存在磁盘的 memory.json 里，启动时读进来，拼进 system prompt，
  模型回答时就能参考。事实多了以后再升级成向量检索，现在先用
  最简单的关键词（bigram）匹配——够学原理，也够用。

注意：memory.json 里是你的个人事实，绝不能提交到公开仓库，
.gitignore 里已经把它排除掉了。
"""

import datetime
import json
import os

from jarvis import PROJECT_ROOT

DEFAULT_MAX_MESSAGES = 30  # 短期记忆上限（条消息数，含 system）


# ---------------------------------------------------------------------------
# 短期记忆：一轮对话的历史
# ---------------------------------------------------------------------------

def _role(message) -> str | None:
    """取一条消息的 role。历史里既有 dict，也有模型返回的原始 message 对象。"""
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


class ConversationMemory:
    """管住 messages 列表：只增不减太危险，满了就按“整轮”裁掉最旧的。

    关键规则：裁剪必须以“轮”为单位——一条 user 消息 + 它后面跟着的
    assistant/tool 消息算一轮，整轮一起删。绝不能只删一半：
    OpenAI 接口要求每条 tool 消息前面必须有带 tool_calls 的 assistant
    消息，拆散了 API 会直接报错。
    """

    def __init__(self, system_prompt: str, max_messages: int = DEFAULT_MAX_MESSAGES):
        self.max_messages = max_messages
        self.messages = [{"role": "system", "content": system_prompt}]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})
        self.trim()

    def add_assistant(self, message) -> None:
        """message 是模型返回的原始 message 对象（含 tool_calls 时也一样存）。"""
        self.messages.append(message)
        self.trim()

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": str(content),
        })
        self.trim()

    def clear(self) -> None:
        """清空本轮对话，只保留 system prompt。长期记忆不受影响。"""
        self.messages = self.messages[:1]

    def trim(self) -> None:
        """超过上限时，从最旧的一轮开始删，直到不超为止。system 永不删。"""
        while len(self.messages) > self.max_messages:
            # 找第一条 user 消息（下标 0 是 system，跳过），整轮删到下一条 user 之前
            start = next(
                (i for i, m in enumerate(self.messages)
                 if i > 0 and _role(m) == "user"),
                None,
            )
            if start is None:
                break  # 没有 user 消息可删（只剩 system），停下
            end = next(
                (i for i in range(start + 1, len(self.messages))
                 if _role(self.messages[i]) == "user"),
                len(self.messages),
            )
            del self.messages[start:end]


# ---------------------------------------------------------------------------
# 长期记忆：存在磁盘的事实
# ---------------------------------------------------------------------------

def _bigrams(text: str) -> set:
    """把文本切成相邻两字一组。中文没空格，用 bigram 做关键词匹配最省事。

    例："喜欢蓝色" -> {"喜欢", "欢蓝", "蓝色"}，能和事实“最喜欢的颜色是蓝色”
    对上“喜欢”和“蓝色”两组。
    """
    compact = "".join(text.split())
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


class FactStore:
    """事实仓库：一个 JSON 文件，一条事实就是一行。

    每条事实：{"text": "用户最喜欢的颜色是蓝色", "created_at": "2026-11-21 19:00:00"}
    现在只有几十条的量级，全量拼进 system prompt 完全没问题；
    等事实到几百上千条时，再改成“先 recall 检索、只拼命中的几条”。
    """

    def __init__(self, path: str | None = None):
        if path is None:
            # 默认放项目根目录的 memory.json（不是 memory.py 旁边）。
            # PROJECT_ROOT 只在 jarvis/__init__.py 里算一次。
            path = os.path.join(PROJECT_ROOT, "memory.json")
        self.path = path
        self.facts = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.facts = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.facts = []

    def add(self, text: str) -> str:
        text = text.strip()
        if not text:
            return "记的内容是空的，没记下来。"
        if any(f["text"] == text for f in self.facts):
            return f"已经记过了：{text}"
        self.facts.append({
            "text": text,
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._save()
        return f"记住了：{text}"

    def delete(self, text: str) -> str:
        """删一条事实：按全文精确匹配，没找到就明说。"""
        text = text.strip()
        before = len(self.facts)
        self.facts = [f for f in self.facts if f["text"] != text]
        if len(self.facts) == before:
            return f"没找到这条事实：{text}"
        self._save()
        return f"已忘记：{text}"

    def update(self, old_text: str, new_text: str) -> str:
        """改一条事实：旧内容换成新内容，时间戳刷新。"""
        old_text, new_text = old_text.strip(), new_text.strip()
        if not new_text:
            return "新内容是空的，没改。"
        for fact in self.facts:
            if fact["text"] == old_text:
                fact["text"] = new_text
                fact["created_at"] = datetime.datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S")
                self._save()
                return f"已更新：{old_text} → {new_text}"
        return f"没找到这条事实：{old_text}"

    def search(self, query: str, top_k: int = 5) -> list:
        """按 bigram 重合度给事实打分，返回最相关的 top_k 条。"""
        query_bigrams = _bigrams(query)
        if not query_bigrams:
            return []
        scored = []
        for fact in self.facts:
            fact_bigrams = _bigrams(fact["text"])
            overlap = len(query_bigrams & fact_bigrams)
            # 整句原样出现在事实里，再加 2 分（比如问“喜欢什么颜色”）
            if "".join(query.split()) in "".join(fact["text"].split()):
                overlap += 2
            if overlap > 0:
                scored.append((overlap, fact["text"]))
        scored.sort(key=lambda item: -item[0])
        return [text for _, text in scored[:top_k]]

    def all_texts(self) -> list:
        return [f["text"] for f in self.facts]

    def render_for_prompt(self, query: str | None = None) -> str:
        """拼进 system prompt 的一段话。没有事实时返回空字符串。

        事实不多（≤10 条）时全量拼；多了以后只拼相关的：有 query 就
        按 query 检索 top 5，没 query 就拼最近的 10 条。prompt 里
        不塞用不上的事实，省上下文。
        """
        if not self.facts:
            return ""
        if len(self.facts) <= 10:
            texts = self.all_texts()
        elif query:
            texts = self.search(query, top_k=5)
        else:
            texts = self.all_texts()[-10:]
        if not texts:
            return ""
        lines = "\n".join(f"- {text}" for text in texts)
        return f"以下是你长期记忆中关于用户的事实，回答时可以参考：\n{lines}"

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.facts, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            # 写不进磁盘时（比如 Vercel 这类只读/无状态环境）不抛异常：
            # 事实留在内存里继续用，持久化交给调用方（网页版是浏览器
            # localStorage）。桌面版运行时写盘失败会在终端里看到这条警告。
            import sys
            print(f"警告：长期记忆写盘失败（仅内存保留）：{exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 记忆工具：让模型自己也能读写长期记忆
# ---------------------------------------------------------------------------

def memory_tools(store: FactStore):
    """返回 (schemas, funcs)，用法和 tools.py 的 TOOLS / TOOL_FUNCS 一样。

    模型在对话里可以主动调 remember_fact，比如用户说“我最喜欢蓝色”
    时自己记下来；也可以调 recall_facts 查一条旧事实，而不是每次
    都靠 system prompt 里那份全量清单。
    """

    def remember_fact(fact: str) -> str:
        """把一条关于用户的事实存进长期记忆。"""
        return store.add(fact)

    def recall_facts(query: str) -> str:
        """按关键词在长期记忆里找相关的事实。"""
        hits = store.search(query)
        if not hits:
            return "长期记忆里没找到相关的事实。"
        return "\n".join(f"- {text}" for text in hits)

    def delete_fact(fact: str) -> str:
        """从长期记忆里删一条事实（全文匹配）。"""
        return store.delete(fact)

    def update_fact(old_fact: str, new_fact: str) -> str:
        """把长期记忆里的一条事实改成新内容。"""
        return store.update(old_fact, new_fact)

    schemas = [
        {
            "type": "function",
            "function": {
                "name": "remember_fact",
                "description": "把一条关于用户的事实（偏好、习惯、重要信息）存进长期记忆，下次对话还能用。用户明确说“记住”或者说出一个稳定偏好时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fact": {
                            "type": "string",
                            "description": "要记住的事实，例如 '用户最喜欢的颜色是蓝色'",
                        }
                    },
                    "required": ["fact"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recall_facts",
                "description": "按关键词在长期记忆里检索相关的事实。当用户的问题可能和过去记住的事有关时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "检索关键词，例如 '喜欢的颜色'",
                        }
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "delete_fact",
                "description": "从长期记忆里删除一条事实。用户说“忘了这个”“别再记这个”时调用，要传事实的完整原文。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fact": {
                            "type": "string",
                            "description": "要删除的事实原文",
                        }
                    },
                    "required": ["fact"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "update_fact",
                "description": "修改长期记忆里的一条事实。用户纠正之前记住的内容时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "old_fact": {
                            "type": "string",
                            "description": "要修改的事实原文",
                        },
                        "new_fact": {
                            "type": "string",
                            "description": "改成的新内容",
                        },
                    },
                    "required": ["old_fact", "new_fact"],
                },
            },
        },
    ]
    return schemas, {
        "remember_fact": remember_fact,
        "recall_facts": recall_facts,
        "delete_fact": delete_fact,
        "update_fact": update_fact,
    }
