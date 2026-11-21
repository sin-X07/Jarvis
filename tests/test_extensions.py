"""P6 测试：技能、MCP、Hooks、插件（不用 key、不联网）。

验证：
  1. 技能：内置 4 个都能列出来、读全文、use_skill 工具能调
  2. MCP：假服务器握手、列工具、调工具；mcp_tools 接线后名字带前缀
  3. Hooks：通过的钩子放行，失败的钩子拦住写入
  4. 插件：放一个 .py 就能注册工具；坏插件被跳过不影响别的
"""

import json
import os
import sys
import tempfile

from jarvis.extensions import skills
from jarvis.tools import tools
from jarvis.extensions.hooks import before_write_hook, load_hooks, run_hooks
from jarvis.extensions.mcp_client import MCPClient, mcp_tools
from jarvis.extensions.plugins import load_plugin_tools

FAKE_MCP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "test_fixtures", "fake_mcp_server.py")


def test_skills_listed():
    found = skills.list_skills()
    names = [s["name"] for s in found]
    for expected in ("code-review", "write-tests", "project-onboarding",
                     "github-trending-summary"):
        assert expected in names, names
    descs = {s["name"]: s["description"] for s in found}
    assert "评审" in descs["code-review"], descs
    print("[OK] 技能：内置 4 个都能列出来，带一句话描述")


def test_skill_load_and_tool():
    body = skills.load_skill("code-review")
    assert body and "git_diff" in body, body[:200]
    assert skills.load_skill("不存在的技能") is None
    schemas, funcs = skills.skill_tools()
    assert schemas[0]["function"]["name"] == "use_skill"
    out = funcs["use_skill"]("write-tests")
    assert "离线" in out, out[:200]
    assert "没有这个技能" in funcs["use_skill"]("不存在的技能")
    fragment = skills.skills_prompt_fragment()
    assert "code-review" in fragment, fragment
    print("[OK] 技能：读全文、use_skill 工具、prompt 片段都正常")


def test_mcp_fake_server():
    client = MCPClient("fake", sys.executable, args=[FAKE_MCP])
    client.start()
    try:
        names = [t["name"] for t in client.list_tools()]
        assert names == ["echo", "add"], names
        assert client.call_tool("echo", {"text": "hello"}) == "hello"
        assert client.call_tool("add", {"a": 3, "b": 4}) == "7"
        assert "报错" in client.call_tool("nope", {})
    finally:
        client.close()
    print("[OK] MCP：假服务器握手、列工具、调工具都正常")


def test_mcp_tools_wiring():
    config = {"servers": {"fake": {
        "command": sys.executable, "args": [FAKE_MCP]}}}
    schemas, funcs, clients = mcp_tools(config)
    try:
        names = [s["function"]["name"] for s in schemas]
        assert "mcp__fake_echo" in names and "mcp__fake_add" in names, names
        assert funcs["mcp__fake_echo"](text="hi") == "hi"
        assert funcs["mcp__fake_add"](a=10, b=5) == "15"
    finally:
        for client in clients:
            client.close()
    print("[OK] MCP：接线后工具名带 mcp__fake_ 前缀，能直接调")


def test_mcp_bad_server_skipped():
    config = {"servers": {"broken": {"command": "definitely-not-a-program"}}}
    schemas, funcs, clients = mcp_tools(config)
    assert schemas == [] and funcs == {} and clients == []
    print("[OK] MCP：起不来的服务器被跳过，不拦主流程")


def test_hooks_pass_and_block():
    tmp = tempfile.mkdtemp()
    hooks = {"before_write": ["echo ok"]}
    results = run_hooks("before_write", {"file": "x.py"}, hooks=hooks)
    assert results and results[0][1] is True, results
    bad_hooks = {"before_write": [
        f'"{sys.executable}" -c "import sys; sys.exit(1)"']}
    results = run_hooks("before_write", {"file": "x.py"}, hooks=bad_hooks)
    assert results and results[0][1] is False, results
    ok, msg = before_write_hook("x.py", hooks=bad_hooks)
    assert not ok and "钩子" in msg, msg
    print("[OK] Hooks：通过的放行，失败的拦住写入")


def test_hooks_block_write():
    tmp = tempfile.mkdtemp()
    old_proj = tools.PROJECT_DIR
    tools.PROJECT_DIR = tmp
    old_hooks = list(tools._BEFORE_WRITE_HOOKS)
    from jarvis.safety import audit as audit_mod
    from jarvis.safety.audit import AuditLog
    old_audit = audit_mod._DEFAULT
    audit_mod._DEFAULT = AuditLog(os.path.join(tmp, "audit.jsonl"))
    bad = {"before_write": [
        f'"{sys.executable}" -c "import sys; sys.exit(1)"']}
    try:
        tools.write_text_file("a.txt", "hello\n")
        tools.register_before_write_hook(
            lambda path: before_write_hook(path, hooks=bad))
        msg = tools.write_text_file("a.txt", "second\n")
        assert "未写入" in msg and "钩子" in msg, msg
        assert open(os.path.join(tmp, "a.txt")).read() == "hello\n"
    finally:
        tools._BEFORE_WRITE_HOOKS[:] = old_hooks
        tools.PROJECT_DIR = old_proj
        audit_mod._DEFAULT = old_audit
    print("[OK] Hooks：写前钩子失败时文件不被覆盖")


def test_plugins():
    tmp = tempfile.mkdtemp()
    plugin = ('TOOL_SCHEMA = {"type": "function", "function": {'
              '"name": "shout", "description": "大声说", "parameters": '
              '{"type": "object", "properties": {"text": {"type": "string"}}, '
              '"required": ["text"]}}}\n'
              'def TOOL_FUNC(text):\n'
              '    return text.upper() + "!"\n')
    with open(os.path.join(tmp, "shout.py"), "w", encoding="utf-8") as f:
        f.write(plugin)
    with open(os.path.join(tmp, "broken.py"), "w", encoding="utf-8") as f:
        f.write("def oops(:\n")
    with open(os.path.join(tmp, "no_schema.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    schemas, funcs, skipped = load_plugin_tools(tmp)
    assert [s["function"]["name"] for s in schemas] == ["shout"], schemas
    assert funcs["shout"](text="hi") == "HI!"
    assert len(skipped) == 2, skipped
    schemas2, _, skipped2 = load_plugin_tools(os.path.join(tmp, "不存在"))
    assert schemas2 == [] and skipped2 == []
    print("[OK] 插件：放一个 .py 就能注册工具，坏插件被跳过")


def test_load_hooks_file():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "hooks.json")
    with open(path, "w") as f:
        json.dump({"before_write": ["echo x"], "nonsense": ["echo y"]}, f)
    hooks = load_hooks(path)
    assert hooks == {"before_write": ["echo x"]}, hooks
    print("[OK] Hooks：配置文件只认三个时机点")


if __name__ == "__main__":
    test_skills_listed()
    test_skill_load_and_tool()
    test_mcp_fake_server()
    test_mcp_tools_wiring()
    test_mcp_bad_server_skipped()
    test_hooks_pass_and_block()
    test_hooks_block_write()
    test_plugins()
    test_load_hooks_file()
    print("全部通过。")
