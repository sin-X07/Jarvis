"""Smoke test for item 2: prove the DeepSeek key works with one plain call.

Run:  python tests/smoke_test.py   （在项目根目录下跑，.env 也在那里）
Success looks like:
    [OK] API 调用成功，模型 deepseek-flash 返回：
    你好！我是 DeepSeek...
"""

import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

api_key = os.getenv("DEEPSEEK_API_KEY")
if not api_key:
    raise SystemExit("未找到 DEEPSEEK_API_KEY：先复制 .env.example 为 .env 并填入 key。")

client = OpenAI(api_key=api_key, base_url=BASE_URL)
resp = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": "用一句话介绍你自己。"}],
)
print(f"[OK] API 调用成功，模型 {MODEL} 返回：")
print(resp.choices[0].message.content)
print(f"用量：{resp.usage.prompt_tokens} 输入 / {resp.usage.completion_tokens} 输出 tokens")
