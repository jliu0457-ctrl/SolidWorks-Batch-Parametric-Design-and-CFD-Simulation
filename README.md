# SolidWorks Batch Parametric Design & CFD Simulation

Batch design-variable sampling, SolidWorks parametric modeling, and Flow Simulation for my **triple-eccentric butterfly valve** project.

```text
Design samples → Template inspection → Parametric copies → Flow setup & validation → Solver → Results
```

## Scope and model dependencies

This repository contains automation source code and project-specific configuration, **not** the valve parts, assembly, Flow reference project, or simulation dataset. A clone cannot run the full pipeline until the matching local model assets and a licensed SolidWorks installation with Flow Simulation are supplied.

Some code is intentionally tied to this valve model: input column names, part/feature/dimension/mate names, parameter-write rules, geometry checks, lid and opening-face references, Flow boundary conditions, goals, and output definitions. For example, the opening mate is named `D1@角度2`. Replacing file paths alone is **not** enough to use another assembly; its mapping and physical setup must be inspected and validated again.

## 1. Generate design samples

[`data generate/generate_lhs.mjs`](data%20generate/generate_lhs.mjs) uses Latin hypercube sampling (LHS) to generate design points within the bounds defined in the script. Sample count and seed are configurable. Check the project-specific variables and bounds before generating an Excel input table.

```powershell
cd "data generate"
node generate_lhs.mjs --samples 100 --seed 20260921 --output "outputs\designs.xlsx"
```

The generator uses `@oai/artifact-tool` to write Excel files. Sampling proposes candidate designs; CAD validation determines whether a candidate rebuilds successfully. See the [English data-generation notes](data%20generate/README.md) for the current bounds and caveats.

## 2. Inspect and parameterize the assembly

The main pipeline is in [`SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/`](SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/). CAD probes enumerate features, dimensions, and equations exposed by the SolidWorks API. The batch runner reads input tables from `Variables/`, makes an isolated copy for each design, writes dimensions, rebuilds the model, and checks geometry before CFD.

The included mappings are adapters for **this valve assembly**, not a universal mapping between arbitrary Excel columns and SolidWorks models. Keep the original template read-only; generated copies belong in `working/` and should not be committed.

## 3. Rebuild and run Flow Simulation

Flow probes and API code read settings that are accessible from the project's reference study, then create, configure, and check a new Flow study on each parametric copy. The pipeline handles openings, computational domain, boundary conditions, goals, mesh, solve, and result extraction, with diagnostic reports for each stage.

This is **not** a byte-for-byte copy of the reference study. Some Flow settings are not fully exposed through the public API. Before a long batch or a run on another computer, compare the reconstructed study, mesh, and one solved result with the reference. The base `.fwp` is used to create a study skeleton and is supplied as a local runtime asset, not distributed in this repository.

## 4. Run a batch and export results

Supply the matching valve template, input table, Flow reference assets, and licensed software. Follow the [English quick-start](SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/Instruction.md) or the [detailed Chinese manual](SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/Instruction.cn.md) for setup. Before executing a batch, open **one** SolidWorks instance at its empty main window and close Excel workbooks that may lock the output files.

```powershell
cd "SolidWorks-Batch-Parametric-Design-and-CFD-Simulation"
python -m pip install -r ..\requirements.txt
python scripts\Doctor.py                         # Environment and asset checks
python scripts\Run-Batch.py                       # Read-only preview
python scripts\Run-Batch.py --execute --only 1    # Validate one case first
python scripts\Run-Batch.py --execute             # Continue the batch
```

The runner supports selecting an input table, limiting a run, recording failures, and resuming completed batches. Results are written under `outputs/`; state and diagnostic evidence are kept under runtime directories such as `working/`. Only cases that pass the geometry, Flow, and result checks should enter the training dataset.

