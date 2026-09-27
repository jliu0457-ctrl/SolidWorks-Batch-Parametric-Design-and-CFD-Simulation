#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run-Batch 里**不需要 SolidWorks** 的那部分：读表、编号命名、跨表去重。

读表那几条最要紧 —— 读错了不是报错，是**悄悄跑错样本或漏跑样本**，
而这批要跑好几个小时，等发现时已经晚了。所以这里专挑「静默出错」的路径测。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook

MAPPING = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAPPING / "scripts"))
sys.path.insert(0, str(MAPPING))

import flow_geometry as fg  # noqa: E402


def _load_batch():
    """脚本名带连字符（`Run-Batch.py`），不是合法标识符，只能用 importlib 装。"""
    path = MAPPING / "scripts" / "Run-Batch.py"
    spec = importlib.util.spec_from_file_location("Run_Batch", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["Run_Batch"] = module
    spec.loader.exec_module(module)
    return module


B = _load_batch()

DESIGN = {"c_mm": 32.0, "e_mm": 3.7, "phi_deg": 8.25,
          "alpha_deg": 35.5, "Dmax_mm": 191.3, "bm_mm": 7.5, "ds_mm": 45.0}
HEADER = [fg.ID_COLUMNS[0], *fg.DESIGN_COLUMNS]


def _write_xlsx(path: Path, header, rows) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(list(header))
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    workbook.close()
    return path


def _row(sample_id=1, **overrides):
    design = dict(DESIGN)
    design.update(overrides)
    return [sample_id] + [design[c] for c in fg.DESIGN_COLUMNS]


# ---------------------------------------------------------------- 读表

def test_reads_a_normal_table(tmp_path):
    path = _write_xlsx(tmp_path / "a.xlsx", HEADER, [_row(1), _row(2, c_mm=31.0)])
    designs = B.read_designs_from_xlsx(path)

    assert [d["样本序号"] for d in designs] == [1, 2]
    assert designs[0]["c_mm"] == 32.0
    assert designs[1]["c_mm"] == 31.0
    assert set(designs[0]) == {fg.ID_COLUMNS[0], *fg.DESIGN_COLUMNS}


def test_column_order_does_not_matter(tmp_path):
    """按**表头名字**取列，不按位置 —— 别人把列顺序改了也必须读对。"""
    shuffled = list(reversed(HEADER))
    values = dict(zip(HEADER, _row(7, Dmax_mm=188.5)))
    path = _write_xlsx(tmp_path / "a.xlsx", shuffled, [[values[h] for h in shuffled]])

    designs = B.read_designs_from_xlsx(path)
    assert designs[0]["样本序号"] == 7
    assert designs[0]["Dmax_mm"] == 188.5
    assert designs[0]["c_mm"] == 32.0


def test_missing_column_is_an_error(tmp_path):
    header = [h for h in HEADER if h != "ds_mm"]
    path = _write_xlsx(tmp_path / "a.xlsx", header, [[v for h, v in zip(HEADER, _row(1)) if h != "ds_mm"]])
    with pytest.raises(ValueError, match="缺列"):
        B.read_designs_from_xlsx(path)


def test_duplicate_ids_are_an_error(tmp_path):
    """编号撞车 = 两个样本会写同一个 run 目录，必须当场拦下。"""
    path = _write_xlsx(tmp_path / "a.xlsx", HEADER, [_row(3), _row(3, c_mm=31.0)])
    with pytest.raises(ValueError, match="重复"):
        B.read_designs_from_xlsx(path)


def test_blank_rows_are_skipped(tmp_path):
    path = _write_xlsx(tmp_path / "a.xlsx", HEADER, [_row(1), [None] * 8, _row(2)])
    assert [d["样本序号"] for d in B.read_designs_from_xlsx(path)] == [1, 2]


def test_non_numeric_cell_is_an_error(tmp_path):
    """空单元格不得被当成 0 —— 那会造出一个没人在意的坏设计点。"""
    bad = _row(1)
    bad[3] = None
    path = _write_xlsx(tmp_path / "a.xlsx", HEADER, [bad])
    with pytest.raises(ValueError, match="不是数字"):
        B.read_designs_from_xlsx(path)


def test_row_without_an_id_is_an_error(tmp_path):
    """有数据没编号 → 报错。静默跳过就等于悄悄少跑一个样本。"""
    path = _write_xlsx(tmp_path / "a.xlsx", HEADER, [[None] + _row(1)[1:]])
    with pytest.raises(ValueError, match="没有编号"):
        B.read_designs_from_xlsx(path)


def test_out_of_domain_design_is_rejected(tmp_path):
    """走的是和单样本同一套校验（valve_mapping.validate_design）。"""
    path = _write_xlsx(tmp_path / "a.xlsx", HEADER, [_row(1, alpha_deg=190.0)])
    with pytest.raises(ValueError):
        B.read_designs_from_xlsx(path)


# ---------------------------------------------------------------- 编号 → run 名

def test_run_name_is_safe_and_deterministic():
    assert B.run_name_for(7) == "sample_7"
    assert B.run_name_for("3200") == "sample_3200"
    assert B.run_name_for(7) == B.run_name_for(7)


def test_run_name_sanitises_unsafe_ids():
    """编号里带空格/点/中文时不能直接当目录名 —— 换成下划线，且仍要合法。"""
    assert B.run_name_for("S 07") == "sample_S_07"
    assert B.run_name_for("a.b") == "sample_a_b"
    assert B.run_name_for("样本1") == "sample___1"


# ---------------------------------------------------------------- 跨表去重

def test_collect_designs_merges_the_whole_folder(tmp_path, monkeypatch):
    _write_xlsx(tmp_path / "a.xlsx", HEADER, [_row(1), _row(2)])
    _write_xlsx(tmp_path / "b.xlsx", HEADER, [_row(3)])
    monkeypatch.setattr(B, "VARIABLE_DIR", tmp_path)

    designs, sources = B.collect_designs(None)
    assert [d["样本序号"] for d in designs] == [1, 2, 3]
    assert sorted(set(sources)) == ["a.xlsx", "b.xlsx"]


def test_cross_file_duplicate_ids_are_an_error(tmp_path, monkeypatch):
    """两份表出现同一个编号 → 会互相覆盖 run 目录，必须拦下。"""
    _write_xlsx(tmp_path / "a.xlsx", HEADER, [_row(1)])
    _write_xlsx(tmp_path / "b.xlsx", HEADER, [_row(1, c_mm=31.0)])
    monkeypatch.setattr(B, "VARIABLE_DIR", tmp_path)

    with pytest.raises(SystemExit, match="撞车"):
        B.collect_designs(None)


def test_empty_folder_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "VARIABLE_DIR", tmp_path)
    with pytest.raises(SystemExit, match="没有 .xlsx"):
        B.collect_designs(None)


