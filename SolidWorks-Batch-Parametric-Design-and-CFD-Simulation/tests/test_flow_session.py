#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flow_session 里**不需要 SolidWorks** 的那部分：模板完整性、锁文件、路径守卫。

连接相关的部分（attach / gate / probe_alive）只能在真机上验，见 plan §八。
这里先把「模板不能被改动」这条最要命的门禁钉死 —— 它是所有回滚决策的依据。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MAPPING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAPPING / "scripts"))

import flow_session as fs  # noqa: E402


def test_flow_session_imports_without_pywin32():
    """这个模块必须能离线 import —— COM 只能在 attach() 里出现。"""
    source = (MAPPING / "scripts" / "flow_session.py").read_text(encoding="utf-8")
    header = source.split("def attach", 1)[0]
    assert "import win32com" not in header, "win32com 不得出现在模块顶层"
    assert "import pythoncom" not in header, "pythoncom 不得出现在模块顶层"


def test_template_manifest_is_intact():
    """母版必须逐字节与清单相符。这条挂了，所有跑出来的样本都不可信。"""
    result = fs.verify_template_manifest()
    assert result["ok"], "模板哈希不符：\n  " + "\n  ".join(result["problems"])
    assert result["template_id"] == "sealed_flow_capless_v6"
    assert len(result["checked"]) == 7, "模板应为装配体 + 6 个核心零件"


def test_manifest_detects_a_tampered_file(tmp_path):
    """改动母版里任何一个字节都必须被抓到 —— 这是回滚决策的唯一依据。"""
    fake_template = tmp_path / "tpl"
    shutil.copytree(fs.TEMPLATE_DIR, fake_template)
    victim = next(p for p in fake_template.iterdir() if p.suffix == ".SLDPRT")
    victim.write_bytes(victim.read_bytes() + b"\x00")

    result = fs.verify_template_manifest(fs.MANIFEST_PATH, fake_template)
    assert not result["ok"]
    assert any(victim.name in p for p in result["problems"]), result["problems"]


def test_manifest_detects_missing_file(tmp_path):
    fake_template = tmp_path / "tpl"
    shutil.copytree(fs.TEMPLATE_DIR, fake_template)
    victim = next(p for p in fake_template.iterdir() if p.suffix == ".SLDPRT")
    victim.unlink()

    result = fs.verify_template_manifest(fs.MANIFEST_PATH, fake_template)
    assert not result["ok"]
    assert any("缺文件" in p for p in result["problems"])


def test_extra_files_are_reported_but_do_not_fail_the_gate(tmp_path):
    """清单只登记 7 个 CAD 文件；`_project_folders.html` 是 Flow 写的索引，不是 CAD 数据。

    「装配体里还挂着 Flow 工程」这件事必须靠打开文档读 GetProjectNames() 判断，
    不能靠「目录里多了个文件」来判 —— 否则每天都会误报。
    """
    fake_template = tmp_path / "tpl"
    shutil.copytree(fs.TEMPLATE_DIR, fake_template)
    (fake_template / "多出来的.SLDPRT").write_bytes(b"x")

    result = fs.verify_template_manifest(fs.MANIFEST_PATH, fake_template)
    assert result["ok"], "多出文件不应判失败"
    assert "多出来的.SLDPRT" in result["extra_files"]


def test_real_template_reports_the_flow_index_file():
    """母版里确实躺着一个 `_project_folders.html` —— 它是悬空工程注册的症状。"""
    result = fs.verify_template_manifest()
    assert any(n.endswith("_project_folders.html") for n in result["extra_files"])


def test_lock_file_detection(tmp_path):
    """`~$*` 残留锁文件会让适配器的 Single() 抛「序列包含一个以上的元素」。"""
    (tmp_path / "a.SLDPRT").write_bytes(b"x")
    assert fs.lock_files_under(tmp_path) == []

    (tmp_path / "~$a.SLDPRT").write_bytes(b"\x00" * 12)
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "~$b.SLDASM").write_bytes(b"\x00" * 12)

    found = {p.name for p in fs.lock_files_under(tmp_path)}
    assert found == {"~$a.SLDPRT", "~$b.SLDASM"}


