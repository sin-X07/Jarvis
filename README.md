# Jarvis — Personal Agent（Python 版，0.2.0）

一个能跑的个人助手：用 DeepSeek 的 API，通过 agent loop
（思考 → 调工具 → 看结果 → 再思考）回答问题、改文件、跑命令、
做规划。三个界面（命令行 / 桌面 GUI / 网页）共用同一个核心。

## 快速开始

```powershell
# 1. 建虚拟环境并装依赖
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-gui.txt   # 桌面版；只要命令行装 requirements.txt

# 2. 配 key：复制 .env.example 为 .env，填入你的 DeepSeek API key
#    key 在 https://platform.deepseek.com/api_keys 创建

# 3. 先验证 key 能通
python smoke_test.py

# 4. 跑起来
python chat.py                        # 交互式对话
python chat.py --print "现在几点？"    # 一次问答
python gui.py                         # 桌面版
uvicorn server:app --reload           # 网页版，打开 http://127.0.0.1:8000/

# 5. 离线测试（不用 key、不联网）
python run_tests.py                   # 全量测试，19 个文件 117 项
```

Windows 上也可以直接用打包好的 exe：打 tag 后 GitHub Actions 自动出
`jarvis.exe`（命令行）和 `jarvis-gui.exe`（桌面版），挂在 release 上；
本地打包跑 `.\build-exe.ps1`。

## 模型名注意

DeepSeek 官方 API 上，DeepSeek-V4.1-Flash-1017 的模型 id 是
`deepseek-flash`（旧 id `deepseek-v4-flash` 也能用，会路由到同一版本）。
不要发 `deepseek-v4.1-1017`——那是权重版本号，不是 API 模型名。
默认 base URL：`https://api.deepseek.com`。

## 命令行

```
jarvis --print "问题"        一次问答，只打答案
jarvis --sessions            列出会话
jarvis --resume <id 前缀>     恢复一个会话继续聊
jarvis --usage               最近 7 天用量小结
jarvis --trajectory <id>     看一个会话的轨迹
jarvis --update              检查 GitHub 上有没有新版
jarvis --version             打印版本号
```

交互里输入 `/help` 看全部命令。最常用的几个：

| 命令 | 干什么 |
| --- | --- |
| `/plan <任务>` | 规划模式：先调研出计划，确认后执行，结束对照验收标准自查 |
| `/todo` | 看待办清单（执行计划时模型自己更新） |
| `/rewind [n]` | 恢复最近 n 个文件检查点（写文件前自动备份） |
| `/resume [id]` / `/fork` / `/sessions` | 恢复 / 分叉 / 列出会话 |
| `/remember <内容>` / `/memory` / `/forget <内容>` | 长期记忆 |
| `/model [名字]` / `/status` / `/permission [档位]` | 换模型 / 看状态 / 换权限档位 |
| `/jobs` / `/usage` / `/trajectory` | 后台任务 / 用量 / 当前会话轨迹 |
| `/clear` / `/quit` | 清空本轮对话 / 退出 |

## 权限三档

| 档位 | 读文件/读命令 | 写文件/写命令 | 危险命令 |
| --- | --- | --- | --- |
| `readonly` | 允许 | 拒绝 | 拒绝 |
| `edit`（默认） | 允许 | 弹框确认（写文件前先给你看 diff） | 拒绝 |
| `auto` | 允许 | 直接放行 | 拒绝 |

危险命令（`rm -rf /`、`format`、`del /s`、`reg delete` 等）任何档位都拒绝。
切换：CLI 里 `/permission auto`，或 GUI 设置对话框。
配置优先级：命令行参数 > 环境变量 > `~/.jarvis/config.json` > 默认值。

## 工具

文件、Shell、Git、搜索、记忆、委派、后台任务、技能、MCP 工具都在
`tools.py` / `subagent.py` / `jobs.py` / `skills.py` / `mcp_client.py` 里。
安全边界：文件工具只能动项目内的文件（`../` 越狱路径会被拦）；
Shell 先过危险命令黑名单和工作区检查，写操作要确认，输出截断、
超时 30 秒（可配），环境变量里不带 key；每一步都写审计日志
`~/.jarvis/audit.jsonl`（只记动作和结果，不记对话内容和 key）。

## 会话、记忆、规划

- **会话**：桌面版和 CLI 的会话存在 `~/.jarvis/sessions/`，一个会话一个
  `.jsonl` 文件，关掉再开能接着聊；网页版的会话存在浏览器 localStorage。
