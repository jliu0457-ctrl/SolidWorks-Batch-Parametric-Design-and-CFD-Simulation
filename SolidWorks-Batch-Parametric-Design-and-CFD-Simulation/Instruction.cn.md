# 使用说明

本文只说明当前实际使用的流程。历史试验、失败原因和接口探索记录见
[`参数化与Flow自动化-过程交接.md`](参数化与Flow自动化-过程交接.md)，七变量的CAD含义见
[`七变量CAD映射说明.md`](七变量CAD映射说明.md)。

当前主流程是：读取七变量表 → CAD参数化与拓扑门禁 →
建立/重建Flow工程 → 求解 → 逐行写入训练表。

开度固定 **45°**（装配体配合 `D1@角度2`），**不参数化、不由命令行传入** ——
一行的七个变量就是一个样本。

## 1. 最快启动

运行前：

1. 手工启动SolidWorks，停在**空白主界面**。
2. 关闭所有装配体和零件文档，只保留一个SolidWorks实例。
3. 关闭训练结果Excel（`outputs/training_dataset.xlsx`），避免文件被占用。
4. 长时间运行时关闭Windows自动睡眠；显示器可以关闭。

在PowerShell中执行：

```powershell
cd "<项目根>\SolidWorks-Batch-Parametric-Design-and-CFD-Simulation"

# 只读预检：显示输入表、任务数、已完成数和待跑清单
python scripts\Run-Batch.py

# 执行全部未完成任务
python scripts\Run-Batch.py --execute
```

已成功完成的样本会自动跳过，所以相同命令也用于断点续跑。

## 2. 输入变量表

日常批处理默认读取 [`Variables`](Variables/) 下所有 `.xlsx` 文件。表格要求：

| 列 | 内容 |
|---|---|
| 第1列 | `样本序号`，每组唯一 |
| 其余7列 | `c_mm`、`e_mm`、`phi_deg`、`alpha_deg`、`Dmax_mm`、`bm_mm`、`ds_mm` |

- 列按表头名称匹配，顺序可以调整。
- 输入表就是这 8 列，**不要加开度列** —— 开度固定 45°，不是设计变量。
- 多份输入表会合并读取；样本序号重复会直接报错。
- `alpha_deg`是**全锥角**，不是半锥角。
- 当前50组试运行表是 `Variables/随机化变量.xlsx`。
- 3600组拉丁超立方候选集由同级目录 `../data generate/generate_lhs.mjs`生成；范围和生成方法以
  [`../data generate/README.md`](../data%20generate/README.md)为准。

只读取指定表：

```powershell
python scripts\Run-Batch.py --xlsx "Variables\随机化变量.xlsx"
python scripts\Run-Batch.py --xlsx "Variables\随机化变量.xlsx" --execute
```

## 3. 命令行用法

### 3.1 预检和全量运行

```powershell
# 只读预检，不创建CAD副本、不连接SolidWorks
python scripts\Run-Batch.py

# 运行全部未完成任务
python scripts\Run-Batch.py --execute
```

### 3.2 指定设计点

`--only`按**设计点编号**选择：

```powershell
# 跑第1～10组；已经成功的会跳过
python scripts\Run-Batch.py --execute --only 1,2,3,4,5,6,7,8,9,10

# 只补跑第49和50组
python scripts\Run-Batch.py --execute --only 49,50
```

### 3.3 限制任务数量

`--limit`限制的是**样本数**，一个设计点就是一个样本：

```powershell
# 最多运行接下来的30个待跑样本
python scripts\Run-Batch.py --execute --limit 30
```

### 3.4 指定输入表、超时和保留副本

```powershell
# 只读取指定Excel
python scripts\Run-Batch.py --execute `
  --xlsx "Variables\随机化变量.xlsx"

# 把单次Flow求解超时改为40分钟
python scripts\Run-Batch.py --execute --solve-timeout-min 40

# 保留完整CAD和Flow目录，用于排查
python scripts\Run-Batch.py --execute --only 7 --keep-run-dirs
```

### 3.5 失败策略和强制重跑

```powershell
# 连续3次失败就停；SolidWorks失联仍会立即停
python scripts\Run-Batch.py --execute --max-consecutive-failures 3