def test_sha256_matches_the_manifest_entries():
    manifest = json.loads(fs.MANIFEST_PATH.read_text(encoding="utf-8"))
    for name, want in manifest["files"].items():
        assert fs.sha256_of(fs.TEMPLATE_DIR / name) == want


def test_runs_root_is_where_runs_live():
    assert fs.RUNS_ROOT.name == "seven_variable_trials"
    assert fs.RUNS_ROOT.parent.name == "working"
    assert fs.MAPPING_ROOT.name == "SolidWorks-Batch-Parametric-Design-and-CFD-Simulation"


def test_revision_prefix_matches_this_machine():
    """本机是 SolidWorks 2026 SP3.2 → Rev 34.3.2。门禁按 '34.' 开头判。"""
    assert fs.REQUIRED_REVISION_PREFIX == "34."


def test_process_listing_uses_csv_parser_and_keeps_window_title(monkeypatch):
    output = ('"SLDWORKS.exe","123","Console","1","100,000 K","Running",'
              '"USER","0:00:01","NVOGLDC invisible"\n'
              '"SLDWORKS.exe","456","Console","1","200,000 K","Running",'
              '"USER","0:00:02","SOLIDWORKS 2026"\n')
    monkeypatch.setattr(fs.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=output, stderr=""))

    processes = fs.list_solidworks_processes()

    assert [(p.pid, p.window_title) for p in processes] == [
        (123, "NVOGLDC invisible"), (456, "SOLIDWORKS 2026")]
    assert fs.list_solidworks_pids() == [123, 456]


def test_cleanup_only_kills_the_verified_invisible_process(monkeypatch):
    hidden = fs.SolidWorksProcess(123, "NVOGLDC invisible", "Running")
    visible = fs.SolidWorksProcess(456, "SOLIDWORKS 2026", "Running")
    listings = iter([[hidden, visible], [hidden, visible], [visible]])
    monkeypatch.setattr(fs, "list_solidworks_processes", lambda: next(listings))
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")

    monkeypatch.setattr(fs.subprocess, "run", fake_run)

    report = fs.cleanup_empty_solidworks_processes()

    assert calls == [["taskkill", "/PID", "123", "/T", "/F"]]
    assert report["removed"] == [123]
    assert report["remaining"] == [456]
    assert report["ambiguous_not_touched"] == [456]


# --------------------------------------------- 继承的工程 vs 自己的工程


class _FakeProject:
    def __init__(self, directory: str) -> None:
        self.ProjectFiles = SimpleNamespace(ProjectDirectory=directory)


class _FakeConfiguration:
    """够 `assert_no_flow_project` 用：列工程、激活（只为问出目录）、删工程。"""

    def __init__(self, projects: dict) -> None:
        self._projects = dict(projects)          # 名字 -> 落盘目录
        self.removed: list = []

    def GetProjectNames(self):
        return list(self._projects)

    def ActivateProject(self, name, activate):
        directory = self._projects.get(name)
        if directory is None or str(directory).startswith("!"):
            return None                          # 激活不了 = 目录问不出来
        return _FakeProject(str(directory))

    def RemoveProject(self, name):
        self._projects.pop(name, None)
        self.removed.append(name)
        return True


def _bare_session() -> fs.SwSession:
    """绕开 `__init__`（它要连 COM）—— `assert_no_flow_project` 不碰别的属性。"""
    return object.__new__(fs.SwSession)


def test_inherited_project_is_removed_but_the_runs_own_project_is_kept(tmp_path):
    """门禁要删的是**模板继承来的**悬空注册，不是我们自己存进去的工程。

    回归（2026-09-21 实测）：停在求解前那一次会把工程保存进装配体，求解那一次
    开文档后本该直接激活它。不加区分地全删 → S6 激活不到、只能重建，白花 ~20 秒，
    还要把 9 个特征重写一遍。
    """
    run = tmp_path / "flow_x"
    configuration = _FakeConfiguration({
        "流体力学仿真": run / "1",                              # 我们自己存的 → 留
        "inherited": tmp_path / "assembly_batch_v6" / "1",      # 模板继承的 → 删
    })

    report = _bare_session().assert_no_flow_project(
        configuration, remove=True, keep_under=run)

    assert configuration.removed == ["inherited"]
    assert report["kept"] == ["流体力学仿真"]
    assert report["projects_after"] == ["流体力学仿真"]
    assert report["ok"], "留下的那个不该触发「副本里仍挂着工程」那条报错"