- **短期记忆**：`ConversationMemory` 按“整轮”裁掉最旧的对话（绝不断开
  tool 调用链）；上下文超上限 70% 时自动把旧对话压缩成摘要。
- **长期记忆**：`FactStore` 把事实存进 `memory.json`，启动时拼进 system
  prompt；模型也可以主动调 `remember_fact` / `recall_facts` 读写；
  事实超过 10 条时按问题检索，只拼最相关的 5 条。
- **规划模式**：`/plan` 先调研出带验收标准的计划，确认后执行，待办清单
  自动更新，结束时对照验收标准自查一遍。
- **检查点**：写文件前自动备份在 `~/.jarvis/checkpoints/<会话id>/`，
  `/rewind` 或 GUI“恢复检查点”按钮恢复，只保留最近 50 个。

## 技能、MCP、Hooks、插件

- **技能**：`skills/` 里的 `.md` 文件就是技能：开头写 `name:` 和
  `description:`，正文是执行流程。直接说“按 code-review 技能评审一下
  这次改动”，模型会先读技能全文再干活。自己的技能放 `~/.jarvis/skills/`，
  也会被自动发现。内置 4 个：`code-review`、`write-tests`、
  `project-onboarding`、`github-trending-summary`。
- **MCP**：`mcp.json`（项目里或 `~/.jarvis/mcp.json`）配 MCP 服务器，
  它们的工具会接进来，名字是 `mcp__<服务器>_<工具>`。示例见
  `mcp.json.example`（filesystem + github，需要 `npx` 和 GitHub token）。
- **Hooks**：`~/.jarvis/hooks.json`，三个时机点 `before_write` /
  `before_shell` / `on_session_end`，每条是一条 shell 命令；
  `before_write` 的钩子返回非零就拦住写入。
- **插件**：`plugins/` 里放一个 `.py`，定义 `TOOL_SCHEMA` + `TOOL_FUNC`，
  就是一个新工具，不用改 `tools.py`。

## 桌面版

- 左侧聊天区（Markdown 渲染）、右侧会话列表 + 长期记忆 + 用量显示。
- 设置对话框：改模型、权限档位、最大步数，保存写回 `~/.jarvis/config.json`。
- “恢复检查点”“轨迹”按钮：恢复文件备份、看当前会话的时间线。
- 写操作在 edit 档位会弹确认框；线程规矩：`AgentWorker` 在 QThread 里
  跑循环，工作线程绝不碰界面控件，结果只通过信号传回主线程。

## 网页版

- 和桌面版是同一个核心，只是壳不同：浏览器 `fetch` 发 `POST /api/chat`，
  再用 SSE 把回答一个字一个字接回来，工具调用显示成一行小灰字。
- 状态在浏览器那边：多会话、历史和长期事实存在 localStorage，每次请求
  一起发过来，服务器跑完一轮，把更新过的历史和事实放在最后的 `done`
  事件里还回去（`done` 里还有本次用量和耗时）。这样服务器两次请求之间
  不留状态，才能放到 Vercel 这种无状态的 serverless 上。
- API key 是各用各的（BYOK）：页面上方有个 key 输入框，访客填自己的
  DeepSeek key，存在自己浏览器的 localStorage 里，每次请求一起发过来；
  服务器只拿它调这一次 DeepSeek，不存、不缓存、不记日志。请求没带 key
  时，服务器才用环境变量里的 `DEEPSEEK_API_KEY` 兜底（本地开发就靠这个）；
  两个都没有就报 400，提示填 key。
- 请求日志：每次调 `/api/chat` 记一行 JSON 日志，只有时间、耗时（毫秒）、
  成功失败、失败原因、key 来源（`byok` / `env` / `none`）。不记消息内容、
  历史、事实，更不记 key。
- 网页版默认最多 6 步（`WEB_MAX_STEPS`）：Vercel 免费版函数最长跑 60 秒，
  步数太多容易超时。

### 部署到 Vercel

1. 把仓库推到 GitHub，Vercel 里 New Project → Import 这个仓库。
2. 环境变量：**不要**在 Vercel 上设 `DEEPSEEK_API_KEY`——设了它，不填 key
   的访客就会用你的 key。每个访客在页面上填自己的 key。
   `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` 不填就用默认值。
