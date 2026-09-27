#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SolidWorks / Flow 会话：只附加、门禁、文档生命周期。

除清理已确认的 ``NVOGLDC invisible`` 孤儿进程和残留 ``~$`` 锁文件外，本模块不写
CAD/Flow 数据；正常或状态不明的 SolidWorks 进程绝不结束。

两条铁律（踩过坑，见交接文档 §10）：

  1. **不冷启动 SolidWorks。** 必须由人先手工启动、停在空白主界面，脚本只附加；
     附不上就退出并报告。没有已运行实例时创建 SolidWorks 会
     `CO_E_SERVER_EXEC_FAILURE (0x80080005)` 紧接着 `0x00000003` 崩溃。
     （`Run-FlowSingle.py:415` 的 `RunProduct2` 属于自启动分支，本模块不复用。）

  2. **同一时刻只允许一个 SolidWorks 实例、一个装配体窗口。**
     `flow_transfer.find_sldworks_pid()` 取 tasklist 的第一行 —— 有两个实例时会绑错，
     所以这里要求恰好一个，否则直接报错。

门禁分两段：``SwSession.gate_or_raise()`` 是不需要打开文档就能查的；
打开装配体之后还要调 ``assert_no_flow_project()`` —— 模板里残留着悬空的
Flow 工程注册（`assembly_batch_v6\\1` 被登记但目录不存在），那是弹模态框的引信。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

MAPPING_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = MAPPING_ROOT.parent
sys.path.insert(0, str(REPO_ROOT))

MANIFEST_PATH = MAPPING_ROOT / "config" / "cad_template_manifest_v6.json"
TEMPLATE_DIR = MAPPING_ROOT / "working" / "assembly_batch_v6"
RUNS_ROOT = (MAPPING_ROOT / "working" / "seven_variable_trials").resolve()

REQUIRED_REVISION_PREFIX = "34."

#: SolidWorks 的安装位置**不写死** —— 由 `machine_paths` 从注册表探测（或读覆盖文件）。
#: 这样整个项目文件夹能直接搬到别的电脑上跑。见 `machine_paths.py` 的模块 docstring。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import machine_paths as mpaths  # noqa: E402
import sw_api as swa  # noqa: E402  读取 COM 成员一律走它，见下面的 _member()

#: 重启时等主进程退出的上限（秒）。`ExitApp` 通常几秒内生效。
RESTART_EXIT_TIMEOUT_S = 60.0
#: 重启后等新实例就绪的上限（秒）。照 `launch_sw.py` 的 90 次 × 2 秒 = 180 秒。
RESTART_LAUNCH_TIMEOUT_S = 180.0
#: 轮询间隔（秒）。
RESTART_POLL_S = 2.0


def _member(obj: Any, *names: str, default: Any = None) -> Any:
    """读 SolidWorks 的 COM 成员，**自动抹平「属性 vs 方法」的差异**。

    ⚠️ **别直接写 `sw.RevisionNumber()`。** SolidWorks 的 COM 成员在不同 pywin32
    绑定下有的暴露成方法、有的暴露成属性 —— 是属性时加括号会
    `TypeError: 'str' object is not callable`。

    这个坑项目里早有记录（`sw_api.py:21`：`comp.GetPathName` ✓ **属性**，
    加括号会 `'str' object is not callable`），但 `RevisionNumber` 这两处
    （门禁与探活）一直直接调用，**在只跑过一台机器时没暴露**。
    实测：本机 pywin32 把它当方法（能用），另一台机器（Anaconda 自带 pywin32）
    把它当属性 —— 换机器直接炸。

    统一走 `sw_api.safe_get`：是真 Python 方法就调用，否则原样返回。
    """
    return swa.safe_get(obj, *names, default=default)


class GateFailure(RuntimeError):
    """门禁不过。调用方必须就此停止，不得继续写入。"""


class ModalDialogBlocked(RuntimeError):
    """疑似被模态对话框卡住。调用方应告诉用户手动关掉后 --resume。"""


# ---------------------------------------------------------------- 进程与哈希

@dataclass(frozen=True)
class SolidWorksProcess:
    """`tasklist /V` 能可靠取得的 SolidWorks 进程信息。"""

    pid: int
    window_title: str = ""
    status: str = ""
    #: 工作集，KB。`tasklist` 给的是 `"1,412,345 K"` 这种带千分位逗号的字符串。
    #: 拿不到就是 None —— 内存只是**观测**，不参与任何判据，缺了不能影响主流程。
    memory_kb: int | None = None

    @property
    def memory_mb(self) -> float | None:
        return None if self.memory_kb is None else self.memory_kb / 1024.0


