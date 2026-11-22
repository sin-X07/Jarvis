"""任务清单（P4-2）：agent 自己维护的待办。

Plan 模式产出计划后，步骤变成这张清单；执行时模型调 update_todo
勾掉做完的项，用户随时能看到还剩几步。清单存在会话目录里
（~/.jarvis/sessions/<id>-todos.json），跟着会话走。

U4-2：清单有三态——pending（☐）、active（◐，进行中）、done（☑）。
进行中同时只有一个，取第一个未完成的（派生，不另存状态）：
模型按顺序执行，每勾掉一步，下一项自动变成进行中。
每次变动调 on_change（界面层挂上去：落盘 + 发 todo_set 事件，
三个界面整表重渲染同一份清单）。
"""

import json
import os


class TodoList:
    """一张清单：每项 = {title, acceptance, done}。"""

    def __init__(self, items=None, on_change=None):
        self.items = list(items or [])
        self.on_change = on_change

    def _notify(self):
        if self.on_change is not None:
            self.on_change()

    def add(self, title: str, acceptance: str = "") -> int:
        self.items.append({"title": title, "acceptance": acceptance,
                           "done": False})
        self._notify()
        return len(self.items) - 1

    def mark_done(self, index: int) -> str:
        if not 0 <= index < len(self.items):
            return f"没有第 {index + 1} 项。"
        self.items[index]["done"] = True
        self._notify()
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

    def active_index(self) -> int | None:
        """进行中的下标：第一个未完成的；全做完或清单空时为 None。"""
        for i, item in enumerate(self.items):
            if not item["done"]:
                return i
        return None

    def to_event(self) -> list:
        """todo_set 事件的 payload：[{content, status}] 全量。

        status：pending / active / done；active 同时只有一个。
        """
        active = self.active_index()
        out = []
        for i, item in enumerate(self.items):
            if item["done"]:
                status = "done"
            elif i == active:
                status = "active"
            else:
                status = "pending"
            out.append({"content": item["title"], "status": status,
                        "acceptance": item.get("acceptance", "")})
        return out

    def render(self) -> str:
        if not self.items:
            return "(清单是空的)"
        lines = []
        active = self.active_index()
        for i, item in enumerate(self.items, 1):
            if item["done"]:
                mark = "☑"
            elif i - 1 == active:
                mark = "◐"
            else:
                mark = "☐"
            line = f"{mark} {i}. {item['title']}"
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


def attach_todo_tools(todo: TodoList, extra_tools: list,
                      extra_funcs: dict) -> None:
    """把 update_todo 工具换成指向这张新清单的版本（就地改列表/字典）。

    计划获批后清单换了对象，旧的 update_todo 闭包还指着旧清单，
    必须重挂；三个界面（CLI/TUI/GUI/网页）的 _set_todo 都调它。
    """
    extra_tools[:] = [t for t in extra_tools
                      if t.get("function", {}).get("name") != "update_todo"]
    extra_funcs.pop("update_todo", None)
    schemas, funcs = todo_tools(todo)
    extra_tools += schemas
    extra_funcs.update(funcs)


def todos_path(session_root: str, session_id: str) -> str:
    """某会话的待办落盘文件：<sessions>/<id>-todos.json。"""
    return os.path.join(session_root, session_id + "-todos.json")
