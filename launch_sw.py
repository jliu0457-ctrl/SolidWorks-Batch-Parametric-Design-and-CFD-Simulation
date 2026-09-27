#!/usr/bin/env python3
"""用 Flow 官方 RunProduct2 启动 SolidWorks + Flow，然后确认门禁通过。

走的是官方启动路径（不是会崩的 Activator.CreateInstance 冷启动）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pythoncom
import win32com.client as win32

ROOT = Path(__file__).resolve().parent              # 项目根由自身位置推出，不写死
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "SolidWorks-Batch-Parametric-Design-and-CFD-Simulation" / "scripts"))

import flow_transfer as ft      # noqa: E402
import machine_paths as mpaths  # noqa: E402

#: 安装位置由 machine_paths 探测（注册表）或读覆盖文件 —— 不写死，这样项目能整体搬机器。
_SW = mpaths.resolve()
SOLIDWORKS_EXE = _SW["solidworks_exe"]
BINCFW = _SW["bincfw"]


def main():
    pythoncom.CoInitialize()
    if not (SOLIDWORKS_EXE and SOLIDWORKS_EXE.is_file()):
        raise SystemExit("找不到 SolidWorks。\n%s" % mpaths.describe())
    if not (BINCFW and BINCFW.is_dir()):
        raise SystemExit("找不到 binCFW。\n%s" % mpaths.describe())

    api = ft.connect()

    # 已经在跑就直接用
    running = ft.safe_get(api, "Attach2RunningObject")
    if running is not None:
        print("already running, attaching")
    else:
        print("launching via RunProduct2 ...")
        running = api.RunProduct2(str(SOLIDWORKS_EXE), str(BINCFW))
        if running is None:
            raise SystemExit("RunProduct2 returned no InteractiveApplication")
        print("launched")

    # 等 COM 可见
    sw = None
    for attempt in range(90):
        try:
            sw = win32.GetActiveObject("SldWorks.Application")
        except Exception:
            sw = None
        if sw is not None:
            break
        time.sleep(2)
    if sw is None:
        raise SystemExit("SolidWorks 起来了但 COM 连不上")

    print("RevisionNumber :", sw.RevisionNumber())
    print("Visible        :", sw.Visible)
    doc = sw.ActiveDoc
    title = None
    if doc is not None:
        t = doc.GetTitle
        title = t if isinstance(t, str) else t()
    print("ActiveDoc      :", title)
    print("GATE:", "PASS" if (str(sw.RevisionNumber()).startswith("34.") and sw.Visible and doc is None) else "CHECK")


if __name__ == "__main__":
    main()
