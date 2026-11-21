# Jarvis Starter — 最小 Agent Loop（Python 版）

一个能跑的最小 personal agent 起手项目：用 DeepSeek 的 API，
通过 agent loop（思考 → 调工具 → 看结果 → 再思考）回答问题。

## 快速开始

```powershell
# 1. 建虚拟环境并装依赖
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. 配 key：复制 .env.example 为 .env，填入你的 DeepSeek API key
#    key 在 https://platform.deepseek.com/api_keys 创建

# 3. 先验证 key 能通
python smoke_test.py

# 4. 跑 agent
python agent.py "现在几点？12*(3+4) 等于多少？"
python agent.py "帮我读一下 README.md，告诉我这个项目是干什么的"

# 5. 离线测试循环逻辑（不用 key、不联网）
python test_loop.py
```

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `agent.py` | agent loop 本体：发消息 → 模型要么回答、要么要求调工具 → 执行工具 → 把结果喂回去 → 循环 |
| `tools.py` | 工具：查时间、计算器、读项目内文件。加工具 = 写一个函数 + 一段 schema + 注册进 `TOOL_FUNCS` |
| `smoke_test.py` | 一次最普通的 API 调用，验证 key 和模型名 |
| `test_loop.py` | 离线测试：用假模型验证循环逻辑，不花钱、不联网 |
| `.env.example` | 配置模板，复制为 `.env` 后填 key |
| `requirements.txt` | 依赖：`openai`（DeepSeek 兼容 OpenAI 接口）、`python-dotenv` |

## 模型名注意

DeepSeek 官方 API 上，DeepSeek-V4.1-Flash-1017 的模型 id 是
`deepseek-flash`（旧 id `deepseek-v4-flash` 也能用，会路由到同一版本）。
不要发 `deepseek-v4.1-1017`——那是权重版本号，不是 API 模型名。
默认 base URL：`https://api.deepseek.com`。

## 下一步

- 加工具：在 `tools.py` 里照着现有的三个写，文件读写、日程、发消息都可以接进来
- 加记忆：把每轮对话记到文件里，下次启动时读回来当上下文
- 加规划：复杂任务先让模型拆成步骤，再一步步执行
- 详见学习指南：《Personal Agent 学习指南：从概念到第一个 Agent Loop》
