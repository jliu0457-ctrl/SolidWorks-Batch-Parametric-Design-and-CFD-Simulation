#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机相关路径的解析 —— 让整个项目能搬到别的电脑上跑。

三条原则：

1. **项目内的东西一律由 `__file__` 推出**，绝不写死盘符、用户名或目录深度。
   搬走整个文件夹就该能用。
2. **项目外的东西（SolidWorks 安装）自动探测**，探测不到才让人用配置文件覆盖。
   SolidWorks 几个 GB 且要授权，**不可能拷进项目**，所以只能用这个办法。
3. **探测失败时给可操作的报错**，不是丢一句 `FileNotFoundError` 让人猜。

## SolidWorks 装在哪

Windows 上 COM 组件必须在注册表登记自己，SolidWorks 也不例外：

    HKCR\\SldWorks.Application\\CLSID           → {666aaee2-7a21-40fc-b768-2078840a88c3}
    HKCR\\CLSID\\{666aaee2-...}\\LocalServer32   → D:\\...\\SLDWORKS.exe

本机实测能从该注册信息取到 SolidWorks 路径；其他机器若注册项缺失、被重定向
或同时安装多个版本，就使用下方的机器专属覆盖文件，不假设注册表一定可靠。

`binCFW` 由 SolidWorks 目录**推导**，不单独探测：

    <SOLIDWORKS 的父目录>\\SOLIDWORKS Flow Simulation\\binCFW

## 覆盖文件（可选）

`config/machine_paths.json`，只在探测不到、或探测到的不是你想要的那个版本时用：

    {
      "solidworks_exe": "D:\\\\SW2026\\\\SOLIDWORKS\\\\SLDWORKS.exe",
      "bincfw": "D:\\\\SW2026\\\\SOLIDWORKS Flow Simulation\\\\binCFW",
      "flow_template_fwp": "SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/assets/internal_water.fwp"
    }

路径**有值就以它为准**，没写就自动探测。相对路径按项目根目录解析。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

#: 最低 Python 版本。判据是 `Path.is_relative_to`（`valve_mapping/runs.py` 等在用），3.9 才有。
MIN_PYTHON = (3, 9)


def require_python(minimum: tuple[int, int] = MIN_PYTHON) -> None:
    """版本太低就给句人话，别让它跑到一半报 `AttributeError: is_relative_to`。

    换机器时最容易踩的是**调错解释器**（装了好几个 Python，`python` 指向老的那个）。
    所以报错里把**当前解释器的完整路径**打出来，一眼看出用错的是哪个。
    """
    if sys.version_info < minimum:
        need = ".".join(str(x) for x in minimum)
        raise SystemExit(
            f"需要 Python {need} 或更高，当前是 {sys.version.split()[0]}\n"
            f"  正在用的解释器：{sys.executable}\n"
            f"  代码里用了 `Path.is_relative_to`（{need} 才有）。\n"
            "  请先激活项目专用环境，再确认 `python --version` 和 `sys.executable`。")

#: 项目根 = 本文件的上上级（`<项目>/SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/scripts/machine_paths.py`）。
#: 解析成绝对路径，之后怎么改工作目录都不受影响。
MAPPING_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = MAPPING_ROOT.parent

CONFIG_PATH = MAPPING_ROOT / "config" / "machine_paths.json"

#: Flow 工程模板。**这个是能拷进项目的**，所以直接放在 `assets/` 下。
DEFAULT_FLOW_TEMPLATE = MAPPING_ROOT / "assets" / "internal_water.fwp"

_PROGID = "SldWorks.Application"

#: 缓存，避免每次调用都读注册表。`resolve(force=True)` 可刷新。
_cache: dict[str, Any] | None = None


# ---------------------------------------------------------------- 注册表探测

def _read_config() -> dict:
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{CONFIG_PATH} 读不了或不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{CONFIG_PATH} 的顶层必须是一个对象")
    return data


def _config_path_value(raw: str, base: Path) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else (base / p)


