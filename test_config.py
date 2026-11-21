"""P0 测试：配置集中化与校验（不用 key、不联网）。

验证：
  1. 默认值正确，显式 overrides 生效
  2. 环境变量能覆盖默认值，非法值报错
  3. validate() 拦下配错的项（步数、档位、超时）
  4. 用户配置文件缺失或损坏时不拦启动
"""

import os
import tempfile

from config import JarvisConfig, load_config


def test_defaults():
    cfg = JarvisConfig()
    assert cfg.model == "deepseek-flash", cfg.model
    assert cfg.base_url == "https://api.deepseek.com", cfg.base_url
    assert cfg.max_steps == 10 and cfg.permission == "edit", cfg
    assert cfg.validate() == [], cfg.validate()
    print("[OK] 配置：默认值正确，校验通过")


def test_overrides():
    cfg = JarvisConfig(max_steps=5, permission="readonly")
    assert cfg.max_steps == 5 and cfg.permission == "readonly"
    print("[OK] 配置：显式 overrides 生效")


def test_env_override():
    env = dict(os.environ)
    env["AGENT_MAX_STEPS"] = "7"
    env["JARVIS_PERMISSION"] = "auto"
    cfg = load_config(env=env)
    assert cfg.max_steps == 7 and cfg.permission == "auto", cfg
    print("[OK] 配置：环境变量覆盖默认值")


def test_env_bad_value():
    env = dict(os.environ)
    env["AGENT_MAX_STEPS"] = "不是数字"
    try:
        load_config(env=env)
    except ValueError as exc:
        assert "max_steps" in str(exc) or "整数" in str(exc), exc
        print("[OK] 配置：环境变量非法值报错")
        return
    raise AssertionError("非法环境变量没报错")


def test_validate_catches_bad():
    bad = JarvisConfig(max_steps=0)
    assert bad.validate(), "步数 0 该被拦下"
    bad = JarvisConfig(permission="yolo")
    assert any("readonly" in p for p in bad.validate()), bad.validate()
    bad = JarvisConfig(timeout=-1)
    assert bad.validate(), "负超时该被拦下"
    print("[OK] 配置：validate() 拦下配错的项")


def test_missing_config_file_ok():
    # 用户配置文件不存在时不拦启动（用临时 HOME 隔离）
    tmp = tempfile.mkdtemp()
    old_home, old_profile = os.environ.get("HOME"), os.environ.get("USERPROFILE")
    os.environ["HOME"] = tmp
    # Windows 上 expanduser("~") 看 USERPROFILE 不看 HOME，两个都得换。
    os.environ["USERPROFILE"] = tmp
    try:
        cfg = load_config(env={})
        assert cfg.model == "deepseek-flash"
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_profile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_profile
    print("[OK] 配置：没有用户配置文件时按默认值启动")


def test_bad_config_file_ok():
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, ".jarvis"), exist_ok=True)
    with open(os.path.join(tmp, ".jarvis", "config.json"), "w", encoding="utf-8") as f:
        f.write("{ 这不是合法 JSON")
    old_home, old_profile = os.environ.get("HOME"), os.environ.get("USERPROFILE")
    os.environ["HOME"] = tmp
    os.environ["USERPROFILE"] = tmp
    try:
        cfg = load_config(env={})
        assert cfg.model == "deepseek-flash"
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_profile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_profile
    print("[OK] 配置：用户配置文件损坏时不拦启动")


if __name__ == "__main__":
    test_defaults()
    test_overrides()
    test_env_override()
    test_env_bad_value()
    test_validate_catches_bad()
    test_missing_config_file_ok()
    test_bad_config_file_ok()
    print("全部通过。")
