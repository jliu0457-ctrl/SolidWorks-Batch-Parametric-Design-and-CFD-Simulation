#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量仿真：读 `Variables/*.xlsx`，逐个跑完整链路，**失败不中断、逐条记录**。

设计点由**别人**放进 `Variables/` 的 Excel 提供 —— 本程序**不生成数据**。

表格约定：第 1 行表头，第 1 列是 `样本序号`，其余列是七个设计变量
（`c_mm, e_mm, phi_deg, alpha_deg, Dmax_mm, bm_mm, ds_mm`）。
列**按表头名字**匹配，不按位置 —— 列顺序换了也不会读错。

用法::

    python scripts/Run-Batch.py                    # 只读预检（默认），不写任何盘
    python scripts/Run-Batch.py --execute          # 真正跑
    python scripts/Run-Batch.py --execute --limit 5        # 只跑前 5 个待跑的
    python scripts/Run-Batch.py --execute --only 3,7,12    # 只跑指定编号
    python scripts/Run-Batch.py --xlsx Variables/别的表.xlsx --execute

跑之前必须**手工启动 SolidWorks 并停在空白主界面**（铁律：绝不冷启动）。
每个样本约 3.5 分钟。

开度固定 45°（装配体配合 `D1@角度2`），**不参数化、不由命令行传入** ——
一行的七个变量就是一个样本，结果写进唯一一张 `outputs/training_dataset.xlsx`。

**失败不中断**：某个样本失败就记下原因、留一份诊断报告、删掉它的 run 目录，
**继续跑下一个**（失败只写报告、不追加数据行）。

只有两种情况会停，见 `should_stop_after_failure`：
**探活发现 SolidWorks 没响应**（主判据），或**连续失败到了上限**
（默认 10，`--max-consecutive-failures N` 可调，`0` = 不限）。
停了就处理完再 `--execute` 续跑，已完成的样本会自动跳过。

**定期重启 SolidWorks**（默认每 50 个样本一次，`--restart-every 0` 关闭）：
SolidWorks 工作集约 **64 MB/例** 线性累积（实测基线 477 MB），长跑必须定期换进程。
重启走"干净退出 → Flow 官方 `RunProduct2` 重新拉起 → 等就绪"，实测**约 18 秒**一次。
失败会重试 3 次，仍失败就停批处理等你处理 —— **不会安静降级成继续用旧进程**。
详见 `flow_session.restart_solidworks`。

