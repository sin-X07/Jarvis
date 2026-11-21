"""Shell 工具（P1-1）：带安全检查的 run_shell。

执行顺序（抄 DeepSeek Harness 的工具管线：检查 → 审批 → 执行 → 审计）：
  1. policy.check_command：黑名单命中直接拒绝；
  2. escapes_workspace：想跳出项目目录直接拒绝；
  3. gate.approve：只读模式拒绝；edit 模式写命令要确认；
  4. 子进程跑命令：cwd 固定在项目目录；环境变量里去掉 key
     （DEEPSEEK_API_KEY 绝不能进子进程）；超时默认 30s；
  5. 输出截断：前 4000 字符 + 末尾 500 字符；
  6. audit 记一行：跑了什么命令、退出码是几（不记输出内容）。

平台说明：subprocess 用 shell=True，走系统默认 shell——Windows 上是
cmd.exe，Linux 上是 /bin/sh。命令本身要按目标平台写；跨平台的
命令（如 python、git）在两个平台上行为一致。Windows 上需复核的点
见交付报告。
"""

import os
import subprocess
import sys

from audit import audit
from config import load_config
from permissions import current_gate
from policy import check_command, classify_command, escapes_workspace

# 项目根目录：和 tools.py 里的 PROJECT_DIR 同一个值（不从 tools 导入，
# 避免 shell_tools ↔ tools 循环引用）。
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

MAX_OUTPUT_HEAD = 4000
MAX_OUTPUT_TAIL = 500


def _truncate(text: str) -> str:
    limit = MAX_OUTPUT_HEAD + MAX_OUTPUT_TAIL
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return (text[:MAX_OUTPUT_HEAD]
            + f"\n…（中间省略 {omitted} 字符）…\n"
            + text[-MAX_OUTPUT_TAIL:])


def _clean_env() -> dict:
    """子进程的环境变量：去掉 key，防止命令把它打出来或传出去。"""
    env = dict(os.environ)
    for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        env.pop(key, None)
    return env


def kill_process_tree(proc) -> None:
    """杀掉一整棵进程树。

    只杀 shell 本身不够：shell 起的子进程（比如 python）还抱着管道，
    等输出的 communicate 会一直等下去。POSIX 杀进程组；Windows 用
    taskkill /T 连子进程一起杀，失败时退回 proc.kill()。

    前提：调用方在 POSIX 上必须用 start_new_session=True 起子进程，
    否则 killpg 会杀到调用者自己的进程组（曾经因此自杀过一次）。
    """
    import sys
    try:
        if sys.platform != "win32":
            import os
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5)
            except OSError:
                pass
            try:
                proc.kill()
            except OSError:
                pass
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def run_shell(command: str, timeout: float | None = None) -> str:
    """跑一条 shell 命令，返回“退出码 + 输出”。失败也返回文本，不抛异常。"""
    cfg = load_config()
    timeout = cfg.shell_timeout if timeout is None else timeout

    allowed, reason = check_command(command)
    if not allowed:
        audit("shell", command[:200], ok=False, detail="policy 拒绝")
        return f"拒绝：{reason}"
    if escapes_workspace(command):
        audit("shell", command[:200], ok=False, detail="跳出工作区")
        return "拒绝：命令想离开项目目录，Jarvis 只在项目目录里跑命令。"

    kind = classify_command(command)
    gate = current_gate()
    action = {"kind": "shell", "target": command,
              "detail": f"命令类型：{kind}（{'写命令' if kind == 'write' else '读命令'}）"}
    if kind == "write":
        ok, why = gate.approve(action)
        if not ok:
            audit("shell", command[:200], ok=False, detail="用户拒绝")
            return f"未执行：{why}"
    else:
        # 读命令：只读模式也拒绝（只读 = 什么都不跑）
        if gate.mode == "readonly":
            audit("shell", command[:200], ok=False, detail="只读模式")
            return "未执行：当前是只读模式（readonly），不允许跑命令。"

    try:
        # POSIX 上开新进程组：超时杀进程组时才不会误杀自己所在的进程组。
        # （jobs.py 的后台任务同理。Windows 上没有进程组概念，不用。）
        kwargs = {}
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            command, shell=True, cwd=PROJECT_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=_clean_env(),
            **kwargs)
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # 超时：杀整棵树（不只杀 shell），否则子进程抱着管道不放，
            # 再等输出时会永远等下去。第二次等也加时限，防极端残留。
            kill_process_tree(proc)
            try:
                output, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                output = ""
            audit("shell", command[:200], ok=False, detail=f"超时 {timeout}s")
            return f"命令超时（{timeout} 秒），已终止。"
    except OSError as exc:
        audit("shell", command[:200], ok=False, detail=str(exc)[:100])
        return f"命令执行失败：{exc}"

    output = _truncate(output or "")
    audit("shell", command[:200], ok=proc.returncode == 0,
          detail=f"退出码 {proc.returncode}")
    return f"退出码 {proc.returncode}\n{output}" if output else f"退出码 {proc.returncode}"