# ---------------------------------------------------------------- 失败之后停不停

def test_a_failed_sample_does_not_stop_the_batch(monkeypatch):
    """**默认口径是失败继续跑** —— 这是常态，不是例外。"""
    monkeypatch.setattr(B.fses, "probe_alive", lambda *a, **k: (True, "ok"))
    stop, why = B.should_stop_after_failure(1, limit=B.DEFAULT_MAX_CONSECUTIVE_FAILURES)
    assert stop is False, f"SolidWorks 还活着、又没到次数上限，就不该停（{why}）"


def test_an_isolated_failure_never_stops_the_batch(monkeypatch):
    """偶尔挂一个、后面又成功 —— 中间的单个失败不该累积成"要停"。"""
    monkeypatch.setattr(B.fses, "probe_alive", lambda *a, **k: (True, "ok"))
    limit = B.DEFAULT_MAX_CONSECUTIVE_FAILURES
    for one_failure in (1, 2, limit - 1):
        assert B.should_stop_after_failure(one_failure, limit=limit)[0] is False


def test_a_dead_solidworks_stops_immediately_even_below_the_limit(monkeypatch):
    """**探活是主判据**：SolidWorks 没了就该立刻停，不用等凑够次数。

    这是"环境坏了"和"这个设计点造不出来"的区分点 —— 光看次数分不开，
    探活能直接问出来。所以它优先于计数。
    """
    monkeypatch.setattr(B.fses, "probe_alive", lambda *a, **k: (False, "MK_E_UNAVAILABLE"))
    stop, why = B.should_stop_after_failure(1, limit=B.DEFAULT_MAX_CONSECUTIVE_FAILURES)
    assert stop is True, "才失败 1 次，但 SolidWorks 都没了 —— 该停"
    assert "MK_E_UNAVAILABLE" in why, "停下时要带上原因"