def test_without_keep_under_every_project_is_removed(tmp_path):
    """不给 `keep_under` 就是老行为（全删）—— 首次跑那次要的正是这个。"""
    run = tmp_path / "flow_x"
    configuration = _FakeConfiguration({"流体力学仿真": run / "1"})

    report = _bare_session().assert_no_flow_project(configuration, remove=True)

    assert configuration.removed == ["流体力学仿真"]
    assert report["projects_after"] == []


def test_project_pointing_outside_the_run_is_removed(tmp_path):
    """目录在别处 = 继承来的注册，必须删 —— 留着它 S8 会去读别人的 xmlconfig。"""
    run = tmp_path / "flow_x"
    configuration = _FakeConfiguration({"流体力学仿真": tmp_path / "another_run" / "1"})

    report = _bare_session().assert_no_flow_project(
        configuration, remove=True, keep_under=run)

    assert configuration.removed == ["流体力学仿真"]
    assert report["kept"] == []
    assert report["projects_after"] == []


def test_unactivatable_project_is_removed_not_kept(tmp_path):
    """激活不了（目录问不出来）→ 当「不是我们的」删掉，不硬留。"""
    run = tmp_path / "flow_x"
    configuration = _FakeConfiguration({"流体力学仿真": "!unactivatable"})

    report = _bare_session().assert_no_flow_project(
        configuration, remove=True, keep_under=run)

    assert configuration.removed == ["流体力学仿真"]
    assert report["kept"] == []


def test_leftover_project_still_raises_when_removal_did_not_take(tmp_path):
    """删不掉就必须报错 —— 这条老门禁不能被 `keep_under` 削弱。"""
    run = tmp_path / "flow_x"

    class _Stubborn(_FakeConfiguration):
        def RemoveProject(self, name):
            return False                         # 没删掉，名字还在表里

    configuration = _Stubborn({"inherited": tmp_path / "assembly_batch_v6" / "1"})
    with pytest.raises(fs.GateFailure):
        _bare_session().assert_no_flow_project(
            configuration, remove=True, keep_under=run)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# --------------------------------------------- 定期重启（内存累积）


def test_memory_column_is_parsed_with_thousands_separators():
    """tasklist 给的是 `"1,412,345 K"`，逗号必须吃掉。认不出要返回 None 而不是抛。"""
    assert fs._parse_memory_kb("1,412,345 K") == 1412345
    assert fs._parse_memory_kb("100,000 K") == 100000
    assert fs._parse_memory_kb("488448") == 488448
    for bad in ("", "   ", "N/A", None):
        assert fs._parse_memory_kb(bad) is None


def test_process_listing_keeps_memory_and_picks_the_largest(monkeypatch):
    output = ('"SLDWORKS.exe","123","Console","1","100,000 K","Running",'
              '"USER","0:00:01","SOLIDWORKS 2026"\n'
              '"SLDWORKS.exe","456","Console","1","1,412,345 K","Running",'
              '"USER","0:00:02","SOLIDWORKS 2026"\n')
    monkeypatch.setattr(fs.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=output, stderr=""))

    processes = fs.list_solidworks_processes()

    assert [p.memory_mb for p in processes] == pytest.approx([97.65625, 1379.2431640625])
    # 两个实例时取最大的那个 —— 内存只用于观测，不做判据
    assert fs.solidworks_memory_mb() == pytest.approx(1379.2431640625)


def test_process_listing_falls_back_when_tasklist_is_denied(monkeypatch):
    def fake_run(command, **_kwargs):
        if command[0] == "tasklist":
            return SimpleNamespace(returncode=1, stdout="", stderr="Access denied")
        return SimpleNamespace(returncode=0,
                               stdout='{"Id":123,"WorkingSet64":104857600,"MainWindowTitle":"SW"}',
                               stderr="")

    monkeypatch.setattr(fs.subprocess, "run", fake_run)
    processes = fs.list_solidworks_processes()

    assert [(p.pid, p.memory_mb, p.window_title) for p in processes] == [(123, 100.0, "SW")]


