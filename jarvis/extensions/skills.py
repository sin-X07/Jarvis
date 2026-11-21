"""技能（P6-1 / P6-2）：可复用的流程说明。

Skill 就是一份 SKILL.md：名字、一句话描述、触发条件、步骤。
启动时只读元数据（名字 + 一句话）拼进 system prompt，用到了
模型再调 use_skill 读全文——懒加载，省上下文（抄 Claude Code）。

找 skill 的地方（按优先级）：
  1. 项目自带的 skills/（随仓库发，4 个内置）
  2. ~/.jarvis/skills/（用户自己的）

格式：文件开头可选 YAML frontmatter：
    ---
    name: code-review
    description: 评审一次改动
    ---
    # Code Review
    步骤……
没有 frontmatter 时，名字取第一个 # 标题，描述取第一段正文。
"""

import os

from jarvis import PROJECT_ROOT
from jarvis.core.config import default_config_dir

# 内置技能目录：项目根目录的 skills/。PROJECT_ROOT 只在
# jarvis/__init__.py 里算一次，这里直接引用。
SHIPPED_SKILLS_DIR = os.path.join(PROJECT_ROOT, "skills")


def user_skills_dir() -> str:
    return os.path.join(default_config_dir(), "skills")


def _parse_skill(text: str, filename: str) -> dict:
    """解析一份 SKILL.md，返回 {name, description, body}。"""
    name = os.path.splitext(filename)[0]
    description = ""
    body = text
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        end = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end = i
                break
        if end is not None:
            for line in lines[1:end]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    value = value.strip().strip("\"'")
                    if key.strip() == "name" and value:
                        name = value
                    elif key.strip() == "description" and value:
                        description = value
            body = "\n".join(lines[end + 1:])
            lines = body.splitlines()
    if not description:
        for line in lines:
            line = line.strip()
            if line.startswith("#"):
                if not name or name == os.path.splitext(filename)[0]:
                    name = line.lstrip("#").strip()
            elif line:
                description = line
                break
    return {"name": name, "description": description, "body": body.strip()}


def list_skills() -> list:
    """列出所有 skill 的元数据（名字 + 一句话），不读全文。"""
    found = {}
    for directory in (SHIPPED_SKILLS_DIR, user_skills_dir()):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for filename in sorted(names):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(directory, filename)
            try:
                with open(path, encoding="utf-8") as f:
                    meta = _parse_skill(f.read(), filename)
            except OSError:
                continue
            meta["source"] = directory
            found.setdefault(meta["name"], meta)
    return sorted(found.values(), key=lambda m: m["name"])


def load_skill(name: str):
    """读一个 skill 的全文；没有返回 None。"""
    for meta in list_skills():
        if meta["name"] == name:
            try:
                with open(os.path.join(meta["source"],
                                       name + ".md"),
                          encoding="utf-8") as f:
                    return _parse_skill(f.read(), name + ".md")["body"]
            except OSError:
                return None
    # 文件名和 skill 名可能不一致（# 标题改了名）：再按内容找一遍
    for directory in (SHIPPED_SKILLS_DIR, user_skills_dir()):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for filename in names:
            if not filename.endswith(".md"):
                continue
            try:
                with open(os.path.join(directory, filename),
                          encoding="utf-8") as f:
                    meta = _parse_skill(f.read(), filename)
            except OSError:
                continue
            if meta["name"] == name:
                return meta["body"]
    return None


def skills_prompt_fragment() -> str:
    """拼进 system prompt 的 skill 清单（只有元数据）。"""
    skills = list_skills()
    if not skills:
        return ""
    lines = ["可用的技能（需要时调 use_skill 读全文，按步骤执行）："]
    for skill in skills:
        lines.append(f"- {skill['name']}：{skill['description']}")
    return "\n".join(lines)


def skill_tools():
    """给模型的 use_skill 工具。"""
    schemas = [{
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": "读一个技能的全文并按它的步骤执行。技能清单在 system prompt 里，遇到匹配的任务时先调这个再动手。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "技能名，如 code-review"},
                },
                "required": ["name"],
            },
        },
    }]

    def use_skill(name: str) -> str:
        body = load_skill(name)
        if body is None:
            known = ", ".join(s["name"] for s in list_skills())
            return f"没有这个技能：{name}。现有的技能：{known or '（无）'}"
        return body

    return schemas, {"use_skill": use_skill}