def test_the_counter_stops_at_the_limit(monkeypatch):
    """连续失败到上限就停 —— 兜底，防"原因各异但一直在倒"。"""
    monkeypatch.setattr(B.fses, "probe_alive", lambda *a, **k: (True, "ok"))
    limit = B.DEFAULT_MAX_CONSECUTIVE_FAILURES
    assert B.should_stop_after_failure(limit - 1, limit=limit)[0] is False, "差一次不该停"
    stop, why = B.should_stop_after_failure(limit, limit=limit)
    assert stop is True and "上限" in why


def test_limit_zero_means_never_stop_for_counting(monkeypatch):
    """`--max-consecutive-failures 0` = 只按探活停，不看次数。"""
    monkeypatch.setattr(B.fses, "probe_alive", lambda *a, **k: (True, "ok"))
    assert B.should_stop_after_failure(999, limit=0)[0] is False


# ---------------------------------------------------------------- 真实数据（只验不变量）

def test_the_real_variable_table_reads_cleanly():
    """`Variables/` 里真放着的表必须能读通 —— 这是批量开跑的第一步。

    只断言**不变量**，不钉具体数值：这份表是别人放的，随时会换。
    """
    files = sorted(p for p in B.VARIABLE_DIR.glob("*.xlsx") if not p.name.startswith("~$"))
    if not files:
        pytest.skip("Variables/ 里没有表 —— 还没放数据")
    designs, sources = B.collect_designs(None)

    assert designs, "读出来一个样本都没有"
    ids = [d["样本序号"] for d in designs]
    assert len(ids) == len(set(ids)), "编号必须唯一"
    for design in designs:
        assert set(design) == {fg.ID_COLUMNS[0], *fg.DESIGN_COLUMNS}
        for name in fg.DESIGN_COLUMNS:
            assert isinstance(design[name], float), f"{name} 必须是 float"
    assert len(sources) == len(designs)


# ---------------------------------------------------------------- 跑完之后的清理

def _fake_run(tmp_path, run, *, with_report=True):
    """造一个像样的 run 目录：CAD 副本 + Flow 结果 + 报告。"""
    d = tmp_path / run
    (d / "1").mkdir(parents=True)
    (d / "8“D94R3Y-CL600C-11蝶板.SLDPRT").write_bytes(b"x" * 2048)
    (d / "1" / "1.fbd").write_bytes(b"y" * 4096)
    (d / "flow_sample_state.json").write_text("{}", encoding="utf-8")
    if with_report:
        (d / "flow_sample.json").write_text('{"status":"completed"}', encoding="utf-8")
        (d / "training_sample.csv").write_text("a,b\n", encoding="utf-8")
    return d


def test_success_deletes_the_copy_but_keeps_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "RUNS_ROOT", tmp_path)
    reports = tmp_path / "_sample_reports"
    monkeypatch.setattr(B, "REPORTS_DIR", reports)
    _fake_run(tmp_path, "sample_7")

    kept = B.keep_sample_report("sample_7")

    assert not (tmp_path / "sample_7").exists(), "整个副本目录应当没了"
    assert (reports / "sample_7.flow_sample.json").is_file()
    assert (reports / "sample_7.training_sample.csv").is_file()
    assert kept and "flow_sample" in kept


def test_report_dir_holds_nothing_heavy(tmp_path, monkeypatch):
    """只搬报告不删重文件，等于这件事没做。"""
    monkeypatch.setattr(B, "RUNS_ROOT", tmp_path)
    reports = tmp_path / "_sample_reports"
    monkeypatch.setattr(B, "REPORTS_DIR", reports)
    _fake_run(tmp_path, "sample_8")

    B.keep_sample_report("sample_8")

    names = [p.name for p in reports.iterdir()]
    assert not any(n.endswith((".SLDPRT", ".SLDASM", ".fbd", ".fld", ".cpt")) for n in names)
    assert not any("state" in n for n in names), "续跑状态是死重量，阶段轨迹报告里已有"


