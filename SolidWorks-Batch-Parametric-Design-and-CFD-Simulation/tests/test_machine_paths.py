#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`machine_paths` —— 让项目能搬到别的电脑上跑的那层。

这里测的是**路径解析的规则**，不是"这台机器上装了什么"。所以除了项目内的断言，
其余全部打桩 —— 否则换个没装 SolidWorks 的机器跑测试就会红。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

MAPPING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAPPING / "scripts"))

import machine_paths as mp  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    """每例都从干净的缓存开始，免得互相污染。"""
    mp._cache = None
    yield
    mp._cache = None


def test_project_root_comes_from_the_file_not_a_constant():
    """项目内的路径必须由 `__file__` 推出 —— 这是"拷走就能用"的前提。"""
    assert mp.MAPPING_ROOT == MAPPING
    assert mp.PROJECT_ROOT == MAPPING.parent
    # 关键：模块里不该有任何写死的盘符
    source = (MAPPING / "scripts" / "machine_paths.py").read_text(encoding="utf-8")
    for needle in ("C:\\Users\\", "D:\\Program Files", "ProgramData"):
        assert needle not in source, f"machine_paths 里不该写死 {needle!r}"


def test_flow_template_ships_inside_the_project():
    """`.fwp` 是能拷进项目的资源，默认就该指向项目内的那份。"""
    assert mp.DEFAULT_FLOW_TEMPLATE == MAPPING / "assets" / "internal_water.fwp"
    assert mp.DEFAULT_FLOW_TEMPLATE.is_file(), "assets/ 下的模板丢了，换机器就带不走"


def test_derive_bincfw_follows_the_solidworks_layout(tmp_path):
    sw_dir = tmp_path / "SW2026" / "SOLIDWORKS"
    bin_dir = tmp_path / "SW2026" / "SOLIDWORKS Flow Simulation" / "binCFW"
    sw_dir.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    exe = sw_dir / "SLDWORKS.exe"
    exe.write_bytes(b"stub")

    assert mp.derive_bincfw(exe) == bin_dir


def test_derive_bincfw_returns_none_when_absent(tmp_path):
    exe = tmp_path / "SW" / "SOLIDWORKS" / "SLDWORKS.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")
    assert mp.derive_bincfw(exe) is None


