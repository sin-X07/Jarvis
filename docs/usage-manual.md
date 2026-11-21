# Jarvis 日常使用手册（0.2.0）

## 安装

```bash
git clone https://github.com/sin-X07/Jarvis.git
cd Jarvis/python-version
pip install -r requirements-gui.txt   # 桌面版；只要命令行就装 requirements.txt
cp .env.example .env                  # 填入 DEEPSEEK_API_KEY
```

Windows 上也可以直接用打包好的 exe：打 tag 后 GitHub Actions 会自动出
`jarvis.exe`（命令行）和 `jarvis-gui.exe`（桌面版），挂在 release 上；
本地打包跑 `.\build-exe.ps1`。

## 启动

| 界面 | 启动方式 |
| --- | --- |
| 命令行 | `python -m jarvis.ui.chat`（交互）或 `jarvis "问题"`（一次问答，需 `pip install -e .`） |
| 桌面 | `python -m jarvis.ui.gui` |
| 网页 | `uvicorn jarvis.ui.server:app`，打开 http://127.0.0.1:8000/（key 填在页面上方，只存浏览器） |

## 配置

配置优先级：命令行参数 > 环境变量 > `~/.jarvis/config.json` > 默认值。
常用项（`~/.jarvis/config.json` 里改，或 GUI 设置对话框里改）：

| 项 | 默认 | 说明 |
| --- | --- | --- |
| `model` | `deepseek-flash` | 模型名 |
| `permission` | `edit` | 权限档位，见下 |
| `max_steps` | 12 | 一轮最多工具调用步数 |
| `max_budget_usd` | 0（不限） | 单轮费用上限，超了停下 |
| `daily_budget_usd` | 0（不提醒） | 日预算，超 80% / 超限时提醒 |
| `context_limit` | 1000000 | 上下文 token 上限，超 70% 自动压缩 |

## 权限三档

| 档位 | 读文件/跑读命令 | 写文件/跑写命令 | 危险命令 |
| --- | --- | --- | --- |
| `readonly` | 允许 | 拒绝 | 拒绝 |
| `edit`（默认） | 允许 | 弹框确认（会先给你看 diff） | 拒绝 |
| `auto` | 允许 | 直接放行 | 拒绝 |

危险命令（`rm -rf /`、`format`、`del /s`、`reg delete` 等）任何档位都拒绝。
切换：CLI 里 `/permission auto`，或 GUI 设置对话框。

## 命令行常用命令

```
jarvis --print "帮我写个备份脚本"     # 一次问答，只打答案
jarvis --sessions                     # 列出会话
jarvis --resume abc123                # 恢复会话（id 前缀）
jarvis --usage                        # 最近 7 天用量
jarvis --trajectory abc123            # 看一个会话的轨迹
jarvis --update                       # 检查有没有新版
```

交互里输入 `/help` 看全部 20 个命令。最常用的几个：

- `/plan <任务>`：先出计划，确认后执行，结束对照验收标准自查
- `/todo`：看待办清单（执行计划时模型自己更新）
- `/rewind [n]`：恢复最近 n 个文件检查点（写文件前自动备份）
- `/remember <内容>` / `/memory` / `/forget <内容>`：长期记忆
- `/jobs`：后台任务（`run_shell_background` 跑的长命令）
- `/trajectory`：当前会话的轨迹

## 会话

桌面版和 CLI 的会话存在 `~/.jarvis/sessions/`，一个会话一个 `.jsonl` 文件，
关掉再开能接着聊（`--resume` 或 GUI 里点会话列表）。
网页版的会话存在浏览器 localStorage，换设备、清缓存就没了。

## 技能（Skills）

`skills/` 里的 `.md` 文件就是技能：开头写 `name:` 和 `description:`，
正文是执行流程。直接说“按 code-review 技能评审一下这次改动”，
模型会先读技能全文再干活。自己的技能放 `~/.jarvis/skills/`，
也会被自动发现。

内置 4 个：`code-review`（评审改动）、`write-tests`（按现有风格写离线测试）、
`project-onboarding`（读陌生项目出结构说明）、`github-trending-summary`（热榜总结）。

## MCP

`mcp.json`（项目里或 `~/.jarvis/mcp.json`）配 MCP 服务器，Jarvis 会把
它们的工具接进来，名字是 `mcp__<服务器>_<工具>`。示例见 `mcp.json.example`
（filesystem + github 两个 server，需要 `npx` 和 GitHub token）。

## Hooks 与插件

- Hooks：`~/.jarvis/hooks.json`，三个时机点 `before_write` / `before_shell` /
  `on_session_end`，每条是一条 shell 命令；`before_write` 的钩子返回非零
  就拦住写入。`{file}` 会被替换成正在写的文件路径。
- 插件：`plugins/` 里放一个 `.py`，定义 `TOOL_SCHEMA` + `TOOL_FUNC`，
  就是一个新工具，不用改 `tools.py`。

## 用量与费用

- 每轮结束 CLI 会打一行用量；`jarvis --usage` 看最近 7 天。
- 费用是估算值：流式调用的 token 按字符估算，价格按 DeepSeek 闪电版峰值价。
- `~/.jarvis/usage.jsonl` 是原始记录，`~/.jarvis/audit.jsonl` 是审计日志
  （只记动作和结果，不记对话内容和 key）。

## 常见问题

- **没 key**：`python -m jarvis.ui.chat` 会提示去 `.env` 填 `DEEPSEEK_API_KEY`；
  网页版在页面上方填 key，只存浏览器。
- **写文件被拦住了**：先看是不是危险命令，再看权限档位（`readonly` 直接拒绝，
  `edit` 要点确认）；`before_write` 钩子失败也会拦，输出里会写原因。
- **上下文太长**：超过上限 70% 会自动把旧对话压缩成摘要，工具链不断。
- **检查点**：写文件前自动备份在 `~/.jarvis/checkpoints/<会话id>/`，
  `/rewind` 或 GUI“恢复检查点”按钮恢复，只恢复最近 50 个。
- **更新**：`jarvis --update` 只检查不安装；有新版去 GitHub releases 下。