def test_a_run_without_the_main_report_is_not_deleted(tmp_path, monkeypatch):
    """主报告不在 = 这个目录不是"跑成功的样子"。宁可留着占地方，也不要删完什么都不剩。"""
    monkeypatch.setattr(B, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(B, "REPORTS_DIR", tmp_path / "_sample_reports")
    _fake_run(tmp_path, "sample_9", with_report=False)

    assert B.keep_sample_report("sample_9") is None
    assert (tmp_path / "sample_9").is_dir(), "不该删"


def test_missing_run_dir_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "RUNS_ROOT", tmp_path)
    monkeypatch.setattr(B, "REPORTS_DIR", tmp_path / "_sample_reports")
    assert B.keep_sample_report("根本没有这个 run") is None


def test_cleanup_is_only_wired_into_the_success_branch():
    """清理只能挂在成功分支上。

    挂到失败分支就出事了：`Run-FlowSample.py --resume` 靠比对盘上的几何哈希判断能不能续跑
    （`resume_is_safe`），而失败样本恰恰是最需要续跑的那种。删了目录，续跑这条路就断了。
    """
    source = (MAPPING / "scripts" / "Run-Batch.py").read_text(encoding="utf-8")
    assert source.count("keep_sample_report(") == 2, "一处定义 + 一处调用，多出来的调用要交代清楚"
    # 只看主循环：按 `else:` 把成功分支和失败分支切开
    loop = source.split("for n, design in enumerate(pending", 1)[1]
    loop = loop.split("print(f\"\\n{'=' * 60}\")", 1)[0]
    ok_branch, marker, fail_branch = loop.partition("\n        else:\n")
    assert marker, "没能切开成功/失败分支 —— 主循环的结构变了，这条断言要跟着改"
    assert "keep_sample_report(" in ok_branch
    assert "keep_sample_report(" not in fail_branch, \
        "失败分支不能调清理 —— 样本还没跑完，删掉 run 目录会断掉 --resume"


# --------------------------------------------- 定期重启 SolidWorks（防内存累积）


def test_restart_every_defaults_to_fifty_and_zero_disables(monkeypatch):
    args = B.build_parser().parse_args([])
    assert args.restart_every == B.DEFAULT_RESTART_EVERY == 50
    assert B.build_parser().parse_args(["--restart-every", "0"]).restart_every == 0
    assert B.build_parser().parse_args(["--restart-every", "7"]).restart_every == 7


def test_child_runner_uses_python_and_returns_output():
    code, tail = B._run([sys.executable, "-c", "print('portable-child-ok')"], 10)
    assert code == 0
    assert "portable-child-ok" in tail


def test_child_runner_times_out_with_captured_output():
    import subprocess

    with pytest.raises(subprocess.TimeoutExpired) as caught:
        B._run([sys.executable, "-c", "import time; print('before-sleep', flush=True); "
                "time.sleep(5)"], 1)
    assert b"before-sleep" in (caught.value.output or b"")


def test_cad_refuses_a_stale_success_report(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(B, "MAPPING_ROOT", tmp_path)
    monkeypatch.setattr(B, "DESIGNS_DIR", tmp_path / "designs")
    monkeypatch.setattr(B, "RUNS_ROOT", tmp_path / "runs")
    report = tmp_path / "_analysis" / "e2e_sample_1.json"
    report.parent.mkdir()
    report.write_text(json.dumps({"completed": True, "physical_mapping_verified": True}))
    monkeypatch.setattr(B, "_run", lambda *a, **k: (0, "EXE exited without a new report"))

    result = B.run_one(1, DESIGN, None)

    assert result["ok"] is False
    assert result["stage"] == "cad"
    assert "未产生本次的新报告" in result["why"]


def test_failed_preflight_does_not_archive_fresh_state(tmp_path, monkeypatch):
    state_path = tmp_path / "batch_state.json"
    state_path.write_text('{"schema_version": 1, "samples": {}}', encoding="utf-8")
    monkeypatch.setattr(B, "STATE_PATH", state_path)
    monkeypatch.setattr(B, "collect_designs", lambda _path: ([{"样本序号": 1, **DESIGN}], ["one.xlsx"]))
    monkeypatch.setattr(B, "_run", lambda *a, **k: (1, "Doctor failed"))

    assert B.main(["--execute", "--fresh", "--strict-preflight"]) == 2
    assert state_path.is_file()
    assert not list(tmp_path.glob("batch_state-*.json"))


def test_failed_preflight_is_advisory_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "MAPPING_ROOT", tmp_path)
    monkeypatch.setattr(B, "collect_designs", lambda _path: ([{"样本序号": 1, **DESIGN}], ["one.xlsx"]))
    monkeypatch.setattr(B, "load_state", lambda: {"schema_version": 1, "samples": {}})
    monkeypatch.setattr(B, "_run", lambda *a, **k: (1, "Doctor failed"))
    called = []
    monkeypatch.setattr(B, "run_one", lambda *a: called.append(True) or
                        {"ok": True, "run": "sample_1", "cv": 1})
    monkeypatch.setattr(B, "record", lambda *a, **k: None)
    monkeypatch.setattr(B, "keep_sample_report", lambda _run: None)

    assert B.main(["--execute", "--limit", "1", "--restart-every", "0"]) == 0
    assert called == [True]


