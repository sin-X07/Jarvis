"""后台任务（P5-3）：长时间命令放后台跑，聊天继续。

用法：run_shell_background 启一个任务，返回 id；job_status 看进度，
job_result 取完整输出。任务跑在线程里，命令本身用 subprocess.Popen，
超时会杀掉进程。输出截断到 8000 字符，完整输出只留最后一次查询。

安全：走同一套 policy 检查（黑名单、跳出工作区、确认门），环境
变量里同样去掉 key。后台任务默认 300 秒超时。
"""

import subprocess
import threading
import time

from jarvis.safety.audit import audit
from jarvis.safety.policy import check_command, escapes_workspace
from jarvis.tools.shell_tools import PROJECT_DIR, _clean_env, _truncate, kill_process_tree

MAX_JOBS = 20


class JobManager:
    """管后台任务：启动、查状态、取结果。"""

    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()
        self._counter = 0

    def start(self, command: str, timeout: float = 300.0):
        """启动一个后台任务，返回 job id；不合法的命令返回错误文本。"""
        allowed, reason = check_command(command)
        if not allowed:
            return None, f"拒绝：{reason}"
        if escapes_workspace(command):
            return None, "拒绝：命令想离开项目目录。"
        with self._lock:
            self._counter += 1
            job_id = f"job-{self._counter}"
            if len(self._jobs) >= MAX_JOBS:
                oldest = sorted(self._jobs)[0]
                del self._jobs[oldest]
            job = {"id": job_id, "command": command, "status": "running",
                   "started": time.time(), "exit_code": None, "output": ""}
            self._jobs[job_id] = job
        thread = threading.Thread(target=self._run, args=(job, timeout),
                                  daemon=True)
        thread.start()
        audit("job_start", command[:200], ok=True, detail=job_id)
        return job_id, f"后台任务已启动：{job_id}，用 job_status 查看进度。"

    def _run(self, job: dict, timeout: float) -> None:
        # POSIX 上开新进程组：超时要杀的是整棵进程树（shell + 它起的子进程）。
        # Windows 上 taskkill /T 同样杀整棵树（见 shell_tools.kill_process_tree）。
        import sys
        kwargs = {}
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                job["command"], shell=True, cwd=PROJECT_DIR,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=_clean_env(), **kwargs)
            try:
                output, _ = proc.communicate(timeout=timeout)
                job["exit_code"] = proc.returncode
                job["status"] = "done" if proc.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                self._kill_tree(proc)
                try:
                    output, _ = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    # 极端情况：仍有孤儿进程抱着管道；不再等，直接记超时。
                    output = ""
                job["exit_code"] = None
                job["status"] = "timeout"
            job["output"] = _truncate(output or "")
        except OSError as exc:
            job["status"] = "failed"
            job["output"] = f"启动失败：{exc}"
        audit("job_finish", job["command"][:200],
              ok=job["status"] == "done", detail=job["id"])

    @staticmethod
    def _kill_tree(proc) -> None:
        """杀掉进程树（共用 shell_tools.kill_process_tree）。"""
        kill_process_tree(proc)

    def status(self, job_id: str) -> str:
        job = self._jobs.get(job_id)
        if job is None:
            return f"没有这个任务：{job_id}"
        elapsed = round(time.time() - job["started"], 1)
        tail = (job["output"] or "")[-500:]
        return (f"{job_id}：{job['status']}（已跑 {elapsed} 秒"
                + (f"，退出码 {job['exit_code']}" if job["exit_code"] is not None else "")
                + "）\n" + tail)

    def result(self, job_id: str) -> str:
        job = self._jobs.get(job_id)
        if job is None:
            return f"没有这个任务：{job_id}"
        if job["status"] == "running":
            return f"{job_id} 还在跑，用 job_status 查看进度。"
        return (f"{job_id}：{job['status']}"
                + (f"，退出码 {job['exit_code']}" if job["exit_code"] is not None else "")
                + "\n" + (job["output"] or "(没有输出)"))

    def list_jobs(self) -> str:
        if not self._jobs:
            return "(没有后台任务)"
        lines = []
        for job_id in sorted(self._jobs):
            job = self._jobs[job_id]
            lines.append(f"{job_id}：{job['status']} —— {job['command'][:80]}")
        return "\n".join(lines)


def jobs_tools(manager: JobManager):
    """给模型的三个后台任务工具。"""
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "run_shell_background",
                "description": "把一条长时间命令放后台跑（编译、测试、大范围搜索），立刻返回任务 id，不用等它跑完。跑完用 job_status / job_result 查。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要跑的命令"},
                        "timeout": {"type": "number", "description": "超时秒数，默认 300"},
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "job_status",
                "description": "查一个后台任务的进度和最近输出。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "任务 id，如 job-1"},
                    },
                    "required": ["job_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "job_result",
                "description": "取一个后台任务的完整输出（跑完才能取）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "任务 id，如 job-1"},
                    },
                    "required": ["job_id"],
                },
            },
        },
    ]

    def run_shell_background(command: str, timeout: float = 300.0) -> str:
        _job_id, message = manager.start(command, timeout)
        return message

    return schemas, {
        "run_shell_background": run_shell_background,
        "job_status": manager.status,
        "job_result": manager.result,
    }