3. Deploy。部署完打开给你的域名就能用。
4. 限制要知道：免费版函数最长 60 秒（一轮对话如果调很多次工具可能超时）；
   `memory.json` 在 Vercel 上写不进磁盘，长期记忆靠浏览器 localStorage
   带着走；key 是访客自己的，但请求要经过你的服务器转发——服务器不存 key，
   不过理论上服务器能看到它，这是 BYOK 模式的固有前提，真要完全不经过你，
   就得在浏览器里直调 DeepSeek（那是下一步的改法）。

## 用量与费用

- 每轮结束 CLI 会打一行用量；`jarvis --usage` 看最近 7 天。
- 费用是估算值：流式调用的 token 按字符估算，价格按 DeepSeek 闪电版峰值价。
- 日预算（`daily_budget_usd`，0 = 不提醒）：当天花到 80% / 超限时提醒。
- `~/.jarvis/usage.jsonl` 是原始记录；`jarvis --update` 只检查新版不安装。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `agent.py` | agent loop 本体：发消息 → 模型要么回答、要么要求调工具 → 执行工具 → 把结果喂回去 → 循环 |
| `tools.py` | 基础工具：时间、计算器、读/写/查文件、取网页、Git、搜索 |
| `shell_tools.py` / `policy.py` / `permissions.py` / `audit.py` / `diffutil.py` | Shell、危险命令黑名单、权限三档、审计日志、写前 diff |
| `memory.py` | 记忆层：短期记忆（整轮裁剪）与长期记忆（`memory.json` 事实库 + 检索 + 记忆工具） |
| `config.py` / `model.py` / `usage.py` / `jlog.py` | 配置、模型调用（重试）、用量与费用、结构化日志 |
| `session_store.py` / `compaction.py` | 会话持久化、上下文压缩 |
| `todo.py` / `checkpoint.py` / `planner.py` | 待办清单、文件检查点、规划模式 |
| `subagent.py` / `jobs.py` | 子代理委派、后台任务 |
| `skills.py` / `skills/` / `mcp_client.py` / `hooks.py` / `plugins.py` | 技能、MCP 客户端、Hooks、插件加载 |
| `md.py` | 极简 Markdown → ANSI / HTML（CLI 与 GUI 共用） |
| `trajectory.py` / `update.py` | 轨迹回放、自更新检查 |
| `chat.py` | 命令行入口：REPL + 一次问答 + 会话/用量/轨迹/更新命令 |
| `worker.py` | GUI 的后台线程：`AgentWorker` 在 QThread 里跑循环，结果用信号发回主线程，写操作确认也走信号 |
| `gui.py` | 图形界面：聊天区、会话列表、记忆侧栏、设置、检查点、轨迹 |
| `server.py` | 网页版后端：FastAPI，`GET /` 返回聊天页，`POST /api/chat` 用 SSE 流式返回 |
| `api/index.py` / `vercel.json` | Vercel 入口与配置 |
| `jarvis.spec` / `build-exe.ps1` / `.github/workflows/build.yml` | PyInstaller 打包（Windows 上跑）、CI 打 tag 自动出 exe |
| `CHANGELOG.md` / `docs/usage-manual.md` | 更新日志、日常使用手册 |
| `run_tests.py` / `test_*.py` | 全量测试入口与 19 个测试文件（117 项，离线跑） |
| `.env.example` / `requirements.txt` / `requirements-gui.txt` / `pyproject.toml` | 配置模板、依赖、打包配置 |

## 常见问题

- **没 key**：`python chat.py` 会提示去 `.env` 填 `DEEPSEEK_API_KEY`；
  网页版在页面上方填 key，只存浏览器。
- **写文件被拦住了**：先看是不是危险命令，再看权限档位（`readonly` 直接拒绝，
  `edit` 要点确认）；`before_write` 钩子失败也会拦，输出里会写原因。
- **上下文太长**：超过上限 70% 会自动把旧对话压缩成摘要，工具链不断。
- **检查点**：写文件前自动备份在 `~/.jarvis/checkpoints/<会话id>/`，
  `/rewind` 或 GUI“恢复检查点”按钮恢复，只恢复最近 50 个。
- **更新**：`jarvis --update` 只检查不安装；有新版去 GitHub releases 下。

## 下一步

- 语音输入/输出（本轮没做）
- 浏览器里直调 DeepSeek（BYOK 不再经过服务器转发）
- 向量检索替代 bigram 关键词检索
- 详见学习指南：《Personal Agent 学习指南：从概念到第一个 Agent Loop》