def test_requirements_are_ascii_for_legacy_windows_pip():
    (MAPPING.parent / "requirements.txt").read_bytes().decode("ascii")


def test_maintenance_restart_records_success_and_the_freed_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "STATE_PATH", tmp_path / "batch_state.json")
    memories = iter([1400.0, 480.0])
    monkeypatch.setattr(B.fses, "solidworks_memory_mb", lambda: next(memories, 480.0))
    monkeypatch.setattr(B.fses, "restart_solidworks",
                        lambda **k: {"ok": True, "attempts": [{"attempt": 1}]})
    state = {"schema_version": 1, "samples": {}}

    ok, why = B.maintenance_restart(state, every=50, ran=50)

    assert ok is True and why == "ok"
    entry = state["samples"]["__maintenance__"]
    assert entry["status"] == "done"
    assert entry["kind"] == "solidworks_restart"
    assert entry["memory_before_mb"] == 1400.0
    assert entry["memory_after_mb"] == 480.0
    assert entry["why"] is None


def test_maintenance_restart_failure_is_recorded_not_swallowed(tmp_path, monkeypatch):
    """重启失败**必须**返回 False 让批处理停下 —— 绝不安静降级成继续用旧进程。"""
    monkeypatch.setattr(B, "STATE_PATH", tmp_path / "batch_state.json")
    monkeypatch.setattr(B.fses, "solidworks_memory_mb", lambda: 1400.0)
    monkeypatch.setattr(B.fses, "restart_solidworks",
                        lambda **k: {"ok": False, "why": "重试 3 次仍未成功"})
    state = {"schema_version": 1, "samples": {}}

    ok, why = B.maintenance_restart(state, every=50, ran=50)

    assert ok is False
    assert "重试 3 次仍未成功" in why
    assert state["samples"]["__maintenance__"]["status"] == "failed"


def test_maintenance_restart_survives_an_exception_from_the_session_layer(tmp_path, monkeypatch):
    """重启层抛异常也不能把批处理带崩 —— 要转成「停批处理」这个可控结果。"""
    monkeypatch.setattr(B, "STATE_PATH", tmp_path / "batch_state.json")
    monkeypatch.setattr(B.fses, "solidworks_memory_mb", lambda: None)

    def boom(**_k):
        raise RuntimeError("COM 炸了")

    monkeypatch.setattr(B.fses, "restart_solidworks", boom)

    ok, why = B.maintenance_restart({"schema_version": 1, "samples": {}}, every=50, ran=50)

    assert ok is False
    assert "COM 炸了" in why


def test_the_restart_hook_sits_at_the_loop_tail_after_both_branches():
    """重启只能发生在样本边界：成功/失败两路都结算完之后。

    插错位置的后果很重 —— 一个样本要起 3 次子进程，中途换 SolidWorks 进程会让
    `--resume` 的"内存未保存"陷阱变成常态，每个样本白花约 20 秒。
    """
    source = (MAPPING / "scripts" / "Run-Batch.py").read_text(encoding="utf-8")
    loop = source.split("for n, design in enumerate(pending", 1)[1]
    loop = loop.split("print(f\"\\n{'=' * 60}\")", 1)[0]
    tail = loop.rsplit("\n        else:\n", 1)[1]          # 失败分支起
    assert "maintenance_restart(" in tail, "重启钩子必须在 if/else 之后"
    assert "run_one(" not in tail.split("maintenance_restart(")[0], \
        "重启钩子不能插在样本内部（run_one 之前）"