def test_solver_detection_falls_back_instead_of_guessing_false(monkeypatch):
    def fake_run(command, **_kwargs):
        if command[0] == "tasklist":
            return SimpleNamespace(returncode=1, stdout="", stderr="Access denied")
        return SimpleNamespace(returncode=0,
                               stdout='{"Id":456,"WorkingSet64":100,"MainWindowTitle":""}',
                               stderr="")

    monkeypatch.setattr(fs.subprocess, "run", fake_run)
    assert fs.solver_running() is True


def test_restart_refuses_while_a_solver_is_running(monkeypatch):
    """安全约束：**绝不能在有 EFDsolver 时重启。**

    `solver_running()` 是按镜像名全局匹配的（不认 PID/父进程），留一个孤儿求解器
    会让下一轮 `wait_for_result()` 永远等不到「求解器已退出」。
    """
    monkeypatch.setattr(fs, "solver_running", lambda: True)
    monkeypatch.setattr(fs, "list_solidworks_pids", lambda: [123])
    called = []
    monkeypatch.setattr(fs, "_stop_solidworks_once", lambda pids: called.append(pids))

    report = fs.restart_solidworks()

    assert report["ok"] is False
    assert "EFDsolver" in report["why"]
    assert called == [], "有求解器在跑时不得碰进程"


def test_restart_refuses_when_the_instance_count_is_ambiguous(monkeypatch):
    monkeypatch.setattr(fs, "solver_running", lambda: False)
    monkeypatch.setattr(fs, "list_solidworks_pids", lambda: [123, 456])
    called = []
    monkeypatch.setattr(fs, "_stop_solidworks_once", lambda pids: called.append(pids))

    report = fs.restart_solidworks()

    assert report["ok"] is False
    assert "不猜" in report["why"]
    assert called == []


def test_restart_happy_path_returns_the_new_pid(monkeypatch):
    monkeypatch.setattr(fs, "solver_running", lambda: False)
    # 只被调两次：进循环前取 before、重启后取 after（stop/launch 都被打桩了）
    pids = iter([[123], [456]])
    monkeypatch.setattr(fs, "list_solidworks_pids", lambda: next(pids, [456]))
    monkeypatch.setattr(fs, "solidworks_memory_mb", lambda: 1400.0)
    monkeypatch.setattr(fs, "_stop_solidworks_once",
                        lambda pids: {"method": "ExitApp", "ok": True})
    monkeypatch.setattr(fs, "_launch_solidworks_once",
                        lambda exe, bincfw: {"ok": True, "probe_alive": True})

    report = fs.restart_solidworks()

    assert report["ok"] is True
    assert report["pids_after"] == [456]
    assert report["pid_changed"] is True
    assert len(report["attempts"]) == 1


def test_restart_gives_up_after_the_retry_budget(monkeypatch):
    monkeypatch.setattr(fs, "solver_running", lambda: False)
    monkeypatch.setattr(fs, "list_solidworks_pids", lambda: [123])
    monkeypatch.setattr(fs, "solidworks_memory_mb", lambda: None)
    monkeypatch.setattr(fs, "_stop_solidworks_once", lambda pids: {"ok": False, "why": "关不掉"})

    report = fs.restart_solidworks(retries=3)

    assert report["ok"] is False
    assert len(report["attempts"]) == 3
    assert "重试 3 次" in report["why"]


