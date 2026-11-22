"""规划模式（P4-1 / P4-4）：先想清楚，再动手。

流程（抄 Claude Code 的 plan 模式）：
  1. /plan 进入只读调研：循环只能读文件、搜索、跑只读命令，不能写；
  2. 模型按固定格式输出一份计划（目标、步骤+验收标准、风险）；
  3. parse_plan 把它解析成 Plan 对象，步骤变成 TodoList；
  4. 用户确认（可以改）后才切回执行模式；
  5. 执行完 verify_plan：再调一次模型，对照验收标准逐项检查，
     不通过的步骤标出来，修三次不过就停下来问用户。

计划格式（模型必须按这个输出，解析器按这个解析）：
    目标：<一句话>
    步骤：
    1. <标题> —— 验收：<验收标准>
    2. <标题> —— 验收：<验收标准>
    风险：
    - <风险>
"""

import re

from jarvis.memory.todo import TodoList

PLAN_INSTRUCTION = (
    "你现在处于规划模式：只能调研，不能写文件、不能跑写命令。\n"
    "先读相关文件、搜索、跑只读命令了解情况，然后按下面的格式输出"
    "一份计划，不要多写别的：\n"
    "目标：<一句话>\n"
    "步骤：\n"
    "1. <标题> —— 验收：<怎么算这步做完了>\n"
    "2. <标题> —— 验收：<怎么算这步做完了>\n"
    "风险：\n"
    "- <可能出问题的地方>\n"
)


class Plan:
    """一份计划：目标 + 带验收标准的步骤 + 风险。"""

    def __init__(self, goal: str = "", steps: list | None = None,
                 risks: list | None = None):
        self.goal = goal
        self.steps = steps or []  # [{title, acceptance, done}]
        self.risks = risks or []

    def to_todos(self) -> TodoList:
        todo = TodoList()
        for step in self.steps:
            todo.add(step["title"], step.get("acceptance", ""))
        return todo

    def render(self) -> str:
        lines = [f"目标：{self.goal}", "步骤："]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"{i}. {step['title']} —— 验收：{step.get('acceptance', '')}")
        if self.risks:
            lines.append("风险：")
            lines.extend(f"- {risk}" for risk in self.risks)
        return "\n".join(lines)


def parse_plan(text: str) -> Plan:
    """解析固定格式的计划文本；解析不到步骤时返回只有目标的空计划。"""
    goal = ""
    steps = []
    risks = []
    section = None
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("目标：") or line.startswith("目标:"):
            goal = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            section = "goal"
        elif line.startswith("步骤"):
            section = "steps"
        elif line.startswith("风险"):
            section = "risks"
        elif section == "steps":
            m = re.match(r"\d+[.、]\s*(.+)", line)
            if not m:
                continue
            body = m.group(1)
            if "——" in body and "验收" in body:
                title, acc = body.split("——", 1)
                acc = re.sub(r"^验收[:：]\s*", "", acc.strip()).strip()
                steps.append({"title": title.strip(),
                              "acceptance": acc, "done": False})
            else:
                steps.append({"title": body.strip(),
                              "acceptance": "", "done": False})
        elif section == "risks" and line.startswith("-"):
            risks.append(line[1:].strip())
    return Plan(goal=goal, steps=steps, risks=risks)


def make_plan(client, model: str, task: str, extra_tools=None,
              extra_funcs=None, max_steps: int = 8, on_event=None) -> Plan:
    """跑一轮只读循环，让模型调研并输出计划，解析后返回 Plan。

    on_event 透传给 run_agent：计划轮里工具被拦等事件也能让界面
    看到（比如网页版的工具卡片），不传就静默。"""
    from jarvis.core.agent import run_agent

    answer = run_agent(
        client, PLAN_INSTRUCTION + "\n任务：" + task,
        model=model, max_steps=max_steps, verbose=False,
        extra_tools=extra_tools, extra_funcs=extra_funcs,
        read_only=True, on_event=on_event)
    plan = parse_plan(answer)
    if not plan.goal:
        plan.goal = task
    return plan


def verify_plan(client, model: str, plan: Plan, result_text: str) -> str:
    """执行完对照验收标准检查：返回模型写的逐项检查报告。"""
    from jarvis.core.agent import run_agent

    checks = "\n".join(
        f"{i}. {s['title']}（验收：{s.get('acceptance', '')}）"
        for i, s in enumerate(plan.steps, 1))
    prompt = ("对照下面的验收标准，检查执行结果，逐项写“通过”或“未通过 + 原因”。"
              "只写检查结果，不要重做任务。\n\n验收标准：\n" + checks +
              "\n\n执行结果：\n" + result_text)
    return run_agent(client, prompt, model=model, max_steps=3, verbose=False)