def test_same_named_document_error_is_translated_to_something_actionable():
    """`RefuseSameNamedOpenDocuments` 要翻成人话。

    换机器实测：SolidWorks 里留着上一个样本的文档时，`Run-OneDesign.exe` 抛
    `InvalidOperationException: RefuseSameNamedOpenDocuments` 加一长串调用栈 ——
    信息全在，但看不出「该干什么」。而它**有明确的处置办法**：关掉所有文档。

    这也是适配器**故意**的行为：它拒绝替用户关闭同名文档（可能是人家自己开的、
    有未保存改动的文件），宁可不干。
    """
    raw = ("[stderr] 无法打开 C:\\...\\sample_51\\8\"D94R3Y-CL600C-03阀体.SLDPRT\n"
           "   System.InvalidOperationException: RefuseSameNamedOpenDocuments(...)\n"
           "   RunOneDesign.Main(String[] args)")
    msg = B._friendly_cad_error(raw)

    assert "关闭全部文档" in msg, "要告诉人怎么办"
    assert "空白主界面" in msg
    assert "RefuseSameNamedOpenDocuments" not in msg or "适配器" in msg
    assert "sample_51" in msg, "要指出是哪个文件被占着，方便定位"


def test_unrelated_cad_errors_pass_through_unchanged():
    """别的 CAD 报错原样透传，别乱翻译。"""
    raw = "Topology gate failed: {\"component\":\"11蝶板\"...}"
    assert B._friendly_cad_error(raw) == raw[:1200]


def test_no_restart_after_the_final_sample():
    """最后一个样本之后不重启 —— 重启有 ~400 MB 的一次性成本（实测），后面没样本就纯属白花。"""
    source = (MAPPING / "scripts" / "Run-Batch.py").read_text(encoding="utf-8")
    loop = source.split("for n, design in enumerate(pending", 1)[1]
    loop = loop.split("print(f\"\\n{'=' * 60}\")", 1)[0]
    guard = loop.split("maintenance_restart(")[0]
    assert "n < len(pending)" in guard, "缺少「还有没有下一个样本」的守卫"


# --------------------------------------------- 换数据集（--fresh / 序号口径）


def test_state_roster_ignores_the_maintenance_record():
    """`__maintenance__` 是维护记录不是样本，不能混进序号集合。"""
    state = {"samples": {"1": {}, "2": {}, "__maintenance__": {}}}
    assert B.state_roster(state) == {"1", "2"}


def test_archive_state_moves_not_deletes(tmp_path, monkeypatch):
    """换数据集时旧进度要**归档**而不是删 —— 万一还想回头看旧那批跑过什么。"""
    state_path = tmp_path / "batch_state.json"
    state_path.write_text('{"samples": {"1": {"status": "done"}}}', encoding="utf-8")
    monkeypatch.setattr(B, "STATE_PATH", state_path)

    dest = B.archive_state()

    assert dest is not None and dest.is_file()
    assert dest.name.startswith("batch_state-") and dest.suffix == ".json"
    assert not state_path.exists(), "原文件应被移走（这样才会从零开始）"
    assert '"done"' in dest.read_text(encoding="utf-8"), "内容不能丢"


def test_archive_state_is_a_noop_without_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "STATE_PATH", tmp_path / "batch_state.json")
    assert B.archive_state() is None


def test_fresh_flag_exists_and_defaults_off():
    assert B.build_parser().parse_args([]).fresh is False
    assert B.build_parser().parse_args(["--fresh"]).fresh is True


def test_stale_roster_is_reported_loudly():
    """**这条是防止静默丢数据的。**

    跳过判据只看序号、不比对设计变量。所以换了一份变量表、序号又从头开始的话，
    相同序号的样本会被当成「已跑过」而跳过 —— 一声不响，少跑几十个设计点。
    预检必须把这件事喊出来。
    """
    source = (MAPPING / "scripts" / "Run-Batch.py").read_text(encoding="utf-8")
    assert "state_roster(state) - {" in source, "缺少「状态里的序号不在当前表里」的比对"
    assert "--fresh" in source
    # 提示语要能让人照做
    assert "换数据集请加 --fresh" in source
