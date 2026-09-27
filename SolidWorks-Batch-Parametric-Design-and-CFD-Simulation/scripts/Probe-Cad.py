#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""跨电脑 CAD-only 探针：不创建 Flow 项目，也不触碰批处理状态/训练表。

从「SolidWorks-Batch-Parametric-Design-and-CFD-Simulation」目录运行：
    python scripts\Probe-Cad.py
    python scripts\Probe-Cad.py --design config\design_baseline_v6.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

MAPPING = Path(__file__).resolve().parents[1]
SCRIPTS = MAPPING / "scripts"


def main() -> int:
    parser = argparse.ArgumentParser(description="只跑一次 CAD 参数化/拓扑门禁")
    parser.add_argument("--design", default="config/design_baseline_v6.json")
    parser.add_argument("--run", default=None, help="可选：指定唯一运行名；不指定则自动生成")
    parser.add_argument("--timeout", type=int, default=600, help="CAD 超时秒数，默认 600")
    parser.add_argument("--strict-preflight", action="store_true",
                        help="体检失败就停止；默认只提示，继续 CAD 探针")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout 必须大于零")
    run = args.run or "port_probe_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run):
        parser.error("--run 只能含英文字母、数字、下划线、连字符，最长 80 字符")
    design = Path(args.design)
    if not design.is_absolute():
        design = MAPPING / design
    design = design.resolve()
    if not design.is_file():
        parser.error(f"设计 JSON 不存在：{design}")
    try:
        obj = json.loads(design.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        parser.error(f"设计 JSON 无法读取：{exc}")
    if not isinstance(obj, dict) or len(obj) != 7:
        parser.error("当前 CAD EXE 要求 JSON 中恰好七个设计变量")
    run_dir = MAPPING / "working" / "seven_variable_trials" / run
    report_path = MAPPING / "_analysis" / f"e2e_{run}.json"
    if run_dir.exists() or report_path.exists():
        parser.error(f"运行名 {run} 已被使用；换一个 --run，避免覆盖旧实例/报告")

    # 同一解释器运行体检；默认仅提示，探针以 CAD 实际结果为准。
    try:
        check = subprocess.run([sys.executable, str(SCRIPTS / "Doctor.py"), "--require-sw"],
                               cwd=MAPPING, timeout=45)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"⚠️ 环境体检未能完成：{type(exc).__name__}: {exc}", flush=True)
        if args.strict_preflight:
            return 2
    else:
        if check.returncode:
            print("⚠️ 环境体检未通过；继续 CAD 探针，以实际参数化结果为准。", flush=True)
            if args.strict_preflight:
                return 2

    cmd = [str(SCRIPTS / "Run-OneDesign.exe"), ".", run, str(design)]
    print(f"开始 CAD-only：{run}；超时 {args.timeout}s", flush=True)
    try:
        # 直接继承当前终端，C# 的 template/design/异常可即时显示。
        proc = subprocess.Popen(cmd, cwd=MAPPING)
        code = proc.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        print(f"CAD 超过 {args.timeout}s；已停止探针进程。检查 SolidWorks 弹窗、"
              "COM 状态及安装版本。未触碰批处理状态。", flush=True)
        return 3
    if not report_path.is_file():
        print(f"CAD 退出码 {code}，但本次无报告 {report_path}；"
              "通常是连接/弹窗/EXE 与依赖异常。", flush=True)
        return 4
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"CAD 报告无法读取：{exc}", flush=True)
        return 4
    ok = (code == 0 and report.get("completed") is True and
          report.get("physical_mapping_verified") is True)
    print(f"CAD {'成功' if ok else '失败'}：退出码={code}，"
          f"completed={report.get('completed')}，"
          f"physical_mapping_verified={report.get('physical_mapping_verified')}")
    if not ok:
        print(f"错误：{report.get('error') or '参见报告'}")
    print(f"报告：{report_path}")
    print(f"副本：{run_dir}（探针不会自动删除，便于排查）")
    return 0 if ok else 4


if __name__ == "__main__":
    sys.exit(main())