# 0表示不按连续失败次数停，只按SolidWorks探活结果停
python scripts\Run-Batch.py --execute --max-consecutive-failures 0

# 连已完成任务也重跑；日常不要使用
python scripts\Run-Batch.py --execute --redo --only 7
```

### 3.6 定期重启 SolidWorks（防内存累积）

**默认每 50 个样本自动重启一次 SolidWorks**，不需要手工干预。

```powershell
# 用默认值（每 50 个样本一次）
python scripts\Run-Batch.py --execute

# 改间隔
python scripts\Run-Batch.py --execute --restart-every 30

# 关掉这个机制
python scripts\Run-Batch.py --execute --restart-every 0
```

为什么必须重启：SolidWorks 工作集随样本**线性累积**（实测基线 477 MB、**约 64 MB/例**），
16 GB 机器约 150 例就会顶到上限。取 50 是留了 3 倍余量。

重启走的是"干净退出 → Flow 官方 `RunProduct2` 重新拉起 → 等就绪"，**实测约 18 秒**一次。
**成功和失败都算数**：内存累积来自反复开关文档，与样本成败无关。
只有批处理本来就要停的时候（连续失败到上限、或探活发现 SolidWorks 已失联）才不重启。

重启失败会**重试 3 次**；仍失败就停批处理并打印诊断，**不会安静地用旧进程继续跑**。
处理完再 `--execute` 续跑即可，已完成的样本会自动跳过。

⚠️ **首次启动仍然要手工**（铁律：不冷启动）。这条只负责"跑起来之后的定期换进程"。

查看完整参数：

```powershell
python scripts\Run-Batch.py --help
```

## 4. 断点续跑

- 状态文件：`working/_batch/batch_state.json`。
- `done`自动跳过；`failed`下次仍会重试。
- 相同命令可以反复执行，不需要手工修改状态文件。
- 异常关机后，重新打开SolidWorks空白主界面，再执行原命令即可。
- 没有写成`done`的中断任务会从头重建隔离副本并重新运行。
- 批处理默认连续失败10次后停止；SolidWorks探活失败会立即停止。
- **定期重启不影响续跑** —— 状态每次样本后即时落盘，重启前后都一样。

## 5. 输出与报告

全部写入唯一一张表 `outputs/training_dataset.xlsx`，共17列：

```text
样本序号 + 7个设计变量 + 7个Flow目标 + ΔP + Cv
```

- 7个目标值取Flow目标历史的时间平均值。
- `ΔP = 入口静压 − 出口静压`，单位Pa。
- `Cv`由入口体积流量、压差和密度计算。
- 七个输入相同且结果也相同时不会重复追加。
- 七个输入相同但结果不同时拒绝写入，避免静默污染训练集。
- 物理门禁或训练行契约失败时，不会向主训练表追加数据。

运行记录：

| 内容 | 位置 |
|---|---|
| 成功/失败状态 | `working/_batch/batch_state.json` |
| 成功样本报告 | `working/_sample_reports/<run>.flow_sample.json` |
| 成功样本单行CSV | `working/_sample_reports/<run>.training_sample.csv` |
| 失败证据 | `working/_batch/failures/` |
| CAD参数化报告 | `_analysis/e2e_<run>.json` |

默认情况下，成功后会删除CAD/Flow临时副本，只保留小型报告。训练结果Excel必须在运行时关闭；
Excel被占用时，单样本本地CSV可能已写出，但批处理不会把该任务记为成功。

## 6. 常见失败怎么处理

| 现象 | 含义与处理 |
|---|---|
| `WinError 32` / `PermissionError` | 旧run目录中的SLDASM/SLDPRT仍被占用。停止批处理，关闭所有SolidWorks文档；最好重启SolidWorks并停在空白页后再跑。 |
| `A same-named document is already open` / `RefuseSameNamedOpenDocuments` | SolidWorks中还打开着同名历史副本。关闭全部文档，不要在批处理期间手动打开装配体。 |
| `CO_E_SERVER_EXEC_FAILURE` | SolidWorks未启动、正在启动或COM实例异常。手工重启SolidWorks空白页。 |
| `Topology gate failed` | 真实几何失败。参数化后面数或曲面类型与母版不一致；该设计点不能进入训练集。同一设计点反复重跑没有意义。 |
| `NeedsSeatFaces` / 面类型不符 | Flow目标引用面在该几何下发生重排，属于门禁失败；不要用允许缺目标的降级参数生成训练数据。 |
| `blocked_by_modal` / 界面卡住 | SolidWorks有模态弹窗。先停止批处理并人工关闭弹窗，再从空白页续跑。 |
| 连续多项在0～2秒内失败 | 通常不是多组参数同时坏，而是SolidWorks状态、文件锁或同名文档问题；立即停止，不要等十次上限。 |
| 终端只显示`template -> ... design -> ...` | CAD程序在生成正式e2e报告前退出。优先检查SolidWorks是否为空白页和同名文件是否仍打开。 |

当前批处理在新报告未生成时可能读到同名任务以前留下的旧e2e报告。因此，0秒失败时应以
Windows/Application日志和本轮是否生成新报告为准，不要仅凭旧报告判断本轮又做完了拓扑检查。

## 7. 当前数据质量边界

当前自动化链路按**固定 45° 开度**实机完成CAD参数化、Flow求解与训练表写入。
45°是人工参照工程 `1/` 用的角度，也是这条链路唯一有人工对照的工况
（人工参考约297次迭代、travel≈7.425）。

开度扫掠试验（15°/45°/60°/75°）已于 2026-09-23 做完并回退到单开度，结论留档在
[`../data generate/开度扫掠方案.md`](../data%20generate/开度扫掠方案.md)。其中15°在入口固定3 m/s时
为维持同一流量会产生约620 MPa的极高压差，不适合作为当前边界条件下的训练工况。

当前还有以下精度限制：

1. 实际求解通常停在约160次迭代、travel≈4；人工参考曾运行约297次、travel≈7.425。
2. 希望写入的0.5%目标收敛条件会被Flow内存状态覆盖，报告中可能出现
   `criteria_verification.ok=false`。
3. 压力、流量和Cv在已检查样本中趋势合理，但力矩可能有更明显的末段振荡。
4. 当前结果尚未完成系统性的网格独立性和加长求解验证。

因此现有数据适合批量筛选和模型流程验证。如果最终模型高度依赖力矩或绝对精度，应先抽样做
更长求解和网格独立性对照，再决定是否扩大到3600组。

## 8. 关键目录和不可修改项

| 路径 | 用途 | 规则 |
|---|---|---|
| `working/assembly_batch_v6/` | 当前CAD母版 | **只读，不修改** |
| `config/cad_template_manifest_v6.json` | 母版哈希与结构门禁 | 仅在明确更换母版时更新 |
| `config/flow_physics_reference.json` | Flow边界、目标、网格及人工参考 | 仅在人工参考工程确认变更后更新 |
| `Variables/*.xlsx` | 七变量输入 | 批处理只读 |
| `outputs/*.xlsx` | 训练结果 | 运行时必须关闭，不要手工混入数据 |

主代码：

| 文件 | 职责 |
|---|---|
| `scripts/Run-Batch.py` | 批量展开、状态、失败隔离、清理 |
| `scripts/Run-OneDesign.exe` | CAD参数化、拓扑门禁 |
| `scripts/SevenVariableAdapter.cs` | CAD参数化源码 |
| `scripts/Run-FlowSample.py` | 单样本Flow全链路与训练行写入 |
| `scripts/flow_project.py` | Flow工程、特征、边界和目标接口 |
| `scripts/flow_geometry.py` | 训练表字段、Cv及纯几何逻辑 |
| `scripts/flow_session.py` | 附加现有SolidWorks实例及进程门禁 |

根目录的`flow_transfer.py`是Flow API连接依赖，也不能遗漏。

## 9. 单样本排查

日常不要手工拆分流程；仅在排查某一设计JSON时使用。设计JSON必须包含完整的七个数值字段。

```powershell
cd "<项目根>\SolidWorks-Batch-Parametric-Design-and-CFD-Simulation"

# 1. 生成隔离CAD副本
scripts\Run-OneDesign.exe . debug_case config\design_baseline_v6.json

# 2. 建Flow工程并停在求解前
python scripts\Run-FlowSample.py debug_case `
  --design config\design_baseline_v6.json --execute

# 3. 求解并写训练表
python scripts\Run-FlowSample.py debug_case `
  --design config\design_baseline_v6.json --execute --resume --yes-solve `
  --timeout-min 25 --dataset-xlsx outputs\training_dataset.xlsx
```

单样本失败时先读`working/seven_variable_trials/debug_case/flow_sample.json`。不要在原因未知时
反复复制新目录。

新建 Flow 工程时，程序先用基础 `assets/internal_water.fwp` 建工程骨架，随后按**当前四个封盖**
计算并写入计算域，读回确认后才添加边界条件和目标。不能依赖基础模板的占位计算域：
迁移机 SP0 曾因此报“封盖面已超出计算域”。计算域写入详情在报告的
`stages.project.computational_domain`（`source`、`before`、`target`、`after`、`mismatch`）；
仅排查时可用 `--domain x_min,x_max,y_min,y_max,z_min,z_max` 指定六个米数，批量运行不要使用。
这一步解决计算域迁移问题，**不代表不同电脑的网格与人工工程完全一致**，仍需单例核对网格与目标。

## 10. 范围验证与数据生成

修改七变量范围后，先进行CAD-only验证，不要直接跑Flow：

```powershell
# 只看测试计划
python scripts\Validate-DesignRanges.py

# 实际执行；需要SolidWorks空白页
python scripts\Validate-DesignRanges.py --execute
```

范围验证只检查参数化、重建、实体Dmax和逐面拓扑，不创建Flow项目。当前范围结论和失败点见
[`../data generate/参数范围CAD验证报告_2026-09-22.md`](../data%20generate/参数范围CAD验证报告_2026-09-22.md)。

生成拉丁超立方候选集：

```powershell
cd "<项目根>\data generate"
node generate_lhs.mjs
```

生成后选择需要试跑的行复制到`SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/Variables/`，不要直接把3600组全部投入未验证的
长期求解。

## 11. 维护与测试

离线测试不需要SolidWorks：

```powershell
cd "<项目根>\SolidWorks-Batch-Parametric-Design-and-CFD-Simulation"
python -m pytest -q
```

本次迁移修改后离线回归为 **338 passed，39 subtests passed**（不含 SolidWorks 真机运行）。

只有修改了`Run-OneDesign.cs`或`SevenVariableAdapter.cs`才需要重新编译。CAD任务运行期间
不要替换EXE：

```powershell
cd "<项目根>\SolidWorks-Batch-Parametric-Design-and-CFD-Simulation\scripts"

& "C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe" `
  /nologo /codepage:65001 /target:exe `
  /r:"SolidWorks.Interop.sldworks.dll" /r:System.Web.Extensions.dll `
  /out:"Run-OneDesign.new.exe" "Run-OneDesign.cs" "SevenVariableAdapter.cs"

Move-Item -LiteralPath "Run-OneDesign.new.exe" -Destination "Run-OneDesign.exe" -Force
```

清理历史临时副本：

```powershell
# 先只看清单
python scripts\Clean-RunDirs.py

# 确认后执行
python scripts\Clean-RunDirs.py --apply
```

清理前必须关闭SolidWorks中的相关文档。

## 12. 搬到别的电脑上跑：先对齐环境，再碰 CAD

“直接用 `python`”的前提是：当前终端的 `python` 必须指向**已配置的 64 位解释器**。
`python` 这个命令本身不会自动安装依赖，也不会消除 SolidWorks 版本、授权和桌面会话差异。
开发机基线：Python 3.12.3、pywin32 306、openpyxl 3.1.4、SolidWorks 2026 SP3.2。
这是基线而非上限：体检接受 Python 3.9+、pywin32 306+、openpyxl 3.1.4+。
Python 3.14 与 pywin32 的发布号（如 306）是两套不同的版本号；版本检查通过
也不替代在目标机运行一例 CAD/Flow 的实际兼容性验证。
目标机若仍用 Python 3.11.7，也可先做诊断；为减少差异，推荐使用独立的 3.12 环境。

**交付方式**：复制整个 `流体仿真2\` 文件夹，不是单独 `git clone`。
`working/assembly_batch_v6` CAD 母版、`assets/internal_water.fwp`、
`scripts/Run-OneDesign.exe`、Interop DLL 等运行资产可能被 Git 忽略，缺任一项都跑不通。
外层盘符和用户名可以不同，但**不要改名**内层的 `SolidWorks-Batch-Parametric-Design-and-CFD-Simulation` 目录：CAD 适配器仍以这个
目录名作为作用域安全检查。
不要把开发机的 `.venv`、Conda 环境或 pywin32 缓存一起复制；在目标机**先建环境**。

在目标机 PowerShell 中，以下命令均从 `SolidWorks-Batch-Parametric-Design-and-CFD-Simulation` 目录运行：

```powershell
cd "<项目根>\SolidWorks-Batch-Parametric-Design-and-CFD-Simulation"
python --version
python -c "import sys,struct; print(sys.executable); print(struct.calcsize('P')*8, 'bit')"
python -m pip --version
```

若机器已有 Conda，推荐新建独立环境，不要用杂装依赖的 `base`。**但若现有环境
已有 pywin32 306 或更高版本、openpyxl 3.1.5，可先运行 `python scripts\Doctor.py`；Python
依赖检查通过就不用为了基线版本再运行 pip 降级。`pywin32>=306`、`openpyxl>=3.1.4` 均允许通过版本检查，
但新版本仍建议先做一次读表和单例验证。** 新建环境时：

```powershell
conda create -n valve-flow python=3.12
conda activate valve-flow
python -m pip install -r ..\requirements.txt
python scripts\Doctor.py
```

`requirements.txt` 刻意只保留 ASCII 字符和两行依赖：旧版 Windows/Conda pip 可能按
GBK 读取该文件；若文件里有 UTF-8 中文注释，会在解析依赖前抛
`UnicodeDecodeError: 'gbk' codec can't decode ...`。遇到此报错，先更新这份文件，
或直接运行 `python -m pip install "pywin32>=306" "openpyxl>=3.1.4"`，不要把它当成
依赖版本冲突。`pip install` 若报多次 `Retrying`/`FAIL`，先看 `Doctor.py` 的 Python 环境部分：
依赖都通过就暂时跳过安装；若缺包，再根据安装输出末尾的实际错误处理网络或权限问题。
不要仅凭重复的重试行判断为代码不兼容。

若当前 PowerShell 不认识 `conda activate`，先按 Conda 的提示初始化 PowerShell，
重新打开终端后再执行上面的命令；不要退回装了其他包的 `base` 环境。

不用 Conda 时，先安装 64 位 Python，再用标准 `venv`；在 **cmd.exe** 中执行激活
（PowerShell 的脚本执行策略可能拦截 `Activate.ps1`）：

```bat
cd /d "<项目根>\SolidWorks-Batch-Parametric-Design-and-CFD-Simulation"
python -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r ..\requirements.txt
python scripts\Doctor.py
```

每次开新终端都先激活同一环境，再敲 `python`。`Doctor.py` 会检查解释器位数、
pywin32/openpyxl 的兼容版本、母版与清单的文件哈希、EXE/JSON 字段数及本机
SolidWorks/Flow 路径。
需要离线测试时另装 `python -m pip install -r ..\requirements-dev.txt`。
**不要用 `py -3.12`**：目标机可能没有 Windows Python 启动器。

若安装路径未被注册表识别，在 `config/machine_paths.json` 写目标机路径（不要把该配置
从另一台机器直接照搬）：

```json
{
  "solidworks_exe": "D:\\SW2026\\SOLIDWORKS\\SLDWORKS.exe",
  "bincfw": "D:\\SW2026\\SOLIDWORKS Flow Simulation\\binCFW"
}
```

接着**手工**打开唯一一个 SolidWorks 2026 实例，关闭其中全部文档，保持空白主界面；
Flow Simulation 必须已安装且可获授权。先做无样本体检，再做 CAD-only 探针：

```powershell
python scripts\Doctor.py --require-sw
python scripts\Probe-Cad.py
```

探针只运行七变量 CAD 参数化和拓扑门禁，不创建 Flow 工程、不写批处理状态或训练表。
它会即时显示 CAD 输出，卡住时最多等 600 秒；诊断副本和报告会保留在
`working/seven_variable_trials/port_probe_*` 与 `_analysis/e2e_port_probe_*.json`。
**只有探针报告 `completed=True` 且 `physical_mapping_verified=True`，才试批处理一例**：

```powershell
python scripts\Run-Batch.py --xlsx "Variables\随机化变量.xlsx"
python scripts\Run-Batch.py --execute --limit 1 --restart-every 0 --keep-run-dirs
```

确认这例的 CAD、Flow 与训练表都正常，再用 `python scripts\Run-Batch.py --execute`
续跑。批处理入口现在也会先运行 `Doctor.py --require-sw`（含“无打开文档”检查）；
体检失败默认只警告，**仍会尝试运行**，实际 CAD/Flow 错误照常记录。若需要体检失败时
在创建样本前停止，显式加 `--strict-preflight`；CAD-only 探针也支持这个开关。
长跑默认每 50 例重启一次 SolidWorks，迁移机应先用少量样本验证重启路径；
必要时暂用 `--restart-every 0`，不要直接上全量。`--fresh` 用于**换数据集**，
不是一般重试；只读预检不会再归档旧进度。

| 必要条件 | 备注 |
|---|---|
| 64 位 Windows、64 位 Python 3.9+ | 推荐与开发机一致的 3.12.3；以 `python -c` 输出为准 |
| SolidWorks 2026 + Flow Simulation | 同一交互式登录桌面、一个实例、空白主界面、有效授权 |
| 完整项目资产 | CAD 母版、Flow 模板、EXE、Interop DLL 必须随项目一起复制 |
| 同一 Python 环境 | `python -m pip` 与 `python scripts\...` 必须是同一个解释器 |

### ⚠️ Service Pack 也要对得上

门禁**只查大版本**（`34.` = 2026），**不查 Service Pack**。但拓扑门禁比的是
**每个零件有几个面**（蝶板 55 面、阀体 515 面…），那些数是在 **SP3.2** 上抓的。
SolidWorks 换 SP 时偶尔会改变面的切分方式，**面数一变门禁就判「拓扑不一致」**。
Flow 的计算域现在按本次封盖显式设置，已不依赖基础 `.fwp` 的默认占位范围；
这不替代跨 SP 的 CAD 拓扑和仿真结果单例验收。

版本号编码是 `<大版本>.<SP>.<hotfix>`：

```
34.3.2  =  SOLIDWORKS 2026 SP3.2   ← 本机基线，母版就是在它上面定版的
34.0.0  =  SOLIDWORKS 2026 SP0     ← 门禁会放行，但面数可能对不上
```

**新机器上先自查**（SolidWorks 要开着、停在空白主界面）：

```powershell
python scripts\Probe-SwRestart.py
```

看这几行：

```
SolidWorks 版本 : 34.3.2
基线（母版定版）: 34.3.2   ← SP3.2
  ✓ SP 与基线一致
```

不是 `34.3` 开头就会给出警告。**SP 不一致时不保证能跑** —— 先跑
`Probe-Cad.py` 的基准案例，再用 `--limit 1` 跑完整链路；这仍不保证其他变量点全通过。

### 必须是有人在的交互式桌面

SolidWorks 是图形程序，它的 COM 入口只在**当前登录会话**里可见。脚本必须和它
在同一个会话里，否则报 `MK_E_UNAVAILABLE`。

| 做法 | 结果 |
|---|---|
| 正常登录 → 开 SolidWorks 空白页 → 同一桌面开终端跑脚本 | ✅ **就这么用** |
| 计划任务勾「不管用户是否登录都运行」/ 做成 Windows 服务 | ❌ 跑在 session 0，永远看不到 SolidWorks |
| **注销**（Log off） | ❌ 会话销毁 |
| 锁屏 / 屏保 | ✅ 不影响 |
| RDP 连上去操作后**断开**（不是注销） | ⚠️ 通常没事，但别人用同账号登录或会话被重置就会断 |
| Windows 自动睡眠 | ❌ 会中断（见 §1 第 4 条） |

**照本机现在的用法来就行** —— 别改成计划任务/服务，别注销。

其余一切（变量表、训练表、状态文件、临时副本）都在项目文件夹内，跟着走。