> CAD files, `.fwp` files, generated binaries, input spreadsheets, and solver results do not come with a GitHub clone. The code retains assumptions about this particular valve; adapting it to another product requires fresh mapping and physical validation.

---

# 中文说明

本项目面向我自己的**三偏心蝶阀**模型，实现设计变量采样、SolidWorks 参数化建模和 Flow Simulation 批量求解。

```text
设计样本 → 母版探查 → 参数化副本 → Flow 工程重建与校验 → 求解 → 结果表
```

## 适用范围与模型依赖

仓库保存自动化源码和与本项目模型配套的配置，**不提供**蝶阀零件图、装配图、Flow 参考工程或仿真数据。完整流程需要对应的本地模型资产，以及可用的 SolidWorks 和 Flow Simulation。

部分代码有意依赖当前蝶阀模型，包括输入列名、零件/特征/尺寸/配合名称、参数写入规则、几何门禁、封盖与开口面的引用、Flow 边界条件、目标和输出定义。例如开度配合名为 `D1@角度2`。换用其他装配体时，**仅修改路径不够**，还需重新建立并验证尺寸映射与仿真配置。

## 1. 生成设计样本

[`data generate/generate_lhs.mjs`](data%20generate/generate_lhs.mjs) 用拉丁超立方采样在脚本定义的范围内生成设计点。样本数和随机种子可配置；生成 Excel 输入表前，应核对本项目的变量定义和上下界。

```powershell
cd "data generate"
node generate_lhs.mjs --samples 100 --seed 20260921 --output "outputs\designs.xlsx"
```

生成器使用 `@oai/artifact-tool` 写入 Excel。采样只产生候选设计点，能否成功重建仍由后续 CAD 校验决定。

## 2. 探查并参数化装配体

主流程位于 [`SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/`](SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/)。CAD 探针枚举 SolidWorks API 可访问的特征、尺寸和方程。批处理读取 `Variables/` 中的输入表，为各设计点建立独立副本、写入尺寸、重建模型，并在 CFD 前检查几何。

现有映射是**本蝶阀装配体的适配代码**，不能直接用于任意 Excel 列和任意模型。原始母版应保持只读，生成的副本放在 `working/`，不提交到 Git。

## 3. 重建并运行 Flow Simulation

Flow 探针和 API 代码读取参考工程中可访问的设置，为参数化副本创建、配置和校验新的 Flow 工程。流程涵盖开口、计算域、边界条件、目标、网格、求解和结果提取，并保留各阶段的诊断报告。

这**不是**对参考工程的逐字节复制：部分 Flow 设置无法通过公开 API 完整访问。长批次或跨电脑运行前，应对照参考工程检查重建后的设置、网格和单例结果。基础 `.fwp` 用于创建工程骨架，属于本地运行资产，不随仓库分发。

## 4. 批量运行并导出结果

准备好匹配的蝶阀母版、输入表、Flow 参考资产和软件环境，并按项目的 [`Instruction.cn.md`](SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/Instruction.cn.md) 配置。运行前只打开**一个** SolidWorks 实例，停在空白主界面，并关闭可能占用结果表的 Excel。

```powershell
cd "SolidWorks-Batch-Parametric-Design-and-CFD-Simulation"
python -m pip install -r ..\requirements.txt
python scripts\Doctor.py                         # 环境与资产检查
python scripts\Run-Batch.py                       # 只读预检
python scripts\Run-Batch.py --execute --only 1    # 先验证一例
python scripts\Run-Batch.py --execute             # 继续批处理
```

批处理支持指定输入表、限制本轮样本数、记录失败和断点续跑。结果写入 `outputs/`，状态与诊断证据保存在 `working/` 等运行目录。只有通过几何、Flow 和结果校验的样本才应进入训练表。

> GitHub 克隆不包含 CAD 文件、`.fwp`、编译产物、输入表或求解结果。代码保留了对本蝶阀模型的假设；用于其他产品时必须重新验证映射和物理设置。
