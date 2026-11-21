# plugins/ — 自定义工具插件

在这个目录放一个 `.py` 文件就能加一个工具，不用改 `tools.py`。

约定：每个文件定义两个东西——

```python
TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "shout",
        "description": "大声说",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}
def TOOL_FUNC(text: str) -> str:
    return text.upper() + "!"
```

加载规则（`plugins.py`）：按文件名排序加载；名字冲突时后面的覆盖前面的
并打印警告；import 失败或缺少 `TOOL_SCHEMA` / `TOOL_FUNC` 的文件会被跳过，
不影响别的插件和主流程。

插件也能访问 `tools.py` 的沙箱——想读文件用 `tools.read_text_file`，
不要自己 open 任意路径。
