#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""换机器后的体检：把整条链路要用的东西一次全查一遍。

默认的离线检查只读取文件；加 `--require-sw` 时会加载会话模块探测 COM。

用法::

    python scripts\\Doctor.py

逐项打 ✓ / ✗，最后给一句结论。出问题把整份输出发回来即可。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

# 统一 UTF-8 输出；Windows 的旧默认编码不能让一个勾号使体检崩溃。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

parser = argparse.ArgumentParser(description="检查迁移所需文件与 Python 环境")
parser.add_argument("--require-sw", action="store_true",
                    help="批处理前使用：还要求当前桌面有且仅有一个可响应的 SolidWorks")
args = parser.parse_args()

SCRIPTS = Path(__file__).resolve().parent
MAPPING = SCRIPTS.parent
ROOT = MAPPING.parent

ok_count = 0
bad: list[str] = []


def line(mark: str, text: str) -> None:
    global ok_count
    if mark == "✓":
        ok_count += 1
    print(f"  {mark} {text}")


def check_file(rel: str, why: str, *, must: bool = True) -> None:
    p = MAPPING / rel
    if p.is_file():
        line("✓", f"{rel}  ({p.stat().st_size:,} B)")
    else:
        line("✗", f"**缺** {rel}   ← {why}")
        if must:
            bad.append(rel)


def check_dir(rel: str, why: str) -> None:
    p = MAPPING / rel
    if p.is_dir():
        n = sum(1 for _ in p.rglob("*") if _.is_file())
        line("✓", f"{rel}/  ({n} 个文件)")
    else:
        line("✗", f"**缺** {rel}/   ← {why}")
        bad.append(rel + "/")


print("=" * 70)
print("换机器体检 —— 逐项检查整条链路的依赖")
print("=" * 70)
print(f"\n项目根 : {ROOT}")
print(f"SolidWorks-Batch-Parametric-Design-and-CFD-Simulation: {MAPPING}")
print(f"本脚本 : {SCRIPTS}")

# ---------------------------------------------------------------- Python
print("\n【Python 环境】")
v = sys.version_info
if v >= (3, 9):
    line("✓", f"版本 {sys.version.split()[0]}  （需要 3.9+）")
else:
    line("✗", f"版本 {sys.version.split()[0]} —— **太低，需要 3.9+**")
    bad.append("python<3.9")
line("·", f"解释器 {sys.executable}")
if sys.maxsize > 2**32:
    line("✓", "64 位 Python")
else:
    line("✗", "需要 64 位 Python，当前解释器是 32 位")
    bad.append("python-32bit")

for mod, why in (("win32com", "SolidWorks/Flow 的 COM 接口"),
                 ("pythoncom", "COM 初始化"),
                 ("openpyxl", "读写 Excel")):
    if importlib.util.find_spec(mod):
        line("✓", f"依赖 {mod}")
    else:
        line("✗", f"**缺依赖** {mod}   ← {why}；装：python -m pip install -r ..\\requirements.txt")
        bad.append(mod)
def release_at_least(actual: str, minimum: tuple[int, ...]) -> bool:
    """比较数字发布版本；不要求恰好等于开发机安装的版本。"""
    match = re.match(r"^(\d+(?:\.\d+)*)", actual)
    if not match:
        return False
    release = tuple(int(part) for part in match.group(1).split("."))
    width = max(len(release), len(minimum))
    return release + (0,) * (width - len(release)) >= minimum + (0,) * (width - len(minimum))


for dist, minimum in (("pywin32", (306,)), ("openpyxl", (3, 1, 4))):
    try:
        actual = metadata.version(dist)
        compatible = release_at_least(actual, minimum)
        expected = ">=" + ".".join(map(str, minimum))
        if compatible:
            line("✓", f"{dist} 版本 {actual}（满足 requirements.txt）")
        else:
            line("✗", f"{dist} 版本 {actual}，最低要求 {expected}；"
                  "在选定的 Python 环境运行 python -m pip install -r ..\\requirements.txt")
            bad.append(f"{dist}-version")
    except metadata.PackageNotFoundError:
        pass