def detect_solidworks_exe() -> Path | None:
    """从注册表问出 `SLDWORKS.exe` 的完整路径。拿不到返回 None。

    ⚠️ `winreg` 只在 Windows 有，**必须函数内 import** —— 本模块要能被
    离线测试 import，那些环境里没有 pywin32 / winreg。
    """
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{_PROGID}\CLSID") as key:
            clsid = str(winreg.QueryValueEx(key, "")[0]).strip()
    except OSError:
        return None
    if not clsid:
        return None

    # 两处都查：HKCR 是 HKLM\SOFTWARE\Classes 的合并视图，但个别机器上只有一边有。
    for root, base in ((winreg.HKEY_CLASSES_ROOT, "CLSID"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes\CLSID")):
        for suffix in ("LocalServer32", "InprocServer32"):
            try:
                with winreg.OpenKey(root, rf"{base}\{clsid}\{suffix}") as key:
                    value = str(winreg.QueryValueEx(key, "")[0]).strip()
            except OSError:
                continue
            # 命令行可能带引号和参数，例如 `"D:\...\SLDWORKS.exe" /Automation`
            cleaned = value.split('"')[1] if value.startswith('"') else value.split(" /")[0]
            path = Path(cleaned.strip())
            if path.suffix.lower() == ".exe" and path.is_file():
                return path
    return None


def derive_bincfw(solidworks_exe: Path) -> Path | None:
    """由 SolidWorks 目录推导 Flow 的 `binCFW`。推不出或不存在返回 None。"""
    candidate = solidworks_exe.parent.parent / "SOLIDWORKS Flow Simulation" / "binCFW"
    return candidate if candidate.is_dir() else None


# ---------------------------------------------------------------- 汇总解析

def resolve(force: bool = False) -> dict:
    """解析出本机需要的全部外部路径。结果带缓存。

    返回 ``{solidworks_exe, bincfw, flow_template_fwp, required_revision_prefix,
    sources, problems}``。**`problems` 非空不代表不能跑** —— 比如只跑 CAD 不跑
    Flow 就用不到 `binCFW`。调用方自己决定哪些是硬要求。
    """
    global _cache
    if _cache is not None and not force:
        return _cache

    config = _read_config()
    sources: dict[str, str] = {}
    problems: list[str] = []

    # ---- SolidWorks 可执行文件：配置优先，其次注册表
    sw: Path | None = None
    if config.get("solidworks_exe"):
        sw = _config_path_value(str(config["solidworks_exe"]), PROJECT_ROOT)
        sources["solidworks_exe"] = "config/machine_paths.json"
        if not sw.is_file():
            problems.append(f"配置里的 solidworks_exe 不存在：{sw}")
    else:
        sw = detect_solidworks_exe()
        if sw is not None:
            sources["solidworks_exe"] = "注册表 SldWorks.Application"
        else:
            problems.append(
                "注册表里没找到 SolidWorks（查了 HKCR\\SldWorks.Application\\CLSID → "
                "LocalServer32）。装了却探测不到的话，在 "
                f"{CONFIG_PATH.name} 里写死 solidworks_exe")

    # ---- binCFW：配置优先，其次由 SolidWorks 目录推导
    bincfw: Path | None = None
    if config.get("bincfw"):
        bincfw = _config_path_value(str(config["bincfw"]), PROJECT_ROOT)
        sources["bincfw"] = "config/machine_paths.json"
        if not bincfw.is_dir():
            problems.append(f"配置里的 bincfw 不存在：{bincfw}")
    elif sw is not None:
        bincfw = derive_bincfw(sw)
        if bincfw is not None:
            sources["bincfw"] = "由 SolidWorks 目录推导"
        else:
            problems.append(
                f"由 {sw.parent.parent} 推导不出 'SOLIDWORKS Flow Simulation\\binCFW'。"
                "Flow 装在别处的话，在配置里写死 bincfw")
    else:
        problems.append("没有 SolidWorks 路径，推不出 binCFW")

    # ---- Flow 工程模板：默认项目内，可覆盖
    if config.get("flow_template_fwp"):
        fwp = _config_path_value(str(config["flow_template_fwp"]), PROJECT_ROOT)
        sources["flow_template_fwp"] = "config/machine_paths.json"
    else:
        fwp = DEFAULT_FLOW_TEMPLATE
        sources["flow_template_fwp"] = "项目内 assets/"
    if not fwp.is_file():
        problems.append(f"Flow 工程模板不存在：{fwp}")

    _cache = {
        "solidworks_exe": sw,
        "bincfw": bincfw,
        "flow_template_fwp": fwp,
        "sources": sources,
        "problems": problems,
        "config_path": CONFIG_PATH,
        "project_root": PROJECT_ROOT,
    }
    return _cache


def solidworks_exe() -> Path:
    """要 `SLDWORKS.exe` 的调用方用这个 —— 拿不到就抛**带指引**的错。"""
    got = resolve()["solidworks_exe"]
    if got is None or not got.is_file():
        raise RuntimeError(
            "找不到 SolidWorks 的安装位置。\n"
            "  已试过：注册表 HKCR\\SldWorks.Application\\CLSID → LocalServer32\n"
            "  若 SolidWorks 确实装了却探测不到，手工写一份 "
            f"{CONFIG_PATH}：\n"
            '    {"solidworks_exe": "D:\\\\你的路径\\\\SOLIDWORKS\\\\SLDWORKS.exe"}')
    return got


def flow_bincfw() -> Path:
    """要 Flow `binCFW` 的调用方用这个。"""
    got = resolve()["bincfw"]
    if got is None or not got.is_dir():
        raise RuntimeError(
            "找不到 Flow Simulation 的 binCFW 目录。\n"
            "  已试过：由 SolidWorks 目录推导 "
            "<SW 父目录>\\SOLIDWORKS Flow Simulation\\binCFW\n"
            f"  若 Flow 装在别处，手工写进 {CONFIG_PATH}：\n"
            '    {"bincfw": "D:\\\\你的路径\\\\SOLIDWORKS Flow Simulation\\\\binCFW"}')
    return got


def flow_template_fwp() -> Path:
    got = resolve()["flow_template_fwp"]
    if got is None or not got.is_file():
        raise RuntimeError(f"Flow 工程模板不存在：{got}；检查项目 assets/ 或 {CONFIG_PATH}")
    return got


def describe() -> str:
    """给人看的一行行现状。排查"换机器跑不起来"时先看这个。"""
    data = resolve()
    lines = [f"项目根   : {data['project_root']}"]
    for key in ("solidworks_exe", "bincfw", "flow_template_fwp"):
        value = data[key]
        mark = "✓" if value is not None and Path(value).exists() else "✗"
        lines.append(f"{mark} {key:20s}: {value}   （来源：{data['sources'].get(key, '未找到')}）")
    if data["problems"]:
        lines.append("问题：")
        lines.extend(f"  ! {p}" for p in data["problems"])
    return "\n".join(lines)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    require_python()
    print(describe())
