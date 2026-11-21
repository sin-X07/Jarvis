"""MCP 客户端（P6-3）：标准库实现 JSON-RPC over stdio，不引入新依赖。

MCP（Model Context Protocol）是模型与外部服务之间的标准接口：
起一个 MCP 服务器子进程（比如官方的 filesystem、github server），
用 JSON-RPC 在 stdin/stdout 上说话，服务器的工具就变成 Jarvis 的
工具，名字前加 `mcp__<服务器>_` 以区分。

协议流程（MCP 2024-11 版最小实现）：
  1. 发 initialize（带 protocolVersion + clientInfo）；
  2. 发 notifications/initialized；
  3. tools/list 拿工具表；tools/call 调工具。
服务器的工具 schema 是 JSON Schema，和 OpenAI 的 function calling
格式基本一致，直接转。

配置：~/.jarvis/mcp.json 或项目 mcp.json：
    {"servers": {"filesystem": {"command": "npx", "args": ["-y",
      "@modelcontextprotocol/server-filesystem", "/path/to/dir"]}}}
"""

import json
import os
import queue
import subprocess
import threading

from config import default_config_dir


class MCPError(Exception):
    """MCP 协议层面的错误（服务器报错、超时、进程挂了）。"""


class MCPClient:
    """一个 MCP 服务器子进程的客户端。"""

    def __init__(self, name: str, command: str, args=None, env=None,
                 timeout: float = 10.0):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = env
        self.timeout = timeout
        self._proc = None
        self._next_id = 1
        self._lines = queue.Queue()
        self._reader = None

    def start(self) -> None:
        """启动子进程并完成 initialize 握手。"""
        env = dict(os.environ)
        if self.env:
            env.update(self.env)
        env.pop("DEEPSEEK_API_KEY", None)  # key 绝不进子进程
        try:
            self._proc = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", env=env)
        except OSError as exc:
            raise MCPError(f"MCP 服务器 {self.name} 启动失败：{exc}")
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        try:
            result = self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "jarvis", "version": "0.2.0"},
            })
        except MCPError:
            self.close()
            raise
        _ = result
        self._notify("notifications/initialized", {})

    def _read_loop(self) -> None:
        try:
            for line in self._proc.stdout:
                self._lines.put(line)
        except (OSError, ValueError):
            pass

    def _send(self, payload: dict) -> None:
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except (OSError, BrokenPipeError) as exc:
            raise MCPError(f"写 MCP 服务器失败：{exc}")

    def _request(self, method: str, params: dict):
        req_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": req_id,
                    "method": method, "params": params})
        deadline = self.timeout
        while True:
            try:
                line = self._lines.get(timeout=deadline)
            except queue.Empty:
                raise MCPError(f"MCP 请求超时：{method}")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") != req_id:
                continue  # 不是这次请求的回应（比如服务器的通知），跳过
            if "error" in msg:
                raise MCPError(f"MCP 服务器报错：{msg['error']}")
            return msg.get("result", {})

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def list_tools(self) -> list:
        """返回 [{name, description, inputSchema}]。"""
        result = self._request("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> str:
        """调一个工具，返回文本结果（content 块拼起来）。"""
        result = self._request("tools/call", {
            "name": name, "arguments": arguments or {}})
        parts = []
        for block in result.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        if result.get("isError"):
            return "MCP 工具报错：\n" + "\n".join(parts)
        return "\n".join(parts) if parts else "(没有返回内容)"

    def close(self) -> None:
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except (OSError, BrokenPipeError):
                pass
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self._proc.kill()
                except OSError:
                    pass
            self._proc = None


def load_mcp_config(path: str | None = None) -> dict:
    """读 MCP 配置：显式 path > 项目 mcp.json > ~/.jarvis/mcp.json。"""
    candidates = []
    if path:
        candidates.append(path)
    candidates.append(os.path.join(os.getcwd(), "mcp.json"))
    candidates.append(os.path.join(default_config_dir(), "mcp.json"))
    for candidate in candidates:
        try:
            with open(candidate, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("servers"):
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return {"servers": {}}


def mcp_tools(config: dict | None = None, path: str | None = None):
    """按配置启动 MCP 服务器，把它们的工具变成 Jarvis 工具。

    返回 (schemas, funcs, clients)：clients 要调用方在结束时 close。
    工具名 = mcp__<服务器>_<工具名>。某个服务器起不来时跳过它，
    不拦别的服务器和主流程。
    """
    config = config or load_mcp_config(path)
    schemas, funcs, clients = [], {}, []
    for server_name, spec in (config.get("servers") or {}).items():
        if not isinstance(spec, dict) or not spec.get("command"):
            continue
        client = MCPClient(server_name, spec["command"],
                           args=spec.get("args"), env=spec.get("env"))
        try:
            client.start()
            tools = client.list_tools()
        except MCPError as exc:
            print(f"警告：MCP 服务器 {server_name} 没接上：{exc}")
            continue
        clients.append(client)
        for tool in tools:
            tool_name = tool.get("name", "")
            if not tool_name:
                continue
            jarvis_name = f"mcp__{server_name}_{tool_name}"

            def _make_call(client=client, tool_name=tool_name):
                def _call(**kwargs):
                    return client.call_tool(tool_name, kwargs)
                return _call

            schemas.append({
                "type": "function",
                "function": {
                    "name": jarvis_name,
                    "description": (f"[MCP/{server_name}] "
                                    + (tool.get("description") or tool_name)),
                    "parameters": tool.get("inputSchema") or {
                        "type": "object", "properties": {}},
                },
            })
            funcs[jarvis_name] = _make_call()
    return schemas, funcs, clients
