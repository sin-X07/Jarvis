"""文件与网络工具的离线测试：不用 key、不用真实网络。

fetch_url 用测试自己起的本地 HTTP 服务测，所以全程离线、结果确定。
文件工具通过 monkeypatch tools.PROJECT_DIR 到临时目录测，
绝不碰真实仓库。

Run:  python test_tools.py
"""

import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from jarvis.tools import tools

ORIGINAL_DIR = tools.PROJECT_DIR


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ("<html><head><title>Hi</title></head><body>"
                "<h1>Hello Jarvis</h1><p>second line</p>"
                "<script>var x = 1;</script></body></html>")
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 保持测试输出干净
        pass


def test_file_tools_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        tools.PROJECT_DIR = tmp
        try:
            result = tools.write_text_file("notes/hello.txt", "你好，Jarvis")
            assert result.startswith("已写入 notes/hello.txt"), result
            assert tools.read_text_file("notes/hello.txt") == "你好，Jarvis"
            listing = tools.list_files("notes")
            assert "hello.txt" in listing, listing
            assert tools.list_files(".") != "(空目录)"
            assert tools.read_text_file("notes/nope.txt") == "文件不存在: notes/nope.txt"
            # 越狱路径全部被拦
            assert "拒绝" in tools.write_text_file("../evil.txt", "x")
            assert "拒绝" in tools.read_text_file("../evil.txt")
            assert "拒绝" in tools.list_files("..")
            assert "拒绝" in tools.write_text_file("notes", "x")  # 写到目录上
            assert "目录" in tools.read_text_file("notes")
        finally:
            tools.PROJECT_DIR = ORIGINAL_DIR
    print("[OK] 文件工具：写读列往返正常，越狱路径与目录误写都被拦")


def test_fetch_url_local_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        text = tools.fetch_url(f"http://127.0.0.1:{port}/")
        assert "Hello Jarvis" in text and "second line" in text, text
        assert "<h1>" not in text and "var x = 1;" not in text, text
        # 错误路径返回文字而不是抛异常，模型才能接着处理
        assert "拒绝" in tools.fetch_url("ftp://example.com/x")
        assert "失败" in tools.fetch_url("http://127.0.0.1:1/")
    finally:
        server.shutdown()
        server.server_close()
    print("[OK] fetch_url：本地服务取回并去标签；非法协议与连不上时返回错误文本")


if __name__ == "__main__":
    test_file_tools_roundtrip()
    test_fetch_url_local_server()
    print("全部通过。")
