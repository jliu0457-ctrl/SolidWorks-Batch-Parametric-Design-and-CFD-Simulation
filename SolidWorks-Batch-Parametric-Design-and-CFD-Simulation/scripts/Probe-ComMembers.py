#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针：把 SolidWorks / Flow 的 COM 成员在**这台机器上**是「属性」还是「方法」列出来。

## 为什么要这个

SolidWorks 的 COM 成员在 pywin32 **晚期绑定**下，有的暴露成方法、有的暴露成属性 ——
而且是**看机器**的。同一个成员：

    sw.RevisionNumber        → 本机是方法，`sw.RevisionNumber()` 能用
                               另一台机器（Anaconda 的 pywin32）是属性，
                               加括号直接 `TypeError: 'str' object is not callable`

这个坑项目早有记录（`sw_api.py:21`），但只在**换机器**时才会暴露。

本脚本**只读**：附加到已运行的 SolidWorks，挨个试上面那些成员，报告每个成员
在哪一种形态下能读出值。**不写任何数据、不改任何配置、不关任何进程。**

用法::

    python scripts/Probe-ComMembers.py

需要 SolidWorks 已启动并停在空白主界面。结果同时写进
`_analysis/com_member_probe.json`，可以直接发给别人对比。

## 怎么用结果

* 全是「方法」→ 和本机一致，那台机器不该再出 `'str' object is not callable`
* 出现「属性」→ 把这份报告发回来，我们据此决定哪些调用点要走 `safe_get`
* 「都读不出来」→ 不是属性/方法的问题，是附加本身失败（看最上面的错误）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MAPPING_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(MAPPING_ROOT.parent), str(MAPPING_ROOT / "scripts"), str(MAPPING_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: 项目在生产路径里用到的**无参** SolidWorks 成员（有参数的必然是方法，不存在歧义）。
SW_MEMBERS = [
    "RevisionNumber",   # 门禁 + 探活（换机器就炸在这一个上）
    "Visible",          # 门禁：必须为 True
    "ActiveDoc",        # 门禁：必须为 None
    "GetDocuments",     # 列出已打开文档
    "GetFirstDocument", # 遍历文档
]


def _is_python_callable(member) -> bool:
    """只认真正的 Python 函数/方法 —— COM dispatch 对象的 callable 是假象。

    判据抄 `sw_api._is_python_callable`，不重新发明。
    """
    import types
    return isinstance(member, (types.FunctionType, types.MethodType,
                               types.BuiltinFunctionType, types.BuiltinMethodType))


def probe_member(obj, name: str) -> dict:
    """试一个成员：属性形态能读吗？方法形态能调吗？"""
    out: dict = {"name": name}
    try:
        member = getattr(obj, name)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["usable"] = False
        return out

    out["type"] = type(member).__name__
    out["looks_callable"] = callable(member)

    # 属性形态：直接读
    if not _is_python_callable(member):
        out["as_property"] = repr(member)[:120]
        out["usable"] = True
        out["form"] = "属性" if not callable(member) else "属性（但看着可调用）"
        return out

    # 方法形态：调一下
    try:
        value = member()
        out["as_method"] = repr(value)[:120]
        out["usable"] = True
        out["form"] = "方法"
    except Exception as exc:  # noqa: BLE001
        out["as_method_error"] = f"{type(exc).__name__}: {exc}"
        out["usable"] = False
        out["form"] = "调用失败"
    return out


def main() -> int:
    print("=" * 68)
    print("COM 成员形态探针（属性 vs 方法）")
    print("=" * 68)

    import pythoncom
    import win32com.client as win32

    pythoncom.CoInitialize()
    report: dict = {"members": [], "env": {}}

    import platform
    import win32api
    report["env"] = {
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "platform": platform.platform(),
    }
    try:
        import win32com
        report["env"]["win32com"] = win32com.__file__
    except Exception:  # noqa: BLE001
        pass
    try:
        import importlib.metadata as md
        report["env"]["pywin32_version"] = md.version("pywin32")
    except Exception:  # noqa: BLE001
        report["env"]["pywin32_version"] = "未知"

    print("\n【环境】这台机器")
    for k, v in report["env"].items():
        print(f"  {k:16s}: {v}")
    print("\n  ⚠️ 对照基线：Python 3.12.3 + pywin32 306（母版定版的那台机器）")

    try:
        sw = win32.GetActiveObject("SldWorks.Application")
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ 附加不上 SolidWorks：{type(exc).__name__}: {exc}")
        print("  先手工启动 SolidWorks 并停在空白主界面，再跑本脚本。")
        return 1

    print("\n【SolidWorks 成员】")
    print(f"  {'成员':<18} {'形态':<10} {'可读/可调':<8} 值")
    for name in SW_MEMBERS:
        row = probe_member(sw, name)
        report["members"].append(row)
        form = row.get("form", "—")
        ok = "✓" if row.get("usable") else "✗"
        value = row.get("as_property") or row.get("as_method") or row.get("error") \
            or row.get("as_method_error") or ""
        print(f"  {name:<18} {form:<10} {ok:<8} {value[:60]}")

    try:
        pythoncom.CoUninitialize()
    except Exception:  # noqa: BLE001
        pass

    out = MAPPING_ROOT / "_analysis" / "com_member_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整报告 -> {out}")
    print("   换机器出问题时，把这份文件发回来对比。")

    bad = [m["name"] for m in report["members"] if not m.get("usable")]
    if bad:
        print(f"\n⚠️ 读不出值的成员：{', '.join(bad)}")
        print("   —— 这正是换机器会炸的那类。请把报告发回来。")
        return 2
    print("\n✓ 全部成员都能正常读取。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
