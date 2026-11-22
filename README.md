# Jarvis — Personal Agent（Python，0.2.0）

DeepSeek 驱动的个人助手：agent loop（思考 → 调工具 → 看结果 → 再思考），能回答问题、改文件、跑命令、做规划。四个界面（命令行 / 全屏终端 / 桌面 / 网页）共用同一个核心。

线上网页版（不用安装，直接用）：https://jarvis-two-rho-72.vercel.app/

## 状态

0.2.0 已完成：记忆层、工具箱、规划模式、文件检查点、四界面、Vercel 部署。2026-11-22 刚做完两轮打磨：U0–U6 界面优化（事件模型、真实用量、全屏 TUI、桌面重写、计划/待办、Diff/检查点、浅色主题），U7 四界面统一（功能对照表、缺口补齐、共用视图状态层）。下一步：语音输入/输出、浏览器里直调 DeepSeek（BYOK 不再经过服务器转发）、向量检索替代关键词检索。更新日志见 `CHANGELOG.md`。

## 快速开始

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt              # 命令行 / TUI / 网页版
pip install -r requirements-gui.txt          # 再加这个才有桌面版（PySide6）

copy .env.example .env                        # 填入 DEEPSEEK_API_KEY
python tests/smoke_test.py                   # 验证 key 能通
```

key 在 https://platform.deepseek.com/api_keys 创建。模型 id 是 `deepseek-flash`（不要发 `deepseek-v4.1-1017`，那是权重版本号，不是 API 模型名）；base URL 默认 `https://api.deepseek.com`。

## 四种运行方式

| 界面 | 启动 | 适合 | 要知道 |
| --- | --- | --- | --- |
| **命令行 CLI** | `python -m jarvis.ui.chat`（`pip install -e .` 后直接用 `jarvis` 命令） | 脚本、一次问答、服务器上跑 | `jarvis --print "问题"` 只打答案；交互里 `/help` 看全部命令；`/plan` 规划、`/rewind` 恢复检查点、`/usage` 看用量 |
| **全屏终端 TUI** | `python -m jarvis.ui.tui` | 终端里长时间用 | 四层布局（顶栏 / 聊天流 / 输入框 / 状态栏），右侧栏 Tab 页（会话 / 待办 / Diff / 检查点），分隔条可拖；危险操作单键确认（`a` 允许一次 / `A` 总是允许 / `d` 拒绝 / `e` 编辑）；`?` 看快捷键；`/theme` 切深浅色；非 TTY 时自动回落到命令行 |
| **桌面 GUI** | `python -m jarvis.ui.gui`，或双击 `dist\jarvis-gui.exe` | 日常主力 | 气泡聊天 + 右侧 Tab 页（会话 / 待办 / Diff / 检查点 / 任务）；写文件前行内确认卡片（不再弹框）；顶栏模型与权限芯片、底部真 token / 费用；没 key 时弹首次启动向导；窗口尺寸会记住 |
| **网页版** | 本地 `uvicorn jarvis.ui.server:app`，打开 http://127.0.0.1:8000/；线上直接打开上面的 Vercel 地址 | 给别人用、手机上用 | **key 各用各的（BYOK）**：页面上方填自己的 DeepSeek key，只存自己浏览器，服务器不存不记；无检查点（Vercel 无磁盘）、Diff 只读、默认最多 6 步（函数 60 秒上限） |

exe 本地打包：`.\build-exe.ps1` 出 `dist\jarvis.exe`、`jarvis-gui.exe`、`jarvis-tui.exe`；打 tag 后 GitHub Actions 也会自动出 release。

## 能做什么

- **工具**：读/写/查文件、Shell、Git、取网页、搜索、记忆、委派子代理、后台任务、技能、MCP。文件工具只能动项目内文件；Shell 过危险命令黑名单，写操作要确认；每一步写审计日志（只记动作，不记内容和 key）。
- **规划模式**：`/plan <任务>` 先调研出带验收标准的计划，确认后执行，待办自动更新，结束对照验收标准自查（四个界面都有）。
- **记忆**：短期记忆按整轮裁剪、超上限自动压缩摘要；长期事实存 `memory.json`，启动时拼进 system prompt；会话存在 `~/.jarvis/sessions/`，关掉再开能接着聊（网页版存在浏览器 localStorage）。
- **检查点**：写文件前自动备份，`/rewind`、`/rewind-to <序号>` 或 GUI/TUI 的检查点页点一行恢复，只保留最近 50 个（网页版没有）。
- **权限三档**：`readonly`（只读）、`edit`（默认，写前确认）、`auto`（写直接放行）；危险命令（`rm -rf /`、`format` 等）任何档位都拒绝。
- **用量**：数字全是 API 返回的真值（流式 `include_usage`），拿不到显示“—”，绝不估算；`jarvis --usage` 看最近 7 天。

## 网页版（在线使用）

直接打开 https://jarvis-two-rho-72.vercel.app/ 就能用，不用安装。

页面上方填自己的 DeepSeek key（BYOK）：key 只存在你自己的浏览器里，每次请求一起发过来，服务器不存、不记；没 key 会报 400，提示填 key。

在线版的限制：没有文件检查点（Vercel 无磁盘）、Diff 只读、默认最多 6 步（一轮对话调太多工具可能超时）。想用完整版，用上面四种方式里的命令行 / TUI / GUI 在本地跑。

## 测试与开发

```powershell
python run_tests.py          # 全量测试：25 个文件，208 项，离线跑
```

注意：`build/` 与 `dist/` 是 PyInstaller 产物，不进 git（见 `.gitignore`）；exe 在本地用 `build-exe.ps1` 重新打。

代码结构与分层规则见 `docs/ARCHITECTURE.md`；日常使用手册见 `docs/usage-manual.md`；学习指南见《Personal Agent 学习指南：从概念到第一个 Agent Loop》。
