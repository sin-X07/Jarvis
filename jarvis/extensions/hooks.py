"""Hooks（P6-4，轻量版）：在固定时机自动跑的确定性动作。

抄 Claude Code hooks 的最小版：三个时机点——
  before_write：写文件前（比如自动跑格式化、检查）；
  before_shell：跑命令前；
  on_session_end：会话结束时。

配置：~/.jarvis/hooks.json 或项目 hooks.json：
    {"before_write": ["python -m py_compile {file}"],
     "on_session_end": ["echo 会话结束"]}
{file} 会被替换成正在写的文件路径。钩子用 shell 跑，超时 10 秒，
环境变量里去掉 key。before_write 的钩子返回非零退出码时，写入
被拦住，并把钩子的输出告诉模型。
"""

import json
import os
import subprocess

from jarvis.core.config import default_config_dir
from jarvis.tools.shell_tools import PROJECT_DIR, _clean_env

HOOK_EVENTS = ("before_write", "before_shell", "on_session_end")


def load_hooks(path: str | None = None) -> dict:
    """读 hooks 配置：显式 path > 项目 hooks.json > ~/.jarvis/hooks.json。"""
    candidates = []
    if path:
        candidates.append(path)
    candidates.append(os.path.join(os.getcwd(), "hooks.json"))
    candidates.append(os.path.join(default_config_dir(), "hooks.json"))
    for candidate in candidates:
        try:
            with open(candidate, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {k: v for k, v in data.items()
                        if k in HOOK_EVENTS and isinstance(v, list)}
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def run_hooks(event: str, context: dict | None = None, hooks: dict | None = None,
              timeout: float = 10.0) -> list:
    """跑一个时机的全部钩子，返回 [(cmd, ok, output)]。

    context 里的 {file}、{command} 会被替换进命令。钩子之间互相独立，
    一个失败不影响下一个跑；调用方自己决定失败要不要拦住主流程。
    """
    context = context or {}
    hooks = hooks if hooks is not None else load_hooks()
    results = []
    for cmd in hooks.get(event, []):
        expanded = str(cmd)
        for key, value in context.items():
            expanded = expanded.replace("{" + key + "}", str(value))
        try:
            proc = subprocess.run(
                expanded, shell=True, cwd=PROJECT_DIR, timeout=timeout,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=_clean_env())
            output = ((proc.stdout or "") + (proc.stderr or "")).strip()
            results.append((cmd, proc.returncode == 0, output[:2000]))
        except subprocess.TimeoutExpired:
            results.append((cmd, False, f"钩子超时（{timeout} 秒）"))
        except OSError as exc:
            results.append((cmd, False, f"钩子执行失败：{exc}"))
    return results


def before_write_hook(path: str, hooks: dict | None = None):
    """tools.py 写前钩子的适配器：跑 before_write，失败就拦住写入。"""
    results = run_hooks("before_write", {"file": path}, hooks=hooks)
    for cmd, ok, output in results:
        if not ok:
            return False, (f"写前检查没通过（钩子 {cmd!r} 失败）"
                           + (f"：{output}" if output else ""))
    return True, ""
