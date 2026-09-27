#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可行性探针：SolidWorks「退出 → 重新拉起 → 重新附加 → 过门禁」能不能跑通。

**这不是生产代码，是一次性验证脚本。** 批处理的 `--restart-every` 要靠它先证明可行。

背景：SolidWorks API 里**没有 Restart 方法**（interop 程序集里 `Restart` 零命中），
只有 `ExitApp`。所以"重启"得自己拼：退出 → 用 Flow 官方 `RunProduct2` 重新拉起 →
轮询等它就绪 → 重新附加。

铁律的准确边界：禁的是**用 COM 创建实例**（`Dispatch`/`Activator.CreateInstance`
→ `CO_E_SERVER_EXEC_FAILURE 0x80080005` + `0x00000003` 崩溃）。
`RunProduct2` 是官方启动路径，`launch_sw.py` 已在用。

用法::

    python scripts/Probe-SwRestart.py            # 先只看当前状态
    python scripts/Probe-SwRestart.py --run      # 真的走一遍重启闭环

`--run` 会**关掉当前正在跑的 SolidWorks**。别在有未保存文档时跑它。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

MAPPING_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MAPPING_ROOT.parent
for _p in (str(REPO_ROOT), str(MAPPING_ROOT / "scripts"), str(MAPPING_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import flow_session as fses  # noqa: E402
import machine_paths as mpaths  # noqa: E402

#: 安装位置由 `machine_paths` 探测（注册表）或读覆盖文件 —— 不写死，这样项目能整体搬机器。
SOLIDWORKS_EXE = mpaths.resolve()["solidworks_exe"]
BINCFW = mpaths.resolve()["bincfw"]

EXIT_TIMEOUT_S = 60.0     # 等 ExitApp 生效
LAUNCH_TIMEOUT_S = 180.0  # 等新实例就绪（照 launch_sw.py 的 90×2 秒）
POLL_S = 2.0


def snapshot() -> dict:
    """当前 SolidWorks 状态。不启动、不关闭任何东西。"""
    pids = fses.list_solidworks_pids()
    alive, why = fses.probe_alive(timeout_s=8.0)
    return {
        "pids": pids,
        "count": len(pids),
        "probe_alive": alive,
        "probe_why": why,
        "solver_running": fses.solver_running(),
        "solidworks_exe": str(SOLIDWORKS_EXE) if SOLIDWORKS_EXE else None,
        "bincfw": str(BINCFW) if BINCFW else None,
        "exe_exists": bool(SOLIDWORKS_EXE and SOLIDWORKS_EXE.is_file()),
        "bincfw_exists": bool(BINCFW and BINCFW.is_dir()),
        "revision": solidworks_revision(),
    }


def solidworks_revision() -> str | None:
    """在跑的 SolidWorks 的 RevisionNumber，例如 `34.3.2`。

    **换机器时用它对 SP。** 编码是 `<大版本>.<SP>.<hotfix>`：
    `34.0.0` = SW2026 SP0，`34.3.2` = SW2026 SP3.2。

    为什么重要：拓扑门禁比的是**每个零件有几个面**，那些数是在 **SP3.2** 上抓的。
    换 SP 时 SolidWorks 偶尔会改变面的切分方式 → 面数变了 → 门禁判"拓扑不一致"。
    门禁只查 `34.`（大版本），**SP 差异它不拦**，所以得靠人比。
    """
    if not fses.list_solidworks_pids():
        return None
    try:
        import pythoncom
        import win32com.client as win32
        pythoncom.CoInitialize()
        try:
            sw = win32.GetActiveObject("SldWorks.Application")
            # 走兼容层：SolidWorks 的 COM 成员在不同 pywin32 绑定下可能是属性而非方法，
            # 直接 `sw.RevisionNumber()` 会 'str' object is not callable（换机器实测踩过）
            return str(fses._member(sw, "RevisionNumber", default=""))
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        return None


def wait_for_exit(timeout_s: float) -> dict:
    """轮询等 SLDWORKS.exe 全部消失。"""
    began = time.time()
    while time.time() - began < timeout_s:
        pids = fses.list_solidworks_pids()
        if not pids:
            return {"gone": True, "seconds": round(time.time() - began, 1)}
        time.sleep(POLL_S)
    return {"gone": False, "seconds": round(time.time() - began, 1),
            "remaining": fses.list_solidworks_pids()}


def stop_solidworks(before: dict) -> dict:
    """干净退出；不成再 taskkill 兜底。返回审计结构。"""
    result: dict = {"method": None, "ok": False}
    before_pids = before["pids"]
    if before["solver_running"]:
        result["why"] = "EFDsolver.exe 在跑 —— 拒绝重启，避免留下孤儿求解器"
        return result

    # ---- ① 先试 ExitApp（干净退出，不弹文档恢复）
    try:
        import pythoncom
        import win32com.client as win32
        pythoncom.CoInitialize()
        sw = win32.GetActiveObject("SldWorks.Application")
        try:
            # 顺序照 cleanup_failed_flow_projects.py:117-132 的既有先例
            try:
                sw.ExitApp()
            except Exception as exc:  # noqa: BLE001
                result["exitapp_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass
        result["method"] = "ExitApp"
        result["exit_wait"] = wait_for_exit(EXIT_TIMEOUT_S)
        if result["exit_wait"]["gone"]:
            result["ok"] = True
            return result
    except Exception as exc:  # noqa: BLE001
        result["exitapp_error"] = f"{type(exc).__name__}: {exc}"

    # ---- ② ExitApp 没成 → taskkill 兜底。杀前复核 PID（照 flow_session.py:110-113）
    result["method"] = "taskkill"
    current = {p.pid for p in fses.list_solidworks_processes()}
    stale = [p for p in before_pids if p not in current]
    if stale:
        result["why"] = f"ExitApp 后 PID 已变或消失 {stale}，不猜、不杀"
        return result
    import subprocess
    for pid in before_pids:
        proc = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                              capture_output=True, text=True, timeout=60, errors="replace")
        result.setdefault("taskkill", []).append(
            {"pid": pid, "rc": proc.returncode,
             "out": (proc.stdout or proc.stderr or "").strip()[:200]})
    result["exit_wait"] = wait_for_exit(EXIT_TIMEOUT_S)
    result["ok"] = result["exit_wait"]["gone"]
    return result


def launch_solidworks() -> dict:
    """用 Flow 官方 RunProduct2 拉起，再轮询等就绪。

    ⚠️ **必须先 `CoInitialize()`。** `flow_transfer.connect()` 里的
    `win32.Dispatch(PROGID)` 要求调用线程已初始化 COM，否则直接
    `com_error(-2147221008, '尚未调用 CoInitialize')`。
    `launch_sw.py:24` 在主线程开头做了这件事，本函数照做。
    """
    result: dict = {"ok": False}
    if not (SOLIDWORKS_EXE and SOLIDWORKS_EXE.is_file()):
        result["why"] = f"找不到 SolidWorks：{SOLIDWORKS_EXE}\n{mpaths.describe()}"
        return result
    if not (BINCFW and BINCFW.is_dir()):
        result["why"] = f"找不到 binCFW：{BINCFW}\n{mpaths.describe()}"
        return result

    import pythoncom
    pythoncom.CoInitialize()
    try:
        return _launch_locked(result)
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


def _launch_locked(result: dict) -> dict:
    import flow_transfer as ft
    try:
        api = ft.connect()
    except SystemExit as exc:  # flow_transfer.connect 失败时 sys.exit
        result["why"] = f"flow_transfer.connect 退出码 {exc.code}"
        return result

    began = time.time()
    try:
        running = api.RunProduct2(str(SOLIDWORKS_EXE), str(BINCFW))
    except Exception as exc:  # noqa: BLE001
        result["why"] = f"RunProduct2 抛异常：{type(exc).__name__}: {exc}"
        return result
    result["runproduct2_returned"] = running is not None

    # 轮询就绪：用现成的 probe_alive（子线程 CoInitialize + 每次重新 GetActiveObject）
    alive, why = False, "未开始"
    while time.time() - began < LAUNCH_TIMEOUT_S:
        alive, why = fses.probe_alive(timeout_s=8.0)
        if alive:
            break
        time.sleep(POLL_S)

    result["probe_alive"] = alive
    result["probe_why"] = why
    result["seconds"] = round(time.time() - began, 1)
    result["ok"] = alive
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SolidWorks 重启可行性探针")
    ap.add_argument("--run", action="store_true", help="真的走一遍重启闭环")
    args = ap.parse_args(argv)

    print("=" * 68)
    print("SolidWorks 重启可行性探针")
    print("=" * 68)

    before = snapshot()
    print("\n【重启前】")
    print(json.dumps(before, ensure_ascii=False, indent=2))

    if not args.run:
        rev = before.get("revision")
        print()
        if rev:
            print(f"SolidWorks 版本 : {rev}")
            print(f"基线（母版定版）: 34.3.2   ← SP3.2")
            if not rev.startswith("34.3"):
                print("  ⚠️ SP 与基线不同。拓扑门禁比的是每个零件的【面数】，"
                      "那些数是在 SP3.2 上抓的；")
                print("     换 SP 时 SolidWorks 偶尔会改变面的切分方式，"
                      "面数一变门禁就判「拓扑不一致」。")
                print("     门禁只查大版本 34.（即 2026），SP 差异它不拦 —— "
                      "请先跑一个已知样本确认能过，再放全量。")
            else:
                print("  ✓ SP 与基线一致")
        else:
            print("（SolidWorks 没在跑，读不到版本号）")
        print("\n（只读。确认无误后加 --run 走完整闭环）")
        return 0

    report: dict = {"before": before, "argv": sys.argv[1:]}

    if before["count"] == 0:
        print("\n【当前没有 SolidWorks 在跑 —— 先拉起一次，才能测「退出」这一半】")
        first = launch_solidworks()
        report["initial_launch"] = first
        print(json.dumps(first, ensure_ascii=False, indent=2))
        if not first["ok"]:
            report["verdict"] = "FAIL：连初次拉起都做不到，重启无从谈起"
            print("\n" + json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        before = snapshot()
        report["before"] = before
        print("\n【拉起后】PID =", before["pids"])

    if before["count"] != 1:
        report["verdict"] = f"FAIL：有 {before['count']} 个实例，不猜该动哪一个"
        print("\n" + json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    old_pids = list(before["pids"])

    # ---- 退出
    print(f"\n【① 退出】当前 PID {old_pids}")
    stop = stop_solidworks(before)
    report["stop"] = stop
    print(json.dumps(stop, ensure_ascii=False, indent=2))
    if not stop["ok"]:
        report["verdict"] = "FAIL：关不掉 SolidWorks"
        print("\n" + json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    # ---- 重新拉起
    print("\n【② 重新拉起】")
    launch = launch_solidworks()
    report["launch"] = launch
    print(json.dumps(launch, ensure_ascii=False, indent=2))
    if not launch["ok"]:
        report["verdict"] = "FAIL：拉不起来 / 拉起来但 COM 连不上"
        print("\n" + json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    # ---- 重新附加 + 过门禁
    print("\n【③ 重新附加 + 门禁】")
    after = snapshot()
    report["after"] = after
    print(json.dumps(after, ensure_ascii=False, indent=2))

    checks = {
        "进程数恰好 1": after["count"] == 1,
        "PID 与重启前不同": bool(after["pids"]) and after["pids"] != old_pids,
        "probe_alive": after["probe_alive"],
    }
    try:
        session = fses.SwSession.attach(retries=10, delay=2.0)
        gate = session.gate_or_raise()
        report["gate"] = {"ok": True, "report": gate}
        checks["gate_or_raise 通过"] = True
        session.unload()
    except Exception as exc:  # noqa: BLE001
        report["gate"] = {"ok": False, "why": f"{type(exc).__name__}: {exc}"}
        checks["gate_or_raise 通过"] = False

    print("\n【④ 判据】")
    for name, ok in checks.items():
        print(f"   {'✓' if ok else '✗'} {name}")
    report["checks"] = checks
    passed = all(checks.values())
    report["verdict"] = "PASS" if passed else "FAIL"
    print(f"\n==> 结论：{report['verdict']}")

    out = MAPPING_ROOT / "_analysis" / "sw_restart_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完整报告 -> {out}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
