"""假 MCP 服务器（测试夹具）：在 stdin/stdout 上说 JSON-RPC。

支持 initialize、tools/list、tools/call 两个工具：
  echo：原样返回传进来的 text；
  add：返回 a + b 的和。
通知（没有 id 的消息）直接忽略。给 test_extensions.py 用。
"""

import json
import sys


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "")
        req_id = msg.get("id")
        if req_id is None:
            continue  # 通知，不回
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0.1"},
            }
        elif method == "tools/list":
            result = {"tools": [
                {"name": "echo", "description": "原样返回 text",
                 "inputSchema": {"type": "object", "properties": {
                     "text": {"type": "string"}},
                     "required": ["text"]}},
                {"name": "add", "description": "返回 a + b",
                 "inputSchema": {"type": "object", "properties": {
                     "a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
            ]}
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments", {})
            if name == "echo":
                text = str(args.get("text", ""))
                result = {"content": [{"type": "text", "text": text}]}
            elif name == "add":
                total = args.get("a", 0) + args.get("b", 0)
                result = {"content": [{"type": "text", "text": str(total)}]}
            else:
                result = {"content": [{"type": "text", "text": "未知工具"}],
                          "isError": True}
        else:
            result = {}
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": req_id,
                 "error": {"code": -32601, "message": "未知方法"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