def test_detect_never_raises_without_winreg(monkeypatch):
    """离线环境（没有 winreg）必须返回 None 而不是炸 —— 本模块要能被测试 import。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "winreg":
            raise ImportError("no winreg")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert mp.detect_solidworks_exe() is None


def test_config_overrides_detection(tmp_path, monkeypatch):
    """配置优先于注册表探测 —— 探测错了或装了多个版本时靠它纠正。"""
    exe = tmp_path / "SW" / "SLDWORKS.exe"
    bin_dir = tmp_path / "SW" / "binCFW"
    exe.parent.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    exe.write_bytes(b"stub")
    cfg = tmp_path / "machine_paths.json"
    cfg.write_text(json.dumps({"solidworks_exe": str(exe), "bincfw": str(bin_dir)}),
                   encoding="utf-8")
    monkeypatch.setattr(mp, "CONFIG_PATH", cfg)

    data = mp.resolve()

    assert data["solidworks_exe"] == exe
    assert data["bincfw"] == bin_dir
    assert data["sources"]["solidworks_exe"] == "config/machine_paths.json"


def test_stale_machine_override_is_rejected(tmp_path, monkeypatch):
    """整包复制时旧机器的覆盖路径不能被当成可用安装。"""
    cfg = tmp_path / "machine_paths.json"
    cfg.write_text(json.dumps({"solidworks_exe": str(tmp_path / "old" / "SLDWORKS.exe"),
                               "bincfw": str(tmp_path / "old" / "binCFW")}),
                   encoding="utf-8")
    monkeypatch.setattr(mp, "CONFIG_PATH", cfg)

    with pytest.raises(RuntimeError, match="solidworks_exe"):
        mp.solidworks_exe()
    with pytest.raises(RuntimeError, match="binCFW"):
        mp.flow_bincfw()


def test_config_relative_paths_resolve_against_the_project_root(tmp_path, monkeypatch):
    """配置里写相对路径时，按项目根解析 —— 不是按当前工作目录。"""
    monkeypatch.setattr(mp, "CONFIG_PATH", tmp_path / "machine_paths.json")
    monkeypatch.setattr(mp, "PROJECT_ROOT", tmp_path)
    (tmp_path / "machine_paths.json").write_text(
        json.dumps({"flow_template_fwp": "assets/custom.fwp"}), encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "custom.fwp").write_bytes(b"stub")

    assert mp.resolve()["flow_template_fwp"] == tmp_path / "assets" / "custom.fwp"


def test_missing_paths_become_actionable_errors_not_crashes(tmp_path, monkeypatch):
    """探测不到时要**说清楚试过什么、怎么补救**，不能丢一句 FileNotFoundError。"""
    monkeypatch.setattr(mp, "CONFIG_PATH", tmp_path / "nope.json")
    monkeypatch.setattr(mp, "detect_solidworks_exe", lambda: None)
    monkeypatch.setattr(mp, "DEFAULT_FLOW_TEMPLATE", tmp_path / "missing.fwp")

    data = mp.resolve()
    assert data["solidworks_exe"] is None
    assert data["bincfw"] is None
    joined = " | ".join(data["problems"])
    assert "没找到 SolidWorks" in joined      # 探测失败本身
    assert "推不出 binCFW" in joined          # 连带的推导失败
    assert "模板不存在" in joined             # 项目内资源缺失

    with pytest.raises(RuntimeError) as err:
        mp.solidworks_exe()
    message = str(err.value)
    # 报错要说清「试过什么」和「怎么补救」，而且要指向真正生效的那个配置文件
    assert "注册表" in message
    assert (tmp_path / "nope.json").name in message
    assert "solidworks_exe" in message

    with pytest.raises(RuntimeError, match="binCFW"):
        mp.flow_bincfw()


def test_describe_marks_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "CONFIG_PATH", tmp_path / "nope.json")
    monkeypatch.setattr(mp, "detect_solidworks_exe", lambda: None)
    text = mp.describe()
    assert "项目根" in text and "✗" in text


def test_resolve_is_cached_but_can_be_forced(tmp_path, monkeypatch):
    """注册表读取不该每次调用都做一遍。"""
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return None

    monkeypatch.setattr(mp, "detect_solidworks_exe", counting)
    monkeypatch.setattr(mp, "CONFIG_PATH", tmp_path / "nope.json")

    mp.resolve()
    mp.resolve()
    assert calls["n"] == 1
    mp.resolve(force=True)
    assert calls["n"] == 2


def test_python_floor_is_39_because_of_is_relative_to():
    """最低 3.9 —— 判据是 `Path.is_relative_to`。改这个数要一起改报错文案。"""
    assert mp.MIN_PYTHON == (3, 9)
    mp.require_python()          # 当前环境（3.9+）必须直接通过


def test_too_old_python_gets_a_readable_message():
    """版本不够时要报「用错解释器了」并打出完整路径，而不是深层的 AttributeError。"""
    with pytest.raises(SystemExit) as err:
        mp.require_python((99, 0))
    message = str(err.value)
    assert "Python 99.0" in message
    assert "正在用的解释器" in message
    assert "is_relative_to" in message, "要说清为什么需要这个版本"


def test_a_broken_config_file_says_so(tmp_path, monkeypatch):
    cfg = tmp_path / "machine_paths.json"
    cfg.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(mp, "CONFIG_PATH", cfg)

    with pytest.raises(RuntimeError, match="不是合法 JSON"):
        mp.resolve()