# ---------------------------------------------------------------- 项目内的文件
print("\n【项目内的文件】（都在这个文件夹里，拷漏了就缺）")
check_file("../requirements.txt", "迁移时统一安装的 Python 依赖版本")
check_file("scripts/machine_paths.py", "路径解析，所有模块都 import 它")
check_file("scripts/flow_session.py", "SolidWorks 会话与门禁")
check_file("scripts/flow_project.py", "Flow 工程接口")
check_file("scripts/flow_geometry.py", "训练表契约")
check_file("scripts/sw_api.py", "pywin32 兼容层")
check_file("scripts/Run-FlowSample.py", "单样本全链路")
check_file("scripts/Run-Batch.py", "批量入口")
check_file("scripts/Probe-Cad.py", "只跑 CAD 的迁移探针")
check_file("scripts/Run-OneDesign.exe", "**CAD 参数化**（参数化就靠它）")
check_file("scripts/SolidWorks.Interop.sldworks.dll", "EXE 运行/重编译都要它")
check_file("scripts/SevenVariableAdapter.cs", "参数化源码（只在重编译时需要）")
check_file("../flow_transfer.py", "Flow API 连接依赖（在项目根目录）")
check_file("assets/internal_water.fwp", "Flow 工程模板")
check_file("config/cad_template_manifest_v6.json", "母版哈希与拓扑门禁")
check_file("config/flow_physics_reference.json", "Flow 边界/目标基准")
check_dir("working/assembly_batch_v6", "**CAD 母版**，参数化的模板")

# 母版里的文件数
tpl = MAPPING / "working" / "assembly_batch_v6"
if tpl.is_dir():
    sld = sorted(p.name for p in tpl.glob("*.SLD*"))
    line("·", f"母版里有 {len(sld)} 个 SLDASM/SLDPRT")

# 迁移时不能只数文件：母版与清单若不是同一批，后续参数化/拓扑门禁会失败。
manifest_path = MAPPING / "config" / "cad_template_manifest_v6.json"
if tpl.is_dir() and manifest_path.is_file():
    try:
        expected_files = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
        mismatches = []
        for name, want in expected_files.items():
            part = tpl / name
            if not part.is_file():
                mismatches.append(f"缺 {name}")
                continue
            digest = hashlib.sha256()
            with part.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            if digest.hexdigest().lower() != str(want).lower():
                mismatches.append(f"哈希不符 {name}")
        if mismatches:
            line("✗", "母版与 cad_template_manifest_v6.json 不同批：" + "；".join(mismatches))
            bad.append("template-manifest-mismatch")
        else:
            line("✓", f"母版 {len(expected_files)} 个 CAD 文件与清单逐字节一致")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        line("✗", f"母版清单无法校验：{type(exc).__name__}: {exc}")
        bad.append("template-manifest-invalid")

# ---------------------------------------------------------------- 外部依赖
print("\n【项目外的依赖】（要那台机器自己装）")
try:
    sys.path.insert(0, str(SCRIPTS))
    import machine_paths as mp          # noqa: PLC0415
    for k in ("solidworks_exe", "bincfw", "flow_template_fwp"):
        val = mp.resolve()[k]
        if val and Path(val).exists():
            line("✓", f"{k}: {val}")
        else:
            line("✗", f"**找不到** {k}   ← {mp.resolve()['sources'].get(k, '探测失败')}")
            bad.append(k)
    for p in mp.resolve()["problems"]:
        line("·", f"提示：{p}")
except Exception as exc:                # noqa: BLE001
    line("✗", f"machine_paths 跑不起来：{type(exc).__name__}: {exc}")
    bad.append("machine_paths")

# ---------------------------------------------------------------- EXE 与配置是否同版
print("\n【EXE 与设计 JSON 是否同批】（**只拷了一部分文件时最容易错的地方**）")
exe = MAPPING / "scripts" / "Run-OneDesign.exe"
cfg_dir = MAPPING / "config"
if exe.is_file():
    blob = exe.read_bytes()
    # .NET 字符串是 UTF-16 存的，ASCII 查不到
    exe_seven = "Exactly seven named input fields are required.".encode("utf-16-le") in blob
    exe_eight = "Exactly eight named input fields are required.".encode("utf-16-le") in blob
    exe_wants = 7 if exe_seven else (8 if exe_eight else None)
    if exe_wants is None:
        line("✗", "EXE 里读不出字段数契约 —— 可能不是我们编译的那个")
        bad.append("exe-unknown")
    else:
        line("✓", f"Run-OneDesign.exe 要求 **{exe_wants} 个字段**")

    cfgs = sorted(cfg_dir.glob("design_*.json"))
    if not cfgs:
        line("✗", "config/ 下没有 design_*.json")
        bad.append("no-design-json")
    else:
        import json as _json
        counts = {}
        for c in cfgs:
            try:
                counts[len(_json.loads(c.read_text(encoding="utf-8")))] = \
                    counts.get(len(_json.loads(c.read_text(encoding="utf-8"))), 0) + 1
            except Exception:           # noqa: BLE001
                counts["读不了"] = counts.get("读不了", 0) + 1
        line("·", f"config/design_*.json 的字段数分布：{counts}")
        cfg_wants = 7 if counts.get(7) else (8 if counts.get(8) else None)
        if exe_wants is not None and cfg_wants is not None and exe_wants != cfg_wants:
            line("✗", f"**对不上**：EXE 要 {exe_wants} 个字段，配置文件是 {cfg_wants} 个 —— "
                      "**参数化会当场被拒**。这两个必须同批更新（拷全文件夹即可）")
            bad.append("exe-config-mismatch")
        elif exe_wants == cfg_wants:
            line("✓", f"两边一致（都是 {exe_wants} 个字段）")

