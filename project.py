"""项目感知（P2-4 / P2-5）：项目规则文件 + 项目索引。

JARVIS.md（对标 Claude Code 的 CLAUDE.md、Codex 的 AGENTS.md）：
项目根目录放一份规则文件，写这个项目的约定（怎么构建、怎么测试、
什么不能碰）；启动时自动读进 system prompt。两层叠加：
    全局 ~/.jarvis/JARVIS.md（所有项目通用）
    + 项目级 ./JARVIS.md（这个项目专属）
另外兼容 AGENTS.md / CLAUDE.md：如果项目里已经有这两份，也一起读，
换 harness 不用重写规则。

项目索引：启动时生成文件树摘要（目录结构 + 文件数），拼进 system
prompt，让模型对项目有个整体印象。大项目只摘要顶层（深度 3、
最多 300 个文件），细节按需用 glob/grep 再挖。
"""

import os

from config import default_config_dir

RULE_FILES = ("JARVIS.md", "AGENTS.md", "CLAUDE.md")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
             ".idea", ".vscode", "dist", "build", "target", ".jarvis"}


def load_project_rules(project_dir: str) -> str:
    """读项目规则：全局 + 项目级 JARVIS.md/AGENTS.md/CLAUDE.md，拼成一段。

    没有任何规则文件时返回空字符串。每个文件前面标来源，方便模型
    知道这条规则是哪个文件说的。
    """
    parts = []
    global_path = os.path.join(default_config_dir(), "JARVIS.md")
    if os.path.isfile(global_path):
        parts.append(_read_rule(global_path, "全局规则 ~/.jarvis/JARVIS.md"))
    for name in RULE_FILES:
        path = os.path.join(project_dir, name)
        if os.path.isfile(path):
            parts.append(_read_rule(path, "项目规则 " + name))
    return "\n\n".join(p for p in parts if p)


def _read_rule(path: str, label: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return ""
    if not text:
        return ""
    return "【" + label + "】\n" + text


def build_project_index(project_dir: str, max_depth: int = 3,
                        max_files: int = 300) -> str:
    """文件树摘要：目录结构 + 每个目录的文件数。"""
    lines = []
    count = [0]
    for root, dirs, files in os.walk(project_dir):
        rel = os.path.relpath(root, project_dir)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirs[:] = []
            continue
        dirs[:] = sorted(d for d in dirs
                         if d not in SKIP_DIRS and not d.startswith("."))
        visible = sorted(f for f in files if not f.startswith("."))
        indent = "  " * depth
        label = "." if rel == "." else os.path.basename(root) + "/"
        lines.append(indent + label + "（" + str(len(visible)) + " 个文件）")
        for name in visible:
            lines.append(indent + "  " + name)
            count[0] += 1
            if count[0] >= max_files:
                lines.append("… 文件太多，只显示前 "
                             + str(max_files) + " 个，用 glob_files 再找。")
                return "\n".join(lines)
    return "\n".join(lines) if lines else "(空项目)"


def project_context_text(project_dir: str) -> str:
    """拼进 system prompt 的项目上下文：规则 + 文件树摘要。"""
    rules = load_project_rules(project_dir)
    index = build_project_index(project_dir)
    parts = ["项目目录：" + project_dir + "\n文件结构：\n" + index]
    if rules:
        parts.append(rules)
    return "\n\n".join(parts)