def _parse_memory_kb(raw: str) -> int | None:
    """``"1,412,345 K"`` → ``1446220``（KB）。认不出就 None。"""
    text = str(raw or "").strip().replace(",", "").replace("\xa0", " ")
    if not text:
        return None
    token = text.split()[0]
    try:
        return int(token)
    except ValueError:
        return None


def _powershell_processes(name: str) -> list[dict]:
    """tasklist 被安全策略拒绝时，用 Windows 自带 Get-Process 做只读回退。"""
    if name not in ("SLDWORKS", "EFDsolver"):
        raise ValueError("只允许查询 SolidWorks/Flow 的固定进程名")
    command = (f"@(Get-Process -Name '{name}' -ErrorAction SilentlyContinue | "
               "Select-Object Id,WorkingSet64,MainWindowTitle) | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                              "-Command", command], capture_output=True, text=True,
                             timeout=30, errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise GateFailure(f"Get-Process 回退调用失败：{exc}") from exc
    if out.returncode:
        raise GateFailure(f"Get-Process 回退失败（{out.returncode}）：{out.stderr or out.stdout}")
    try:
        data = json.loads(out.stdout) if out.stdout.strip() else []
    except ValueError as exc:
        raise GateFailure(f"Get-Process 回退输出不是 JSON：{out.stdout[:200]}") from exc
    return [data] if isinstance(data, dict) else data


def list_solidworks_processes() -> list[SolidWorksProcess]:
    """所有 SLDWORKS.exe 进程；保留窗口标题以识别已知的无界面残留。"""
    try:
        out = subprocess.run(["tasklist", "/V", "/FI", "IMAGENAME eq SLDWORKS.exe",
                              "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=30, errors="replace")
    except Exception as exc:  # noqa: BLE001
        out = None
        tasklist_error = str(exc)
    else:
        tasklist_error = ((out.stderr or out.stdout) if out.returncode else "")
    if out is None or out.returncode:
        try:
            rows = _powershell_processes("SLDWORKS")
            return [SolidWorksProcess(
                pid=int(row["Id"]), window_title=str(row.get("MainWindowTitle") or ""),
                memory_kb=(int(row["WorkingSet64"]) // 1024
                           if row.get("WorkingSet64") is not None else None))
                for row in rows]
        except Exception as exc:  # noqa: BLE001
            raise GateFailure(f"tasklist 调用失败：{tasklist_error}；"
                              f"Get-Process 回退也失败：{exc}") from exc
    processes: list[SolidWorksProcess] = []
    for parts in csv.reader(io.StringIO(out.stdout)):
        if len(parts) < 2 or parts[0].strip().lower() != "sldworks.exe":
            continue
        try:
            pid = int(parts[1].strip())
        except ValueError:
            continue
        processes.append(SolidWorksProcess(
            pid=pid,
            window_title=parts[-1].strip() if len(parts) >= 9 else "",
            status=parts[5].strip() if len(parts) >= 6 else "",
            memory_kb=_parse_memory_kb(parts[4]) if len(parts) >= 5 else None,
        ))
    return processes


def list_solidworks_pids() -> list[int]:
    """所有 SLDWORKS.exe 的 PID。**不用 flow_transfer 那一套「取第一行」。**"""
    return [p.pid for p in list_solidworks_processes()]


def solidworks_memory_mb() -> float | None:
    """当前 SolidWorks 的工作集（MB）。多个实例时取最大的那个，取不到返回 None。

    实测曲线（`数据生成/参数范围CAD验证报告_2026-09-22.md:107-116`）：空载基线 477 MB、
    约 64 MB/例累积。这里只**观测**、不判据 —— 用量用来事后校准重启间隔对不对。
    """
    try:
        sizes = [p.memory_mb for p in list_solidworks_processes() if p.memory_mb is not None]
    except GateFailure:
        # 工作集只是观测值；目标机若拒绝 tasklist /V，不应先于求解器/PID 安全
        # 检查抛错，也不应因此使维护重启失效。
        return None
    return max(sizes) if sizes else None


def cleanup_empty_solidworks_processes() -> dict:
    """结束能确定为 ``NVOGLDC invisible`` 的无界面残留进程。

    空标题也可能只是正在启动，不能据此强杀。结束前会重新核对 PID 和标题，防止 PID
    复用或状态变化；正常窗口和状态不明确的进程一律不碰。
    """
    marker = "nvogldc invisible"
    before = list_solidworks_processes()
    candidates = [p for p in before if p.window_title.casefold() == marker]
    removed: list[int] = []
    failed: list[dict[str, Any]] = []
    for candidate in candidates:
        current = {p.pid: p for p in list_solidworks_processes()}.get(candidate.pid)
        if current is None or current.window_title.casefold() != marker:
            failed.append({"pid": candidate.pid, "reason": "结束前复核时进程已消失或标题已变化"})
            continue
        result = subprocess.run(["taskkill", "/PID", str(candidate.pid), "/T", "/F"],
                                capture_output=True, text=True, timeout=30, errors="replace")
        if result.returncode == 0:
            removed.append(candidate.pid)
        else:
            failed.append({"pid": candidate.pid,
                           "reason": (result.stderr or result.stdout).strip()})
    after = list_solidworks_processes()
    return {
        "marker": "NVOGLDC invisible",
        "candidates": [p.pid for p in candidates],
        "removed": removed,
        "failed": failed,
        "remaining": [p.pid for p in after],
        "ambiguous_not_touched": [p.pid for p in before
                                  if p.window_title.casefold() != marker],
        "ok": not failed,
    }


def solver_running() -> bool:
    """EFDsolver.exe 是否在跑。求解阶段靠它判「求解器已退出」。"""
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq EFDsolver.exe", "/NH"],
                             capture_output=True, text=True, timeout=30, errors="replace")
    except Exception:  # noqa: BLE001
        out = None
    if out is not None and out.returncode == 0:
        return "efdsolver.exe" in out.stdout.lower()
    # 两种枚举方式都不可用时必须抛错：把「未知」当成「未在求解」会误重启 SW。
    return bool(_powershell_processes("EFDsolver"))


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def lock_files_under(folder: Path) -> list[Path]:
    """`~$*` 残留锁文件。崩溃或异常退出会留下它们。

    适配器里写的是 ``Directory.GetFiles(folder, "*.SLDASM").Single()``，
    匹配到两个就抛「序列包含一个以上的元素」—— 表现成适配器内部报错，看不出是文件问题。
    """
    return sorted(p for p in folder.rglob("~$*") if p.is_file())


def verify_template_manifest(manifest_path: Path = MANIFEST_PATH,
                             template_dir: Path = TEMPLATE_DIR) -> dict:
    """模板逐字节核对。跑完任何一环都要核一次 —— 靠它抓「什么时候被谁动了」。

    判据只有两条：清单里登记的文件都在、且哈希相符。
    **「多出来的文件」只报告、不判失败** —— 清单故意只登记 7 个 CAD 文件，
    而目录里的 `…_project_folders.html` 是 Flow 写的索引文件，不是 CAD 数据。
    真正要拦的是「装配体里还注册着 Flow 工程」，那要打开文档读
    `GetProjectNames()` 才知道，见 ``SwSession.assert_no_flow_project``。
    """
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    expected = manifest["files"]
    problems, checked = [], {}
    for name, want in expected.items():
        target = Path(template_dir) / name
        if not target.is_file():
            problems.append(f"模板缺文件 {name}")
            continue
        got = sha256_of(target)
        checked[name] = got
        if got != want:
            problems.append(f"{name} 哈希不符：期望 {want[:16]}… 实际 {got[:16]}…")
    extra = sorted(p.name for p in Path(template_dir).iterdir()
                   if p.is_file() and p.name not in expected)
    return {
        "template_id": manifest.get("template_id"),
        "checked": checked,
        "extra_files": extra,
        "extra_files_note": "未登记文件仅供参照；_project_folders.html 是 Flow 索引，"
                            "它出现说明装配体里可能还挂着工程注册 —— 以打开后的 "
                            "GetProjectNames() 为准",
        "problems": problems,
        "ok": not problems,
    }


# ---------------------------------------------------------------- 会话

@dataclass
class SwSession:
    """一个已附加的 SolidWorks + Flow API 会话。"""

    sw: Any
    api: Any
    interactive: Any
    pid: int | None = None
    residual_cleanup: dict[str, Any] = field(default_factory=dict)
    _closed: bool = field(default=False, repr=False)

    # -- 附加 --------------------------------------------------------

    @classmethod
    def attach(cls, *, retries: int = 20, delay: float = 3.0) -> "SwSession":
        import pythoncom
        import win32com.client as win32

        pythoncom.CoInitialize()

        cleanup = cleanup_empty_solidworks_processes()
        pids = list_solidworks_pids()
        if not pids:
            raise GateFailure("没有运行中的 SolidWorks。请先手工启动并停在空白主界面 —— "
                              "本流程不冷启动 SolidWorks。")
        if len(pids) > 1:
            raise GateFailure(
                f"检测到 {len(pids)} 个 SLDWORKS.exe 进程 {pids} —— 无法判定该附加哪一个。"
                "请只保留一个实例后重试。")

        sw = None
        last = None
        for _ in range(retries):
            try:
                sw = win32.GetActiveObject("SldWorks.Application")
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(delay)
        if sw is None:
            raise GateFailure(
                f"无法附加到 SolidWorks（试了 {retries} 次）：{last}\n"
                "  `MK_E_UNAVAILABLE` 不一定代表没开 —— 也可能是当前权限看不到交互式桌面的"
                "COM 运行对象表；请在与桌面同一会话的权限下运行。")

        # flow_transfer.connect() 失败时直接 sys.exit()，不会解 COM —— 这里包一层。
        import flow_transfer as ft
        try:
            api = ft.connect()
        except SystemExit as exc:  # noqa: PERF203
            raise GateFailure(f"Flow 产品 API 加载失败（flow_transfer.connect 退出码 {exc.code}）") from exc
        interactive = ft.safe_get(api, "Attach2RunningObject")
        if interactive is None:
            try:
                api.UnloadProductAPI()
            except Exception:  # noqa: BLE001
                pass
            raise GateFailure("Flow API 无法附加到运行中的 SolidWorks 实例。")

        return cls(sw=sw, api=api, interactive=interactive, pid=pids[0],
                   residual_cleanup=cleanup)

    # -- 门禁（不需要打开文档的部分）------------------------------

    def gate_or_raise(self, *, run: Path | None = None,
                      template_dir: Path = TEMPLATE_DIR,
                      manifest_path: Path = MANIFEST_PATH) -> dict:
        """写任何东西之前必须过。只读，不启动任何东西。"""
        report: dict[str, Any] = {}
        problems: list[str] = []
        report["residual_process_cleanup"] = self.residual_cleanup

        revision = str(_member(self.sw, "RevisionNumber", default=""))
        report["revision"] = revision
        if not revision.startswith(REQUIRED_REVISION_PREFIX):
            problems.append(f"SolidWorks 版本 {revision}，期望 {REQUIRED_REVISION_PREFIX}x")

        # ⚠️ 一律走 `_member` —— `Visible` / `ActiveDoc` 在**这台**机器上是属性，
        # 换台 pywin32 绑定不同的机器可能变方法。若是方法而直接读，
        # `bool(<绑定方法>)` 恒为 True —— 可见性检查会**静默失效**（不报错，比崩溃更糟）。
        visible = bool(_member(self.sw, "Visible", default=False))
        report["visible"] = visible
        if not visible:
            problems.append("SolidWorks 不可见（Visible=False）")

        active = _member(self.sw, "ActiveDoc")
        report["active_doc"] = None if active is None else str(_member(active, "GetTitle", default=""))
        if active is not None:
            problems.append(f"有已打开的文档 {report['active_doc']!r} —— 门禁要求全部关闭")

        report["solidworks_pids"] = list_solidworks_pids()
        if len(report["solidworks_pids"]) != 1:
            problems.append(f"SLDWORKS.exe 实例数 {len(report['solidworks_pids'])}，期望恰好 1")

        if run is not None:
            run = Path(run).resolve()
            if RUNS_ROOT != run and RUNS_ROOT not in run.parents:
                problems.append(f"run 目录 {run} 不在 {RUNS_ROOT} 之下")
            report["run"] = str(run)
            # `<run>/1` 是 Flow 的工程目录。上一轮残留时**只记录、不判否** ——
            # 重建工程那条路（create_project）自己会先清掉它；
            # 在这里拦死会逼人手工删目录，而那正是要避免的手工步骤。
            project_dir = run / "1"
            report["pre_existing_project_dir"] = project_dir.is_dir()
            if project_dir.is_dir():
                report["pre_existing_project_dir_note"] = (
                    "已存在；重建工程时会先清掉。若本轮不重建工程而它又是残留，"
                    "S8 读 xmlconfig 可能读到旧配置。")
            # 残留的 `~$*` 锁文件：适配器的 `Single()` 会被它们搞炸
            # （「序列包含一个以上的元素」），但它们**只是簿记文件、不含数据**，
            # 交接文档已明确「可删除」。SolidWorks 异常退出/重启后会留下它们。
            # 既然上面已经确认没有文档打开，这里的锁就是残留的 —— 直接清掉并记录，
            # 而不是拦死让人手工删。
            locks = lock_files_under(run)
            report["lock_files_present"] = [str(p) for p in locks]
            if locks and active is None:
                removed = []
                for path in locks:
                    try:
                        path.unlink()
                        removed.append(path.name)
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"残留锁文件 {path.name} 删不掉：{type(exc).__name__}: {exc}")
                report["lock_files_removed"] = removed
                report["lock_files_note"] = "门禁时发现并清理的残留锁文件（簿记文件，不含数据）"
            elif locks:
                problems.append(f"发现锁文件 {[p.name for p in locks]}，但此刻有文档打开 —— "
                                "可能是别人正在用，不动它")

        manifest = verify_template_manifest(manifest_path, template_dir)
        report["template_manifest"] = manifest
        if not manifest["ok"]:
            problems.extend(manifest["problems"])

        report["problems"] = problems
        report["ok"] = not problems
        if problems:
            raise GateFailure("门禁不通过：\n  " + "\n  ".join(problems))
        return report

    # -- 门禁（打开文档之后）--------------------------------------

    def assert_no_flow_project(self, configuration, *, remove: bool = False,
                               keep_under: Path | None = None) -> dict:
        """模板里残留着悬空的 Flow 工程注册（`assembly_batch_v6\\1` 登记了但目录不存在）。

        它的边界条件引用的是四个封盖的面；一旦几何变了引用就解析不了，
        下一次重建会弹「面<1>@封盖1<1> 未在固体和流体区域之间的边界上」的模态框，
        之后所有 COM 调用全被堵住。

        remove=True 时在**副本**上删掉它（绝不在母版上做）。

        ⚠️ **`keep_under` 不能省。** 不加区分地全删会连**我们自己存进去的工程**一起删掉：
        停在求解前那一次已经把工程保存进装配体了，求解那一次开文档后本该直接激活它，
        删掉之后 S6 只能重建 —— 实测白花 ~20 秒，还要重写 9 个特征。
        判据用**工程目录落在哪**：在本 run 目录下的 = 我们自己存的，保留；
        指向 `assembly_batch_v6\\1` 之类外面的 = 继承来的悬空注册，删掉（那才是要防的）。
        """
        names = [str(n) for n in (configuration.GetProjectNames() or [])]
        report: dict[str, Any] = {"projects_before": names}
        keep: list[str] = []
        if remove and keep_under is not None:
            root = Path(keep_under).resolve()
            for name in names:
                if self._project_directory(configuration, name, root) is not None:
                    keep.append(name)
        if remove:
            removed, failed = [], []
            for name in names:
                if name in keep:
                    continue
                try:
                    (removed if configuration.RemoveProject(name) else failed).append(name)
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{name} ({type(exc).__name__}: {exc})")
            report["removed"] = removed
            report["kept"] = keep
            report["remove_failed"] = failed
            names = [str(n) for n in (configuration.GetProjectNames() or [])]
        report["projects_after"] = names
        if report.get("remove_failed"):
            # 交接文档 §7.2 记录过 RemoveProject 偶发 RPC_E_DISCONNECTED —— 失败即停，不硬闯。
            raise GateFailure(f"删除继承的 Flow 工程失败：{report['remove_failed']}")
        leftover = [n for n in names if n not in keep]
        if leftover:
            raise GateFailure(
                f"副本里仍挂着 Flow 工程 {leftover} —— 必须先清掉，"
                "否则重建时会弹模态框把后续所有 COM 调用堵死")
        report["ok"] = True
        return report

    @staticmethod
    def _project_directory(configuration, name: str, keep_under: Path) -> Path | None:
        """工程的落盘目录 —— **只有落在 `keep_under` 之下才返回它**，否则 None。

        没有直接读目录的 API，只能先 `ActivateProject` 再问 `ProjectFiles`。
        激活失败 / 目录字段为空 / 目录在别处 → 一律当「不是我们的」，交给调用方删掉。
        """
        try:
            project = configuration.ActivateProject(name, False)
        except Exception:  # noqa: BLE001
            return None
        if project is None:
            return None
        try:
            raw = str(getattr(project.ProjectFiles, "ProjectDirectory", "") or "")
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        try:
            directory = Path(raw).resolve()
        except Exception:  # noqa: BLE001
            return None
        return directory if keep_under in directory.parents else None

    # -- 生命周期 ---------------------------------------------------

    def close_all_documents(self, *, limit: int = 30) -> int:
        """关掉全部文档。只关自己打开的 —— 调用方负责保证进来时没有用户的文档。"""
        closed = 0
        for _ in range(limit):
            doc = self.sw.ActiveDoc
            if doc is None:
                break
            try:
                self.sw.CloseDoc(doc.GetTitle)
                closed += 1
            except Exception:  # noqa: BLE001
                break
        return closed

    def unload(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.api.UnloadProductAPI()
        except Exception:  # noqa: BLE001
            pass
        try:
            import pythoncom
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "SwSession":
        return self

    def __exit__(self, *exc) -> None:
        self.unload()


# ---------------------------------------------------------------- 廉价探活

def probe_alive(session: SwSession | None = None, *, timeout_s: float = 8.0,
                require_blank: bool = False) -> tuple[bool, str]:
    """模态对话框会堵住 COM 调用。调用前后各探一次，卡住就能及早发现。

    ⚠️ **必须在子线程里自己 `CoInitialize` 并重新 `GetActiveObject`。**
    COM 是单元线程的：把主线程拿到的对象直接丢给另一个线程调用，必定报
    `尚未调用 CoInitialize`（实测踩过）—— 那会被当成"SolidWorks 无响应"，
    于是**每一次都误判成模态框挡住**，比不检查还糟。

    返回 `(是否活着, 说明)`。超时、异常、非 34.x 都算不活。
    `require_blank=True` 只供 CAD 开始前的环境体检使用；求解过程中当然有打开的文档。

    `session` 参数**不参与实现**（函数自己在子线程里重新 `GetActiveObject`），
    保留只是为了 `guarded()` 的调用签名稳定；批处理这类没有现成 session 的调用方
    可以直接 `probe_alive()`。
    """
    import threading
    result: dict[str, Any] = {"ok": False, "why": "未知"}

    def _call():
        import pythoncom
        import win32com.client as win32
        pythoncom.CoInitialize()
        try:
            sw = win32.GetActiveObject("SldWorks.Application")
            revision = str(_member(sw, "RevisionNumber", default=""))
            result["revision"] = revision
            result["ok"] = revision.startswith(REQUIRED_REVISION_PREFIX)
            result["why"] = "ok" if result["ok"] else f"版本 {revision}"
            if result["ok"] and require_blank:
                unknown = object()
                first = _member(sw, "GetFirstDocument", default=unknown)
                if first is unknown:
                    result["ok"] = False
                    result["why"] = "无法确认 SolidWorks 是否为空白主界面（GetFirstDocument 不可用）"
                elif first is not None:
                    result["ok"] = False
                    result["why"] = "SolidWorks 仍打开文档；先关闭全部装配体/零件再运行 CAD"
        except Exception as exc:  # noqa: BLE001
            result["why"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:  # noqa: BLE001
                pass

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        return False, f"探活在 {timeout_s:.0f} 秒内没返回（COM 调用被堵住）"
    return bool(result["ok"]), str(result.get("why", "未知"))


def _exit_app_via_com() -> str | None:
    """让 SolidWorks 自己退出（`ExitApp`）。成功返回 None，失败返回错误说明。

    ⚠️ **这是个真的会关掉 SolidWorks 的函数 —— 离线测试必须打桩它。**
    没打桩的话，跑一次 `pytest` 就会把用户正在跑的批处理连同 SolidWorks 一起掀掉
    （作者踩过：`test_stop_falls_back_to_taskkill_...` 起初直接调了
    `_stop_solidworks_once`，于是每次跑测试都真的 `ExitApp` 一次）。

    顺序照 `cleanup_failed_flow_projects.py:117-132` 的既有先例。
    """
    try:
        import pythoncom
        import win32com.client as win32
    except ImportError as exc:
        return f"没有 pywin32：{exc}"
    pythoncom.CoInitialize()
    try:
        sw = win32.GetActiveObject("SldWorks.Application")
        _ = sw.ExitApp()
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


def _stop_solidworks_once(before_pids: list[int]) -> dict:
    """干净退出现有实例；`ExitApp` 不生效才用 taskkill 兜底。

    ⚠️ 会真的关掉 SolidWorks。离线测试请打桩 `_exit_app_via_com`。
    """
    result: dict[str, Any] = {"method": None, "ok": False}

    # ---- ① 先试 ExitApp（干净退出，不会留下"文档恢复"引信）
    error = _exit_app_via_com()
    if error is None:
        result["method"] = "ExitApp"
        result["exit_wait"] = _wait_for_no_process(RESTART_EXIT_TIMEOUT_S)
        if result["exit_wait"]["gone"]:
            result["ok"] = True
            return result
    else:
        result["exitapp_error"] = error

    # ---- ② 兜底 taskkill。**杀前复核 PID**（照 cleanup_empty_solidworks_processes 的写法）。
    # 两次列举之间进程可能已退出、PID 可能被复用，对不上就绝不猜。
    result["method"] = "taskkill"
    current = {p.pid for p in list_solidworks_processes()}
    seen = [p for p in before_pids if p not in current]
    if seen:
        result["why"] = f"ExitApp 后 PID 已变化或消失 {seen} —— 不猜、不杀"
        return result
    killed = []
    for pid in before_pids:
        proc = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                              capture_output=True, text=True, timeout=60, errors="replace")
        killed.append({"pid": pid, "returncode": proc.returncode,
                       "output": (proc.stdout or proc.stderr or "").strip()[:200]})
    result["taskkill"] = killed
    result["exit_wait"] = _wait_for_no_process(RESTART_EXIT_TIMEOUT_S)
    result["ok"] = result["exit_wait"]["gone"]
    return result


def _wait_for_no_process(timeout_s: float) -> dict:
    began = time.time()
    while time.time() - began < timeout_s:
        if not list_solidworks_pids():
            return {"gone": True, "seconds": round(time.time() - began, 1)}
        time.sleep(RESTART_POLL_S)
    return {"gone": False, "seconds": round(time.time() - began, 1),
            "remaining": list_solidworks_pids()}


def _launch_solidworks_once(exe: Path, bincfw: Path) -> dict:
    """用 Flow 官方 `RunProduct2` 拉起，再轮询等就绪。

    ⚠️ **必须先 `CoInitialize()`** —— `flow_transfer.connect()` 里的
    `win32.Dispatch(PROGID)` 要求调用线程已初始化 COM，否则直接
    `com_error(-2147221008, '尚未调用 CoInitialize')`。（`launch_sw.py:24` 同款。）

    为什么不是 COM 冷启动：`RunProduct2` 是 Flow 插件 API 以**普通应用方式**启动
    `SLDWORKS.exe`（≈ 双击图标 + 自动挂载 Flow 插件），不是让 COM 服务控制管理器
    把一个桌面重程序当 COM 服务器起 —— 后者才会 `CO_E_SERVER_EXEC_FAILURE`
    + `0x00000003` 崩溃（见模块 docstring 铁律 1）。
    """
    result: dict[str, Any] = {"ok": False}
    if not exe.is_file():
        result["why"] = f"缺 {exe}"
        return result
    if not bincfw.is_dir():
        result["why"] = f"缺 {bincfw}"
        return result

    import pythoncom
    pythoncom.CoInitialize()
    try:
        import flow_transfer as ft
        try:
            api = ft.connect()
        except SystemExit as exc:   # flow_transfer.connect 失败时直接 sys.exit
            result["why"] = f"flow_transfer.connect 退出码 {exc.code}"
            return result
        began = time.time()
        try:
            running = api.RunProduct2(str(exe), str(bincfw))
        except Exception as exc:  # noqa: BLE001
            result["why"] = f"RunProduct2 抛异常：{type(exc).__name__}: {exc}"
            return result
        result["runproduct2_returned"] = running is not None
        # 就绪判据复用 probe_alive：子线程自己 CoInitialize + **每次重新 GetActiveObject**，
        # 所以进程换了以后旧对象失效这件事天然不构成问题。
        alive, why = False, "未开始轮询"
        while time.time() - began < RESTART_LAUNCH_TIMEOUT_S:
            alive, why = probe_alive(timeout_s=8.0)
            if alive:
                break
            time.sleep(RESTART_POLL_S)
        result["probe_alive"] = alive
        result["probe_why"] = why
        result["seconds"] = round(time.time() - began, 1)
        result["ok"] = alive
        return result
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


def restart_solidworks(*, retries: int = 3, exe: Path | None = None,
                       bincfw: Path | None = None) -> dict:
    """把 SolidWorks 停掉再拉起来，回到"空白主界面"。

    ⚠️ **只能在样本边界调用**（求解已结束、文档已关、状态已落盘）。
    调用方负责保证这一点 —— 本函数不做也无法判断"现在是不是在求解中"。

    返回结构化审计：``{ok, attempts: [...], memory_before_mb, memory_after_mb, ...}``。
    **`ok=False` 时调用方必须停批处理**（状态已落盘，`--execute` 可续跑），
    不要拿着半死不活的进程继续跑。

    为什么值得重启（实测，`数据生成/参数范围CAD验证报告_2026-09-22.md:107-116`）：
    SolidWorks 工作集约 **64 MB/例** 线性累积，空载基线 477 MB；16 GB 机器约 150 例就会顶到上限。
    重启的一次性成本约 456 MB + 启动耗时，摊到 50 例上可以忽略。
    """
    report: dict[str, Any] = {
        "ok": False, "attempts": [],
        "memory_before_mb": solidworks_memory_mb(),
        "solver_running_before": solver_running(),
    }

    # ---- 前置安全检查：任一不过就**拒绝动手**，不猜
    if report["solver_running_before"]:
        report["why"] = ("EFDsolver.exe 在跑 —— 拒绝重启。solver_running() 是按镜像名全局匹配的"
                         "（不认 PID/父进程），留一个孤儿求解器会让下一轮 wait_for_result() "
                         "永远等不到「求解器已退出」")
        return report

    before_pids = list_solidworks_pids()
    report["pids_before"] = before_pids
    if len(before_pids) != 1:
        report["why"] = f"有 {len(before_pids)} 个 SLDWORKS.exe {before_pids} —— 不猜该动哪一个"
        return report

    # ---- 解析 SolidWorks 安装位置（探测/配置），拿不到就别折腾进程了
    try:
        exe = Path(exe) if exe is not None else mpaths.solidworks_exe()
        bincfw = Path(bincfw) if bincfw is not None else mpaths.flow_bincfw()
    except RuntimeError as exc:
        report["why"] = f"找不到 SolidWorks / Flow 的安装位置：{exc}"
        return report

    for attempt in range(1, max(1, retries) + 1):
        record: dict[str, Any] = {"attempt": attempt}
        stop = _stop_solidworks_once(before_pids)
        record["stop"] = stop
        if not stop["ok"]:
            record["why"] = "关不掉"
            report["attempts"].append(record)
            continue

        launch = _launch_solidworks_once(exe, bincfw)
        record["launch"] = launch
        if not launch["ok"]:
            record["why"] = "拉不起来 / 拉起来但 COM 连不上"
            report["attempts"].append(record)
            # 下一次重试前，把可能的半死进程清掉，否则下面 len != 1 会直接失败
            _stop_solidworks_once(list_solidworks_pids())
            continue

        after_pids = list_solidworks_pids()
        record["pids_after"] = after_pids
        if len(after_pids) != 1:
            record["why"] = f"重启后有 {len(after_pids)} 个实例 {after_pids}"
            report["attempts"].append(record)
            continue

        report["attempts"].append(record)
        report["memory_after_mb"] = solidworks_memory_mb()
        report["pids_after"] = after_pids
        report["pid_changed"] = after_pids != before_pids
        report["ok"] = True
        return report

    report["memory_after_mb"] = solidworks_memory_mb()
    report["why"] = f"重试 {retries} 次仍未成功"
    return report


def guarded(action, session: SwSession, *, step: str):
    """执行一个可能弹模态的动作：前探活、执行、后探活。

    卡住时抛 ``ModalDialogBlocked`` 并指明步骤 —— 让用户手动关掉对话框后 ``--resume``。
    **绝不重试**可能已经弹出模态的调用。
    """
    alive, why = probe_alive(session)
    if not alive:
        raise ModalDialogBlocked(f"{step}：调用前 SolidWorks 就无响应（{why}）")
    value = action()
    alive, why = probe_alive(session)
    if not alive:
        raise ModalDialogBlocked(
            f"{step}：调用后 SolidWorks 无响应（{why}）—— 很可能弹出了模态对话框。"
            "请手动关掉它，然后 --resume 从本步继续。")
    return value
