"""任务清单（P4-2）：agent 自己维护的待办。

Plan 模式产出计划后，步骤变成这张清单；执行时模型调 update_todo
勾掉做完的项，用户随时能看到还剩几步。清单存在会话目录里
（~/.jarvis/sessions/<id>-todos.json），跟着会话走。
"""

import json
import os


class TodoList:
    """一张清单：每项 = {title, acceptance, done}。"""

    def __init__(self, items=None):
        self.items = list(items or [])

    def add(self, title: str, acceptance: str = "") -> int:
        self.items.append({"title": title, "acceptance": acceptance,
                           "done": False})
        return len(self.items) - 1

    def mark_done(self, index: int) -> str:
        if not 0 <= index < len(self.items):
            return f"没有第 {index + 1} 项。"
        self.items[index]["done"] = True
        return f"已完成：{self.items[index]['title']}"

    def mark_done_by_title(self, title: str) -> str:
        for i, item in enumerate(self.items):
            if item["title"] == title:
                return self.mark_done(i)
        return f"清单里没有这一项：{title}"

    def pending(self) -> list:
        return [it for it in self.items if not it["done"]]

    def is_all_done(self) -> bool:
        return bool(self.items) and all(it["done"] for it in self.items)

    def render(self) -> str:
        if not self.items:
            return "(清单是空的)"
        lines = []
        for i, item in enumerate(self.items, 1):
            mark = "x" if item["done"] else " "
            line = f"[{mark}] {i}. {item['title']}"
            if item.get("acceptance"):
                line += f"（验收：{item['acceptance']}）"
            lines.append(line)
        return "\n".join(lines)

    def to_dict(self) -> list:
        return [dict(it) for it in self.items]

    @classmethod
    def from_dict(cls, data: list) -> "TodoList":
        items = []
        for row in data or []:
            if isinstance(row, dict) and row.get("title"):
                items.append({
                    "title": row["title"],
                    "acceptance": row.get("acceptance", ""),
                    "done": bool(row.get("done")),
                })
        return cls(items)

    def save(self, path: str) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    @classmethod
    def load(cls, path: str) -> "TodoList":
        try:
            with open(path, encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        except (OSError, json.JSONDecodeError):
            return cls()


def todo_tools(todo: TodoList):
    """给模型的 update_todo 工具：执行中自己勾掉做完的步骤。"""
    schemas = [{
        "type": "function",
        "function": {
            "name": "update_todo",
            "description": "把任务清单里的一项标为已完成。做完一个步骤就调一次，传步骤的标题或序号（从 1 开始）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "步骤标题"},
                    "index": {"type": "integer", "description": "步骤序号（从 1 开始），和 title 二选一"},
                },
            },
        },
    }]

    def update_todo(title: str = "", index: int | None = None) -> str:
        if index is not None:
            return todo.mark_done(int(index) - 1)
        if title:
            return todo.mark_done_by_title(title)
        return "请传 title 或 index，告诉我是哪一步做完了。"

    return schemas, {"update_todo": update_todo}
