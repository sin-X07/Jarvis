"""P1 测试：权限门三档（不用 key、不联网）。

验证：
  1. 读动作三档都放行
  2. 只读模式：写动作直接拒绝，不弹确认
  3. edit 模式：写动作有确认回调时按回调结果；无回调时放行
  4. auto 模式：写动作不确认直接放行
  5. review_diff：只读拒绝、无回调放行、有回调按回调结果
  6. 非法档位报错
"""

from jarvis.safety.permissions import ApprovalGate, current_gate, set_current_gate


def test_read_always_allowed():
    for mode in ("readonly", "edit", "auto"):
        gate = ApprovalGate(mode=mode)
        ok, _ = gate.approve({"kind": "read_file", "target": "README.md"})
        assert ok, mode
    print("[OK] 权限门：读动作三档都放行")


def test_readonly_denies_write():
    gate = ApprovalGate(mode="readonly", confirm=lambda a: True)
    ok, reason = gate.approve({"kind": "write_file", "target": "x.txt"})
    assert not ok and "只读" in reason, (ok, reason)
    ok, _ = gate.approve({"kind": "shell", "target": "ls"})
    assert not ok
    assert gate.review_diff("x.txt", "+hi") is False
    print("[OK] 权限门：只读模式写动作直接拒绝，不弹确认")


def test_edit_confirms():
    seen = []
    gate = ApprovalGate(mode="edit", confirm=lambda a: seen.append(a) or True)
    ok, _ = gate.approve({"kind": "write_file", "target": "x.txt"})
    assert ok and len(seen) == 1, seen
    gate2 = ApprovalGate(mode="edit", confirm=lambda a: False)
    ok, reason = gate2.approve({"kind": "shell", "target": "rm x"})
    assert not ok and "拒绝" in reason, (ok, reason)
    print("[OK] 权限门：edit 模式写动作按确认回调结果")


def test_edit_no_confirm_auto_allows():
    gate = ApprovalGate(mode="edit", confirm=None)
    ok, _ = gate.approve({"kind": "write_file", "target": "x.txt"})
    assert ok
    assert gate.review_diff("x.txt", "+hi") is True
    print("[OK] 权限门：无确认回调时（直接调库）自动放行")


def test_auto_no_confirm():
    seen = []
    gate = ApprovalGate(mode="auto", confirm=lambda a: seen.append(a) or False)
    ok, _ = gate.approve({"kind": "write_file", "target": "x.txt"})
    assert ok and not seen, seen
    print("[OK] 权限门：auto 模式写动作不确认")


def test_bad_mode():
    try:
        ApprovalGate(mode="yolo")
    except ValueError:
        print("[OK] 权限门：非法档位报错")
        return
    raise AssertionError("非法档位该报错")


def test_current_gate_roundtrip():
    gate = ApprovalGate(mode="readonly")
    set_current_gate(gate)
    try:
        assert current_gate() is gate
    finally:
        set_current_gate(None)
    assert current_gate().mode == "edit"  # 恢复默认
    print("[OK] 权限门：进程内当前 gate 可装可卸")


if __name__ == "__main__":
    test_read_always_allowed()
    test_readonly_denies_write()
    test_edit_confirms()
    test_edit_no_confirm_auto_allows()
    test_auto_no_confirm()
    test_bad_mode()
    test_current_gate_roundtrip()
    print("全部通过。")
