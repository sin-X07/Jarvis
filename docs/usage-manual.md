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
| 全屏终端 | `python -m jarvis.ui.tui`（Textual；不是 TTY 时自动回落到命令行） |
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
| `edit`（默认） | 允许 | 内嵌确认卡片（写文件前先给你看 diff） | 拒绝 |
| `auto` | 允许 | 直接放行 | 拒绝 |

危险命令（`rm -rf /`、`format`、`del /s`、`reg delete` 等）任何档位都拒绝。
切换：CLI 里 `/permission auto`，或 GUI 设置对话框；TUI 里 `/permission`。
确认卡片上四个选择：允许一次 / 总是允许（本会话记住这类操作）/ 拒绝 / 编辑
（拒绝并把命令填回输入框，自己改了再发）。

## 命令行常用命令

```
jarvis --print "帮我写个备份脚本"     # 一次问答，只打答案
jarvis --sessions                     # 列出会话
jarvis --resume abc123                # 恢复会话（id 前缀）
jarvis --usage                        # 最近 7 天用量
jarvis --trajectory abc123            # 看一个会话的轨迹
jarvis --update                       # 检查有没有新版
```

交互里输入 `/help` 看全部 20 个命令，输入 `?` 看快捷键总览。最常用的几个：

- `/plan <任务>`：先出计划，确认后执行，结束对照验收标准自查
- `/todo`：看待办清单（执行计划时模型自己更新）
- `/diff`：显示工作区当前改动（按文件分组，含未跟踪文件）
- `/rewind [n]`：恢复最近 n 个文件检查点（写文件前自动备份）
- `/remember <内容>` / `/memory` / `/forget <内容>`：长期记忆
- `/jobs`：后台任务（`run_shell_background` 跑的长命令）
- `/trajectory`：当前会话的轨迹

## 计划与待办

- **计划模式**：`/plan <任务>`（CLI/TUI）或顶栏“📋 计划”开关开着发消息
  （GUI）、模式芯片切到“规划”（网页版）。先调研出带验收标准的计划，
  挂一张计划卡片：批准才执行，编辑改完再批准，拒绝什么都不做。
- **计划模式不提供写/执行工具**：规划档里模型只能读文件、跑读命令，写文件
  或跑写命令会被当场拒绝并提示“先完成调研和计划，确认后再执行”。
  三个界面同一条规则。
- **待办清单**：计划批准后自动生成，☐ 待做 / ◐ 进行中（同时只有一个）
  / ☑ 已完成，模型每完成一步自己调 `update_todo` 更新。三个界面看的是
  同一份清单，存在会话目录的 `<会话id>-todos.json` 里，恢复会话时一起回来。

## Diff 与检查点

- **Diff**：CLI `/diff`、TUI 的 Diff 页、GUI 的 Diff 页、网页版工具栏的
  Diff 按钮，都按文件分组显示工作区当前改动（含未跟踪文件），每组标
  `+N/-M`。接受全部 = 改动留在工作区不管；拒绝全部 = 按检查点把文件
  全部恢复（只回滚文件，对话内容不动）。网页版 Diff 是只读查看。
- **检查点**：每次写文件前自动备份在 `~/.jarvis/checkpoints/<会话id>/`，
  只保留最近 50 个。恢复办法：CLI `/rewind [n]`、GUI 检查点页点一行、
  悬停某条消息点“恢复检查点”（文件回到这条消息出现之前）、TUI 检查点
  时间线按 Enter。

## 主题切换

深色 / 浅色两套，颜色只在 `jarvis/ui/theme.json` / `light.json` 里改一处：

| 界面 | 怎么切 |
| --- | --- |
| GUI | 设置对话框里的主题下拉框 |
| TUI | `/theme`（深色 ↔ 浅色轮换） |
| 网页版 | 顶栏的 🌙/☀️ 芯片 |

选择记在 `~/.jarvis/ui.json`（GUI/TUI）或浏览器 localStorage（网页版），
下次启动还用它。

## 快捷键总览

四个界面同一张表（只列本界面真的能用的）：

| 界面 | 怎么看 |
| --- | --- |
| CLI | 输 `?` |
| TUI | 按 `?` |
| GUI | 帮助菜单 → 快捷键… |
| 网页版 | 工具栏的“⌨ 快捷键”按钮 |

共同的几条：Enter 发送 / Shift+Enter 换行；忙时发送按钮变“停止”，
点一下掐断本轮；忙时回车（CLI 没有）/ Tab 把追问排进队列，轮次结束
自动发；消息悬停可复制 / 重新生成；代码块右上角复制；工具卡片点开
看参数与输出预览。

## 首次启动与 key

- **GUI**：没 key 启动时自动弹首次启动向导（填 key、选模型、选档位），
  key 只写进本机项目 `.env`，不上传、不记日志。取消也不拦：窗口照常
  能用，发消息时给一张“去填 key”卡片。之后随时能在设置对话框里改 key。
- **TUI**：跑 `/setup` 填 key（同样写项目 `.env`），模型和档位进
  `~/.jarvis/config.json`。
- **CLI**：启动时没 key 会直接提示去 `.env` 填 `DEEPSEEK_API_KEY`。
- **网页版**：页面上方填 key，只存浏览器 localStorage。

## 错误恢复

三个界面都在原地出错误卡片，不会把报错吞掉或打死程序：

- 卡片写清原因，并附一句话恢复提示：429 限流（等一会儿再试）、key 无效
  或过期（去设置里重新填 key）、网络不通（检查网络或代理）、上下文超长
  （开新会话，或等自动压缩）、账户额度不足（去 DeepSeek 平台看余额）。
- **重试只给一次**：第一次出错给“重试”，重试一次还失败，改给
  “复制问题，开新会话”（问题复制到剪贴板，开新会话，填回输入框）。
  TUI 没有剪贴板：按 `n` 开新会话，问题填回输入框，按 Enter 再问一次。
- CLI 里报错不会退出 REPL，直接再发一次可重试；重试一次还失败，
  换个说法或 `/clear` 开新会话。

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
- 用量数字是真值：流式调用传 `stream_options={"include_usage": True}`，
  取最后一个 chunk 的 API usage；拿不到的调用记“未知”、界面显示“—”，
  绝不拿估算顶替。费用按 DeepSeek 闪电版峰值价算。计划轮的 token
  不进用量统计（`make_plan` 不带 UsageTracker）。
- `~/.jarvis/usage.jsonl` 是原始记录，`~/.jarvis/audit.jsonl` 是审计日志
  （只记动作和结果，不记对话内容和 key）。

## 常见问题

- **没 key**：`python -m jarvis.ui.chat` 会提示去 `.env` 填 `DEEPSEEK_API_KEY`；
  GUI 没 key 启动时自动弹向导；TUI 跑 `/setup`；网页版在页面上方填 key，只存浏览器。
- **写文件被拦住了**：先看是不是危险命令，再看权限档位（`readonly` 直接拒绝，
  `edit` 要点确认）；`before_write` 钩子失败也会拦，输出里会写原因。
- **上下文太长**：超过上限 70% 会自动把旧对话压缩成摘要，工具链不断。
- **检查点**：写文件前自动备份在 `~/.jarvis/checkpoints/<会话id>/`，
  `/rewind` 或 GUI“恢复检查点”按钮恢复，只恢复最近 50 个。
- **更新**：`jarvis --update` 只检查不安装；有新版去 GitHub releases 下。