def test_stop_falls_back_to_taskkill_only_after_rechecking_the_pid(monkeypatch):
    """ExitApp 不生效才杀；且杀前必须复核 PID 没变 —— 变了就绝不猜。

    ⚠️ **必须打桩 `_exit_app_via_com`。** 不打桩的话，这个测试会真的对着
    用户正在跑的 SolidWorks 调 `ExitApp()` —— 跑一次 pytest 就掀掉一次批处理。
    """
    monkeypatch.setattr(fs, "_exit_app_via_com", lambda: "GetActiveObject 失败（打桩）")
    monkeypatch.setattr(fs, "_wait_for_no_process",
                        lambda timeout_s: {"gone": False, "remaining": [123]})
    # 复核时列出来的 PID 与传进来的不一致 → 说明进程已变，不该动手
    monkeypatch.setattr(fs, "list_solidworks_processes",
                        lambda: [fs.SolidWorksProcess(999, "别的", "Running")])
    calls = []
    monkeypatch.setattr(fs.subprocess, "run", lambda cmd, **k: (
        calls.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr="")))

    report = fs._stop_solidworks_once([123])

    assert report["ok"] is False
    assert calls == [], "PID 对不上时不得 taskkill"
    assert "不猜" in report["why"]


def test_no_unit_test_may_reach_real_solidworks():
    """**护栏测试**：任何单元测试都不许碰到真的 SolidWorks。

    起因：`test_stop_falls_back_to_taskkill_...` 起初直接调了 `_stop_solidworks_once`，
    而那个函数真的会 `ExitApp()` —— 于是每跑一次 pytest，用户正在跑的批处理就被掀掉一次。
    这类错误必须靠结构挡住，不能靠记性。
    """
    source = (Path(__file__).resolve().parents[1] / "scripts" / "flow_session.py"
              ).read_text(encoding="utf-8")
    # 真正动手关进程的只有这两个函数，名字和 docstring 都要把危险讲清楚
    assert "⚠️ 会真的关掉 SolidWorks" in source, "_stop_solidworks_once 的危险提示丢了"
    assert "_exit_app_via_com" in source, "COM 退出必须抽成可打桩的独立函数"
    for name in ("_exit_app_via_com", "_stop_solidworks_once"):
        body = source.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]
        assert "打桩" in body, f"{name} 的 docstring 必须提醒测试打桩"


# --------------------------------------------- COM 成员：属性 vs 方法


def test_no_bare_revisionnumber_call_in_the_production_path():
    """**换机器踩过的坑，用测试锁住。**

    SolidWorks 的 COM 成员在不同 pywin32 绑定下有的暴露成方法、有的暴露成属性。
    `RevisionNumber` 在本机是方法（`sw.RevisionNumber()` 能用），在另一台机器
    （Anaconda 自带 pywin32）是属性 —— 加括号直接 `TypeError: 'str' object is not
    callable`，而且门禁和探活**两条路一起炸**。

    这个坑项目早有记录（`sw_api.py:21`：`comp.GetPathName` ✓ 属性，加括号会炸），
    但门禁和探活那两处一直直接调用 —— 只跑一台机器时看不出来。
    读取一律走 `flow_session._member()`（内部是 `sw_api.safe_get`）。
    """
    import ast
    source = (MAPPING / "scripts" / "flow_session.py").read_text(encoding="utf-8")
    # 用 AST 找**真实的调用节点** —— 文本匹配会把讲解这个坑的注释和 docstring 也算上
    calls = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "RevisionNumber"
    ]
    assert not calls, (
        f"flow_session.py 里还有 {len(calls)} 处直接调用 .RevisionNumber() —— "
        "换台 pywin32 绑定不同的机器就会 'str' object is not callable。\n"
        '请改用 _member(sw, "RevisionNumber")')


def test_member_helper_uses_the_compat_layer():
    """`_member` 必须委托给 `sw_api.safe_get` —— 别自己重造一套判断。"""
    source = (MAPPING / "scripts" / "flow_session.py").read_text(encoding="utf-8")
    body = source.split("def _member(", 1)[1].split("\ndef ", 1)[0]
    assert "swa.safe_get(" in body


def test_safe_get_survives_a_string_member():
    """直接验证兼容层行为：成员是字符串时原样返回，不当成方法来调。"""
    class Fake:
        RevisionNumber = "34.3.2"          # 属性形态（另一台机器的样子）

    assert fs._member(Fake(), "RevisionNumber") == "34.3.2"

    class FakeMethod:
        def RevisionNumber(self):          # 方法形态（本机的样子）
            return "34.3.2"

    assert fs._member(FakeMethod(), "RevisionNumber") == "34.3.2"
