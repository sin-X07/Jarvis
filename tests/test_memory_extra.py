"""P3 测试：长期记忆升级——删除/修改/检索拼 prompt（不用 key、不联网）。

验证：
  1. delete：删掉一条事实，删不存在的有提示
  2. update：改一条事实，改不存在的有提示
  3. render_for_prompt：事实 ≤10 条全量拼；>10 条且有 query 只拼相关的 5 条
  4. 记忆工具新增 delete_fact / update_fact 能被模型调用
"""

import os
import tempfile

from jarvis.memory.memory import FactStore, memory_tools


def _store_with(n):
    tmp = tempfile.mkdtemp()
    store = FactStore(path=os.path.join(tmp, "memory.json"))
    for i in range(n):
        store.add(f"用户喜欢的颜色是蓝色{i}号" if i == 0 else f"事实{i}号：内容{i}")
    return store


def test_delete_update():
    store = _store_with(2)
    assert "已忘记" in store.delete("事实1号：内容1"), store.all_texts()
    assert len(store.all_texts()) == 1
    assert "没找到" in store.delete("不存在的事实")
    assert "已更新" in store.update("用户喜欢的颜色是蓝色0号", "用户喜欢的颜色是红色")
    assert "用户喜欢的颜色是红色" in store.all_texts()
    assert "没找到" in store.update("不存在", "新内容")
    assert "空的" in store.update("用户喜欢的颜色是红色", "   ")
    print("[OK] 记忆：删除/修改事实正常，找不到有提示")


def test_render_retrieval():
    store = _store_with(12)
    # 事实 ≤10 条时全量拼（先验证小库）
    small = _store_with(3)
    assert "事实1号" in small.render_for_prompt(), small.render_for_prompt()
    # 12 条 + 有 query：只拼相关的
    rendered = store.render_for_prompt(query="喜欢的颜色")
    assert "蓝色0号" in rendered, rendered
    assert "事实11号" not in rendered, rendered
    assert rendered.count("- ") <= 5, rendered
    # 12 条 + 没 query：拼最近 10 条
    rendered2 = store.render_for_prompt()
    assert "事实11号" in rendered2, rendered2
    print("[OK] 记忆：事实多了走检索，只拼相关的 5 条")


def test_memory_tools_delete_update():
    store = _store_with(2)
    schemas, funcs = memory_tools(store)
    names = [s["function"]["name"] for s in schemas]
    assert "delete_fact" in names and "update_fact" in names, names
    assert "已忘记" in funcs["delete_fact"]("事实1号：内容1")
    assert "已更新" in funcs["update_fact"]("用户喜欢的颜色是蓝色0号",
                                            "用户喜欢的颜色是绿色")
    assert any("绿色" in t for t in store.all_texts())
    print("[OK] 记忆工具：delete_fact / update_fact 可调用")


if __name__ == "__main__":
    test_delete_update()
    test_render_retrieval()
    test_memory_tools_delete_update()
    print("全部通过。")