# 回退是否彻底：旧的 8 字段残留
stale = [p.name for p in (MAPPING / "scripts").glob("*.py")
         if p.name != "Doctor.py" and
         "opening_deg" in p.read_text(encoding="utf-8", errors="ignore")]
if stale:
    line("·", f"提示：这些脚本里还提到 opening_deg（正常的话只有 Run-FlowSingle/探针）：{stale}")

# ---------------------------------------------------------------- SolidWorks 进程
print("\n【SolidWorks 进程】")
try:
    proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq SLDWORKS.exe", "/NH"],
                          capture_output=True, text=True, timeout=30, errors="replace")
    if proc.returncode:
        fallback = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "@(Get-Process -Name 'SLDWORKS' -ErrorAction SilentlyContinue | "
             "Select-Object Id) | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=30, errors="replace")
        if fallback.returncode:
            raise RuntimeError(f"tasklist 返回 {proc.returncode}："
                               f"{(proc.stderr or proc.stdout).strip()}；"
                               f"Get-Process 也失败：{(fallback.stderr or fallback.stdout).strip()}")
        rows = json.loads(fallback.stdout) if fallback.stdout.strip() else []
        rows = [rows] if isinstance(rows, dict) else rows
        pids = [str(row["Id"]) for row in rows]
        line("·", "tasklist 被拒绝；已用只读 Get-Process 回退枚举")
    else:
        pids = [l.split()[1] for l in proc.stdout.splitlines()
                if l.lower().startswith("sldworks.exe")]
    if not pids:
        line("✗" if args.require_sw else "·",
             "没有运行中的 SolidWorks；环境安装检查可继续，执行 CAD 前须手工打开空白主界面")
        if args.require_sw:
            bad.append("solidworks-not-running")
    elif len(pids) > 1:
        line("✗", f"有 {len(pids)} 个实例 {pids} ← 只能留一个")
        bad.append("solidworks-multiple")
    else:
        line("✓", f"恰好 1 个实例，PID {pids[0]}")
        if args.require_sw:
            try:
                import flow_session
                alive, why = flow_session.probe_alive(timeout_s=8.0, require_blank=True)
                if alive:
                    line("✓", "SolidWorks COM 可响应、版本为 2026，且没有打开的文档")
                else:
                    line("✗", f"SolidWorks COM 不可响应：{why}；检查弹窗、桌面会话和权限")
                    bad.append("solidworks-com-unavailable")
            except Exception as exc:  # noqa: BLE001
                line("✗", f"SolidWorks COM 探活失败：{type(exc).__name__}: {exc}")
                bad.append("solidworks-com-probe")
except Exception as exc:                # noqa: BLE001
    line("✗" if args.require_sw else "·",
         f"tasklist 调用失败：{exc}；静态环境检查可继续，执行前必须解决")
    if args.require_sw:
        bad.append("tasklist")

# ---------------------------------------------------------------- 结论
print("\n" + "=" * 70)
if not bad:
    print(f"[OK] 全部通过（{ok_count} 项）。")
    if not args.require_sw:
        print("   要执行 CAD 时先打开 SolidWorks 空白页，再运行 python scripts\\Doctor.py --require-sw")
    else:
        print("   可以先执行一例 CAD 参数化，再运行批处理。")
else:
    print(f"[FAIL] 有 {len(bad)} 项没过：")
    for b in bad:
        print(f"     · {b}")
    print("\n把整份输出发回来即可定位。")
print("=" * 70)
sys.exit(0 if not bad else 1)