⚠️ **判据是"报告"，不是退出码。** 2026-09-21 实测：SolidWorks 冷启动导致
`Run-OneDesign.exe` 连不上 Flow API 时，**退出码仍然是 0**，而报告里是
`completed=false`。所以每一步都读报告字段，绝不看 returncode。
"""

from __future__ import annotations

import argparse
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 旧版 Windows 终端可能默认 GBK；进度信息含中文/符号，不应因此中断批处理。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")

MAPPING_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MAPPING_ROOT.parent
SCRIPTS = MAPPING_ROOT / "scripts"
# ⚠️ MAPPING_ROOT 本身也要上路径 —— `valve_mapping` 是 SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/ 下的子包，
# 而 `python scripts/Run-Batch.py` 的 sys.path[0] 是 scripts/ 不是 cwd，不加上 import 不到。
for _p in (str(REPO_ROOT), str(SCRIPTS), str(MAPPING_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import flow_geometry as fg   # noqa: E402
import flow_session as fses  # noqa: E402

import machine_paths as mpaths  # noqa: E402
from valve_mapping.runs import validate_design  # noqa: E402

mpaths.require_python()   # 版本不对就在这儿停，别跑到一半报个看不懂的 AttributeError

RUNS_ROOT = fses.RUNS_ROOT
#: 别人放变量表的目录。**两种写法都认**（优先复数）—— 这个名字改过一次，
#: 认两种可以省掉一整类"明明放了表却说找不到"的困惑。
VARIABLE_DIR = next(
    (d for d in (MAPPING_ROOT / "Variables", MAPPING_ROOT / "Variable") if d.is_dir()),
    MAPPING_ROOT / "Variables")
BATCH_DIR = MAPPING_ROOT / "working" / "_batch"
STATE_PATH = BATCH_DIR / "batch_state.json"
DESIGNS_DIR = BATCH_DIR / "designs"
FAILURES_DIR = BATCH_DIR / "failures"

#: 成功样本跑完之后，只把报告搬到这里；CAD 副本和 Flow 结果当场删掉。
#:
#: 一个 run 目录 21 MB：CAD 2.8 MB、Flow 结果 18 MB，而报告只有 ~35 KB。
#: 按老口径「只留最新一个」磁盘占用是常数级，但代价是**成功样本一个都回查不了**；
#: 按新口径 3200 个样本的报告全留也就 ~110 MB，换来每个成功样本都能事后回查。
#: 中间产物只在跑这个样本的当下有意义，报告不是。
REPORTS_DIR = MAPPING_ROOT / "working" / "_sample_reports"

#: run 目录里要留下的文件，其余一律删。刻意不含 `flow_sample_state.json`（10 KB）：
#: 那是续跑用的状态，样本跑完之后就没用了，同样的阶段轨迹 `flow_sample.json`
#: 的 `stages` 里已经记了一份。
KEEP_IN_RUN_DIR = ("flow_sample.json", "training_sample.csv")

ID_COLUMN = fg.ID_COLUMNS[0]            # "样本序号"
ONE_DESIGN_EXE = SCRIPTS / "Run-OneDesign.exe"
RUN_SAMPLE = SCRIPTS / "Run-FlowSample.py"

#: 连续失败多少次就先停下。**0 = 不限**，可以用 `--max-consecutive-failures` 覆盖。
#:
#: 这是**兜底**，不是主判据 —— 主判据是探活（见 `should_stop_after_failure`）。
#: ⚠️ 别把它当"环境坏了"的判据：失败率只要不是零，它就迟早会误触发。
#: 参考量级：失败率 82% 时 P(连续 10 次) ≈ 14%（平均每 ~15 个样本停一次）；
#: 失败率 25% 时 P(连续 10 次) ≈ 0.0001%（几乎不触发）。
DEFAULT_MAX_CONSECUTIVE_FAILURES = 10

#: 每跑多少个样本重启一次 SolidWorks。**0 = 关闭**，可用 `--restart-every` 覆盖。
#:
#: 为什么需要（实测，`数据生成/参数范围CAD验证报告_2026-09-22.md:107-116`）：
#: SolidWorks 工作集随样本线性累积 —— 空载基线 477 MB、**约 64 MB/例**，
#: 16 GB 机器留 10 GB 余量 → 约 150 例就顶到上限。取 50 是留了 3 倍余量。
#: 重启的一次性成本约 456 MB + 18 秒（实测），摊到 50 例上可以忽略。
DEFAULT_RESTART_EVERY = 50

#: 重启失败重试几次再放弃。实测单次重启约 18 秒，重试成本低。
DEFAULT_RESTART_RETRIES = 3

#: 单步的外部超时（秒）。求解那一步由 `--timeout-min` 自己管，这里只兜底。
TIMEOUT_ONE_DESIGN = 600
TIMEOUT_PRE_SOLVE = 900
TIMEOUT_SOLVE = 2400

_SAFE_RUN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================ 读表（纯函数，可离线测）

def read_designs_from_xlsx(path: Path) -> list[dict]:
    """读一份变量表 → 每个样本一个 dict：``{"样本序号": id, "c_mm": ..., ...}``。

    **按表头名字取列**，不按位置 —— 列顺序变了也不会读错。
    缺列、编号重复、空行都会明确报错，不静默跳过（静默跳过 = 悄悄少跑样本）。
    """
    from openpyxl import load_workbook
    path = Path(path)
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        rows = [r for r in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    if not rows:
        raise ValueError(f"{path.name} 是空表")
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    need = {ID_COLUMN, *fg.DESIGN_COLUMNS}
    missing = sorted(need - set(header))
    if missing:
        raise ValueError(f"{path.name} 缺列 {missing}；表头实际是 {header}")

    index = {name: header.index(name) for name in need}
    out, seen = [], {}
    for offset, row in enumerate(rows[1:], start=2):
        if row is None or all(v is None for v in row):
            continue                       # 整行空 —— 跳过，不算错
        raw_id = row[index[ID_COLUMN]]
        if raw_id is None or str(raw_id).strip() == "":
            raise ValueError(f"{path.name} 第 {offset} 行没有编号")
        sample_id = raw_id if isinstance(raw_id, int) else str(raw_id).strip()
        if sample_id in seen:
            raise ValueError(f"{path.name} 编号 {sample_id!r} 重复（第 {seen[sample_id]} 行与第 {offset} 行）")
        seen[sample_id] = offset
        design = {}
        for name in fg.DESIGN_COLUMNS:
            value = row[index[name]]
            if value is None or isinstance(value, bool):
                raise ValueError(f"{path.name} 第 {offset} 行的 {name} 不是数字：{value!r}")
            design[name] = float(value)
        validate_design(design)            # 七字段 + 数值域，与单样本走同一套
        out.append({ID_COLUMN: sample_id, **design})
    if not out:
        raise ValueError(f"{path.name} 只有表头、没有数据行")
    return out


def collect_designs(xlsx: Path | None) -> tuple[list[dict], list[str]]:
    """扫 Variables/ 下全部 .xlsx（或指定的一个）→ 合并后的设计点列表 + 来源文件名。"""
    files = [Path(xlsx)] if xlsx else sorted(VARIABLE_DIR.glob("*.xlsx"))
    files = [f for f in files if not f.name.startswith("~$")]
    if not files:
        raise SystemExit(
            f"{VARIABLE_DIR} 下没有 .xlsx —— 请先把变量表放进去\n"
            "（表格格式：第 1 行表头，第 1 列是「样本序号」，其余列是七个设计变量）")
    designs, sources, seen = [], [], {}
    for path in files:
        for design in read_designs_from_xlsx(path):
            sid = design[ID_COLUMN]
            if sid in seen:
                raise SystemExit(
                    f"编号 {sid!r} 在 {seen[sid]} 和 {path.name} 里都出现 —— 两份表编号撞车，先改掉")
            seen[sid] = path.name
            designs.append(design)
            sources.append(path.name)
    return designs, sources


def run_name_for(sample_id) -> str:
    """样本编号 → run 目录名。非安全字符换成下划线。"""
    token = re.sub(r"[^A-Za-z0-9_-]", "_", str(sample_id))
    name = f"sample_{token}"
    if not _SAFE_RUN.match(name):
        raise ValueError(f"编号 {sample_id!r} 生成的 run 名不合法：{name!r}")
    return name


def dataset_for() -> Path:
    """训练表落点 —— **一份表**。"""
    return MAPPING_ROOT / "outputs" / "training_dataset.xlsx"


# ============================================================ 状态

def load_state() -> dict:
    if STATE_PATH.is_file():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"schema_version": 1, "updated_utc": now(), "samples": {}}


def archive_state() -> Path | None:
    """把现有进度归档（**不删**），返回归档路径；没有进度就返回 None。

    换数据集时用。归档而不是删除 —— 万一还想回头看旧那批跑过什么。
    """
    if not STATE_PATH.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = STATE_PATH.with_name(f"batch_state-{stamp}.json")
    shutil.move(str(STATE_PATH), str(dest))
    return dest


def state_roster(state: dict) -> set[str]:
    """状态里记着的样本序号。`__maintenance__` 这类特殊键不算。"""
    return {k for k in state.get("samples", {}) if k.isdigit()}


def save_state(state: dict) -> None:
    state["updated_utc"] = now()
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def record(state: dict, sample_id, status: str, **extra) -> None:
    entry = {"status": status, "at_utc": now(), **extra}
    state.setdefault("samples", {})[str(sample_id)] = entry
    save_state(state)


# ============================================================ 单样本

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                      # noqa: BLE001
        return {}


def _run(cmd: list[str], timeout: int, *, progress_path: Path | None = None) -> tuple[int, str]:
    """跑子进程，定期报告进度，返回 (退出码, 输出尾巴)。

    ⚠️ **stdout 和 stderr 都要**，不能"stdout 非空就不看 stderr"。
    §10.7 记过这个缺口：子进程真正的异常走 stderr，而 stdout 上往往只有
    `template -> ...` / `design -> ...` 这种正常流程输出 —— 于是异常被遮住，
    报错只显示一句看似无关的 template/design。
    """
    def decode(blob: bytes) -> str:
        for encoding in ("utf-8", locale.getpreferredencoding(False), "mbcs"):
            try:
                return blob.decode(encoding)
            except (UnicodeError, LookupError):
                pass
        return blob.decode("utf-8", errors="replace")

    env = os.environ.copy()
    # 子 Python 进程与当前解释器保持一致，输出也统一 UTF-8；C# 输出仍兼容本机编码。
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.monotonic()
    with subprocess.Popen(cmd, cwd=str(MAPPING_ROOT), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, env=env) as proc:
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                proc.kill()
                output, _ = proc.communicate()
                raise subprocess.TimeoutExpired(cmd, timeout, output=output)
            try:
                output, _ = proc.communicate(timeout=min(20, remaining))
                break
            except subprocess.TimeoutExpired:
                stage = (_read_json(progress_path).get("stage") if progress_path else None)
                detail = f"；CAD 阶段={stage}" if stage else (
                    "；尚无 CAD 阶段文件" if progress_path else "")
                print(f"    {Path(cmd[0]).name} 仍在运行 "
                      f"{time.monotonic() - started:.0f}s（上限 {timeout}s）{detail}...",
                      flush=True)
        combined = decode(output or b"").replace("\r\n", "\n").replace("\r", "\n").strip()
        return proc.returncode, combined[-2000:]


def _friendly_cad_error(text: str) -> str:
    """把 CAD 阶段的 C# 异常翻译成人话。

    适配器抛的是 `InvalidOperationException: RefuseSameNamedOpenDocuments` 外加一长串
    调用栈 —— 信息全在，但看不出"该干什么"。而这条错误**有明确的处置办法**：
    关掉 SolidWorks 里所有文档（或重启 SolidWorks 停在空白主界面）再续跑。

    ⚠️ **故意的行为，不是 bug**：适配器拒绝替你关闭同名文档，因为那可能是**用户自己**
    打开的、有未保存改动的文件。所以它宁可报错也不动手。
    """
    if "RefuseSameNamedOpenDocuments" in text or "same-named document" in text:
        # 从输出里挖出到底是哪个文件被占着
        import re as _re
        hit = _re.search(r"无法打开\s+(\S+)", text)
        which = f"\n      占用的文件：{hit.group(1)}" if hit else ""
        return (
            "SolidWorks 里还打开着上一个样本的文档，适配器拒绝覆盖同名文件"
            f"（它不会替你关闭可能未保存的用户文档）。{which}\n"
            "      处置：在 SolidWorks 里【关闭全部文档】（或重启 SolidWorks 停在空白主界面），"
            "再 --execute 续跑")
    return text[:1200]


def _failure_why(sample: dict, tail: str, fallback: str) -> str:
    """把失败原因拼成**能定位到行**的一段话。

    `Run-FlowSample.py` 在报告里同时写了 `error`（一行消息）和 `traceback`（完整栈），
    但从前只取了前者 —— 于是"某个包装过的 TypeError"就到此为止，看不出是哪一行。
    这里把 traceback 的最后几帧也带上。
    """
    parts = [str(sample.get("error") or tail or fallback)]
    tb = str(sample.get("traceback") or "").strip()
    if tb:
        # 只留最后的调用链，前面几帧是运行器自身的噪音
        frames = [ln.strip() for ln in tb.splitlines() if ln.strip()]
        parts.append("  " + "\n  ".join(frames[-6:]))
    return "\n".join(parts)[:1200]


def run_one(sample_id, design: dict, args) -> dict:
    """跑**一个样本** = 一个设计点。
    返回 ``{"ok": bool, "stage": ..., "why": ...}``。

    ⚠️ **每一步都读报告字段判成败，不看退出码**（见模块开头的实测记录）。
    """
    run = run_name_for(sample_id)
    run_dir = RUNS_ROOT / run
    design_json = DESIGNS_DIR / f"{run}.json"
    DESIGNS_DIR.mkdir(parents=True, exist_ok=True)
    # 写进去的是**七个设计输入**。适配器对字段数是硬校验，少一个会当场拒绝 ——
    # 所以整份 `DESIGN_COLUMNS` 都写，不要挑。
    # （`design` 里还带着 `样本序号`，它是元数据，不算输入，这里被过滤掉了。）
    design_json.write_text(
        json.dumps({k: design[k] for k in fg.DESIGN_COLUMNS}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    # 覆盖保护：同名 run 目录还在（上次崩了没清掉）→ 先删，否则 ① 会拒绝覆盖
    if run_dir.exists():
        shutil.rmtree(run_dir)

    # ---- ① CAD 参数化。不能接受上一次失败遗留的同名报告。
    cad_report = MAPPING_ROOT / "_analysis" / f"e2e_{run}.json"
    previous_mtime = cad_report.stat().st_mtime_ns if cad_report.is_file() else None
    code, tail = _run([str(ONE_DESIGN_EXE), ".", run, str(design_json)], TIMEOUT_ONE_DESIGN,
                      progress_path=run_dir / "mapping_progress.json")
    fresh_report = cad_report.is_file() and (previous_mtime is None or
                     cad_report.stat().st_mtime_ns != previous_mtime)
    report = _read_json(cad_report) if fresh_report else {}
    if code != 0 or not fresh_report or not (report.get("completed") and
                                              report.get("physical_mapping_verified")):
        why = ("CAD 未产生本次的新报告（可能卡在 SolidWorks 弹窗或 COM 连接）；" + tail
               if not fresh_report else report.get("error") or tail or
               "报告缺 completed/physical_mapping_verified")
        return {"ok": False, "stage": "cad", "why": _friendly_cad_error(str(why)),
                "run": run, "fresh_cad_report": bool(fresh_report)}
    s_mm = report.get("internal_s_mm")

    # ---- ② 求解前
    code, tail = _run([sys.executable, str(RUN_SAMPLE), run, "--design", str(design_json),
                       "--execute", "--sample-id", str(sample_id),
                       "--dataset-xlsx", str(dataset_for())], TIMEOUT_PRE_SOLVE)
    sample = _read_json(run_dir / "flow_sample.json")
    if sample.get("status") != "awaiting_solve_confirmation":
        return {"ok": False, "stage": "pre_solve",
                "why": _failure_why(sample, tail, str(sample.get("status"))),
                "run": run, "internal_s_mm": s_mm}

    # ---- ③ 求解 + 出训练行
    code, tail = _run([sys.executable, str(RUN_SAMPLE), run, "--design", str(design_json),
                       "--execute", "--resume", "--yes-solve",
                       "--timeout-min", str(args.solve_timeout_min),
                       "--sample-id", str(sample_id),
                       "--dataset-xlsx", str(dataset_for())], TIMEOUT_SOLVE)
    sample = _read_json(run_dir / "flow_sample.json")
    problems = ((sample.get("stages") or {}).get("results") or {}).get("problems") or []
    appended = (sample.get("training_row_written") or {}).get("appended")
    deduped = (sample.get("training_row_written") or {}).get("deduplicated")
    if sample.get("status") != "completed" or problems or not (appended or deduped):
        return {"ok": False, "stage": "solve",
                "why": _failure_why(sample, tail, str(problems or sample.get("status"))),
                "run": run, "internal_s_mm": s_mm}
    return {"ok": True, "run": run, "internal_s_mm": s_mm,
            "cv": ((sample.get("stages") or {}).get("training_row") or {}).get("row", {}).get("Cv"),
            "deduplicated": bool(deduped)}


def _mb(value) -> str:
    return "未知" if value is None else f"{value:.0f} MB"


def maintenance_restart(state: dict, every: int, ran: int) -> tuple[bool, str]:
    """跑满 `every` 个样本后重启一次 SolidWorks。返回 ``(是否成功, 说明)``。

    ⚠️ **只在样本边界调用。** 调用方负责保证：上一个样本已结算、run 目录已处理、
    状态已落盘、文档已关。本函数自己会再确认一遍求解器没在跑（见
    `flow_session.restart_solidworks` 的前置检查）。

    ⚠️ **不能插在样本内部。** 一个样本要起 3 次子进程（CAD / 求解前 / 求解），
    中途换 SolidWorks 进程会让 `--resume` 的"内存未保存"陷阱变成常态，
    每个样本白花 ~20 秒。

    失败时返回 False，调用方必须停批处理 —— 状态已落盘，`--execute` 续跑即可。
    绝不"安静降级成继续用旧进程"：那样内存没释放，与初衷相违。
    """
    before = fses.solidworks_memory_mb()
    print(f"\n{'─' * 60}")
    print(f"【维护】已跑 {ran} 个样本 → 重启 SolidWorks（重启前 {_mb(before)}）", flush=True)
    began = time.time()
    try:
        result = fses.restart_solidworks(retries=DEFAULT_RESTART_RETRIES)
    except Exception as exc:                # noqa: BLE001
        result = {"ok": False, "why": f"{type(exc).__name__}: {exc}"}
    took = time.time() - began

    after = fses.solidworks_memory_mb()
    record(state, "__maintenance__", "done" if result.get("ok") else "failed",
           kind="solidworks_restart", samples_since_last=int(ran),
           memory_before_mb=before, memory_after_mb=after,
           seconds=round(took, 1),
           attempts=len(result.get("attempts") or []),
           why=None if result.get("ok") else result.get("why"))

    if result.get("ok"):
        freed = (before - after) if (before is not None and after is not None) else None
        print(f"    ✅ 重启完成  {took:.0f}s  {_mb(before)} → {_mb(after)}"
              + (f"（释放 {freed:.0f} MB）" if freed is not None else ""), flush=True)
        return True, "ok"

    print(f"    ❌ 重启失败（{took:.0f}s，试了 {len(result.get('attempts') or [])} 次）："
          f"{result.get('why')}", flush=True)
    return False, f"SolidWorks 重启失败：{result.get('why')}"


def should_stop_after_failure(consecutive: int, *, limit: int) -> tuple[bool, str]:
    """失败之后要不要停 —— 两个判据，**任一命中就停**：

    1. **连续失败次数到上限**（`limit`，0 = 不限）—— 兜底。
    2. **探活发现 SolidWorks 没响应** —— 主判据。

    为什么探活是主判据：样本失败有两个完全不同的原因，光看次数分不开 ——
      * 这个设计点造不出来（CAD 重建失败、面绑不上）→ **该继续跑下一个**
      * 环境没了（SolidWorks 掉了）→ **该停**，之后每个样本都只会是同一种失败
    探活能直接问出是哪一个，不用猜。次数上限则用来兜"原因各异但一直在倒"的情况。

    ⚠️ 别把次数上限当成"环境坏了"的判据 —— 失败率只要不是零它迟早误触发。
    参考：失败率 82% 时 P(连续 10 次) ≈ 14%；失败率 25% 时 ≈ 0.0001%。
    """
    if limit > 0 and consecutive >= limit:
        return True, f"连续失败已达上限 {limit} 次"
    alive, why = fses.probe_alive()
    return (not alive), f"SolidWorks 没响应：{why}"


def keep_failure_evidence(run: str, sample_id, *, include_cad_report: bool = True) -> str | None:
    """删 run 目录之前，把两份诊断报告拷出来 —— 否则失败原因只剩一句摘要。"""
    FAILURES_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = RUNS_ROOT / run
    kept = []
    sources = [(run_dir / "flow_sample.json", "flow_sample")]
    if include_cad_report:
        sources.append((MAPPING_ROOT / "_analysis" / f"e2e_{run}.json", "e2e"))
    for src, suffix in sources:
        if src.is_file():
            dest = FAILURES_DIR / f"{run}.{suffix}.json"
            shutil.copy2(src, dest)
            kept.append(dest.name)
    return ", ".join(kept) or None


def keep_sample_report(run: str) -> str | None:
    """成功样本落盘之后：把报告搬进 `_sample_reports/`，再把整个 run 目录删掉。

    ⚠️ **只能在样本成功走完之后调。** `Run-FlowSample.py --resume` 靠比对盘上的几何哈希
    判断能不能续跑（`resume_is_safe`），对没跑完的样本删目录等于把续跑这条路断掉。
    成功样本没有这个问题：训练行已经进表、门禁全过，不再需要续跑。

    返回留下的文件名，一个都没留则返回 None。
    """
    run_dir = RUNS_ROOT / run
    if not run_dir.is_dir():
        return None
    if not (run_dir / KEEP_IN_RUN_DIR[0]).is_file():
        # 主报告都不在，这个目录不是"跑成功的样子"。**宁可留着占 21 MB，也不要删完
        # 什么都没剩下** —— 报告是事后唯一能回查的东西，目录可以再删，报告丢了就没了。
        return None
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    kept = []
    for name in KEEP_IN_RUN_DIR:
        src = run_dir / name
        if src.is_file():
            dest = REPORTS_DIR / f"{run}.{name}"
            shutil.copy2(src, dest)
            kept.append(dest.name)
    shutil.rmtree(run_dir, ignore_errors=True)
    return ", ".join(kept) or None


# ============================================================ 主流程

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="批量仿真：读 Variables/*.xlsx，逐个跑，失败不中断")
    p.add_argument("--execute", action="store_true", help="真的跑（默认只预检）")
    p.add_argument("--strict-preflight", action="store_true",
                   help="体检失败就停止；默认体检仅提示，不阻止批处理")
    p.add_argument("--xlsx", default=None, help="只读这一份表（默认扫 Variables/ 下全部 .xlsx）")
    p.add_argument("--limit", type=int, default=0, help="最多跑 N 个待跑的样本")
    p.add_argument("--only", default="", help="只跑这些编号，逗号分隔，如 3,7,12")
    p.add_argument("--solve-timeout-min", type=float, default=25.0)
    p.add_argument("--redo", action="store_true", help="已完成/已失败的也重跑")
    p.add_argument("--fresh", action="store_true",
                   help="**换数据集时用**：把旧进度归档（不删），序号从零开始认。"
                        "不加它而直接换表，序号相同的样本会被当成「已跑过」跳过 —— "
                        "因为程序只按序号判断跑没跑过，不比对设计变量")
    p.add_argument("--keep-run-dirs", action="store_true",
                   help="跑完不删 CAD 副本与 Flow 结果（默认删掉，只把报告搬进 "
                        f"{REPORTS_DIR.name}/）。排查某个具体样本时才用")
    p.add_argument("--max-consecutive-failures", type=int,
                   default=DEFAULT_MAX_CONSECUTIVE_FAILURES,
                   help=f"连续失败多少次就先停下（默认 {DEFAULT_MAX_CONSECUTIVE_FAILURES}，0 = 不限）。"
                        "探活发现 SolidWorks 失联会立即停，不看这个数")
    p.add_argument("--restart-every", type=int, default=DEFAULT_RESTART_EVERY,
                   help=f"每跑多少个样本重启一次 SolidWorks（默认 {DEFAULT_RESTART_EVERY}，0 = 关闭）。"
                        "防内存累积：实测约 64 MB/例、空载基线 477 MB，"
                        "16 GB 机器约 150 例就顶到上限。重启失败会重试 3 次，仍失败则停下等你处理")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    xlsx = Path(args.xlsx) if args.xlsx else None
    if xlsx is not None and not xlsx.is_absolute():
        xlsx = MAPPING_ROOT / xlsx
    designs, sources = collect_designs(xlsx)
    # --fresh 的只读预检不能改状态；真正执行前先给出体检结果再归档。
    state = ({"schema_version": 1, "updated_utc": now(), "samples": {}}
             if args.fresh else load_state())
    done = state.get("samples", {})

    # 只按序号判断跑没跑过。若状态里记着的序号在当前表里已经不存在，
    # 说明这份进度是**另一批数据**留下的 —— 必须让人知道，否则会静默跳过。
    stale = sorted(state_roster(state) - {str(d[ID_COLUMN]) for d in designs}, key=str)
    if stale:
        print(f"⚠️ 状态里有 {len(stale)} 个序号在当前的变量表里找不到："
              f"{', '.join(stale[:10])}{' ...' if len(stale) > 10 else ''}")
        print("   这批进度可能来自另一份变量表。换数据集请加 --fresh。")

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    if only:
        designs = [d for d in designs if str(d[ID_COLUMN]) in only]
        if not designs:
            raise SystemExit(f"--only {sorted(only)} 一个都没匹配上")

    # 一个设计点 = 一个样本。
    pending = [d for d in designs
               if args.redo or done.get(str(d[ID_COLUMN]), {}).get("status") != "done"]
    if args.limit:
        pending = pending[:args.limit]

    print(f"变量表   : {', '.join(sorted(set(sources)))}")
    print(f"共       : {len(designs)} 个样本")
    print(f"已完成   : {sum(1 for d in designs if done.get(str(d[ID_COLUMN]), {}).get('status') == 'done')}")
    print(f"已失败   : {sum(1 for d in designs if done.get(str(d[ID_COLUMN]), {}).get('status') == 'failed')}")
    print(f"本次要跑 : {len(pending)} 个")
    if pending:
        ids = [str(d[ID_COLUMN]) for d in pending]
        print(f"           编号 {', '.join(ids[:12])}{' ...' if len(ids) > 12 else ''}")
        print(f"           预计约 {len(pending) * 3.5 / 60:.1f} 小时")
    print(f"训练表   : {dataset_for().relative_to(MAPPING_ROOT)}")
    if args.restart_every:
        n_restarts = max(0, (len(pending) - 1) // args.restart_every) if pending else 0
        print(f"定期重启 : 每 {args.restart_every} 个样本一次"
              f"（本次预计 {n_restarts} 次，每次约 18 秒）")
    else:
        print("定期重启 : 关闭（--restart-every 0）")
    if not args.execute:
        print("\n（只读预检。确认无误后加 --execute）")
        return 0
    if not pending:
        print("\n没有要跑的样本。")
        return 0

    # 体检默认只是提示：跨机器的检查可能过严，用户仍可试跑。
    # 真正的 CAD/Flow 门禁及失败隔离不变；需要硬拦截时显式 --strict-preflight。
    print("\n执行前体检：", flush=True)
    try:
        code, tail = _run([sys.executable, str(SCRIPTS / "Doctor.py"), "--require-sw"], 45)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"⚠️ 体检未能完成：{type(exc).__name__}: {exc}", flush=True)
        if args.strict_preflight:
            print("--strict-preflight 已启用：停止，尚未创建样本。", flush=True)
            return 2
        print("体检仅供提示，继续运行；请留意后续 CAD/Flow 的实际报错。", flush=True)
    else:
        # Doctor 的完整诊断可单独运行；批处理只显示最后的结论段。
        verdict = tail[tail.rfind("[FAIL]"):] if "[FAIL]" in tail else (
            tail[tail.rfind("[OK]"):] if "[OK]" in tail else tail[-700:])
        print(verdict, flush=True)
        if code != 0:
            if args.strict_preflight:
                print("--strict-preflight 已启用：停止，尚未创建样本。", flush=True)
                return 2
            print("⚠️ 体检未通过，但按默认策略继续运行；以 CAD/Flow 实际门禁为准。",
                  flush=True)
    if args.fresh:
        archived = archive_state()
        print(f"（--fresh：旧进度归档到 {archived.name}，从零开始）" if archived
              else "（--fresh：本来就没有进度）")

    # 保留策略：样本门禁全过、训练行进表之后，才动它的 run 目录（顺序不能反）。
    # 默认删掉 21 MB 的 CAD 副本与 Flow 结果、把 35 KB 的报告搬去 `_sample_reports/`。
    failures: list[dict] = []
    consecutive_failures = 0
    #: 距上次重启跑了多少个样本。**只在本次会话内计数** —— 停批处理时进程本来就是新的，
    #: 重启后从 0 起算才对。成功和失败都计数：内存累积来自反复开关文档，与成败无关。
    samples_since_restart = 0

    for n, design in enumerate(pending, start=1):
        sample_id = design[ID_COLUMN]
        print(f"\n[{n}/{len(pending)}] 样本 {sample_id}  {now()}", flush=True)
        started = time.time()
        try:
            result = run_one(sample_id, design, args)
        except subprocess.TimeoutExpired as exc:
            raw = exc.output or b""
            tail = (raw.decode("utf-8", errors="replace") if isinstance(raw, bytes)
                    else str(raw))[-600:]
            result = {"ok": False, "stage": "timeout",
                      "why": f"超时 {exc.timeout}s：{exc.cmd[:2]}；末尾输出：{tail}",
                      "run": run_name_for(sample_id)}
        except Exception as exc:           # noqa: BLE001
            result = {"ok": False, "stage": "exception",
                      "why": f"{type(exc).__name__}: {exc}",
                      "run": run_name_for(sample_id)}
        took = time.time() - started

        if result["ok"]:
            consecutive_failures = 0
            record(state, sample_id, "done",
                   run=result["run"], seconds=round(took, 1),
                   internal_s_mm=result.get("internal_s_mm"), cv=result.get("cv"),
                   deduplicated=result.get("deduplicated"))
            print(f"    ✅ 完成  {took:.0f}s   Cv={result.get('cv')}", flush=True)
            # 训练行已经进表、门禁全过 —— 此刻才动 run 目录。删重文件、留报告。
            if args.keep_run_dirs:
                print(f"    （--keep-run-dirs：完整副本留在 {result['run']}）")
            else:
                kept = keep_sample_report(result["run"])
                if kept:
                    print(f"    （删掉 CAD 副本与 Flow 结果，报告存到 {REPORTS_DIR.name}/：{kept}）",
                          flush=True)
                else:
                    print(f"    ⚠️ {result['run']} 里没有主报告，**没有删** —— 请人工看一眼",
                          flush=True)
        else:
            consecutive_failures += 1
            evidence = keep_failure_evidence(
                result["run"], sample_id,
                include_cad_report=result.get("fresh_cad_report", True))
            shutil.rmtree(RUNS_ROOT / result["run"], ignore_errors=True)
            record(state, sample_id, "failed",
                   stage=result["stage"], why=result["why"], evidence=evidence,
                   seconds=round(took, 1))
            failures.append({"id": sample_id, **result})
            print(f"    ❌ 失败于 {result['stage']}  {took:.0f}s\n       {result['why']}", flush=True)
            # 默认**失败不中断**：记下来、继续跑下一个。
            # 只有两个判据命中才停（见 should_stop_after_failure）：
            # 探活发现 SolidWorks 失联，或连续失败到了上限。
            stop, why = should_stop_after_failure(
                consecutive_failures, limit=args.max_consecutive_failures)
            if stop:
                print(f"\n⚠️ 停下：{why}。"
                      "处理完再 --execute 续跑；已完成的样本会自动跳过。", flush=True)
                break

        # ---- 循环尾部：定期重启维护（**只能在样本边界**，此处必然已结算完毕）
        samples_since_restart += 1
        # 最后一个样本之后不重启 —— 重启有 ~400 MB 的一次性成本（实测），
        # 后面没样本可跑了就纯属白花 17 秒。
        if (args.restart_every and samples_since_restart >= args.restart_every
                and n < len(pending)):
            ok, why = maintenance_restart(state, args.restart_every, samples_since_restart)
            if not ok:
                print(f"\n⚠️ 停下：{why}。\n"
                      "  SolidWorks 没回到可用状态 —— 手工启动并停在空白主界面后，"
                      "再 --execute 续跑（已完成的样本会自动跳过）。", flush=True)
                break
            samples_since_restart = 0

    print(f"\n{'=' * 60}")
    print(f"本轮结束：成功 {len(pending[:n]) - len(failures)}，失败 {len(failures)}，共跑 {n}")
    if failures:
        print("\n失败清单：")
        for f in failures:
            print(f"  样本 {str(f['id']):<6} {f['stage']:<12} {f['why']}")
    print(f"\n状态文件 : {STATE_PATH}")
    print(f"失败证据 : {FAILURES_DIR}")
    if not args.keep_run_dirs:
        print(f"样本报告 : {REPORTS_DIR}")
    print(f"训练表   : {dataset_for()}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
