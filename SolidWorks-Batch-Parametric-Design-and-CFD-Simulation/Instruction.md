# Batch pipeline: English quick start

This guide covers the operational path for the project's triple-eccentric butterfly valve. The [Chinese manual](Instruction.cn.md) contains extended diagnostics, experiments, and historical notes.

> **Directory-name compatibility:** The CAD source now checks for `SolidWorks-Batch-Parametric-Design-and-CFD-Simulation`. Rebuild the local `Run-OneDesign.exe` from the updated C# sources before running CAD; an older executable still contains the former directory-name check.

## Requirements and local assets

- 64-bit Windows with SolidWorks 2026 and a licensed Flow Simulation installation.
- A 64-bit Python environment with `pywin32>=306` and `openpyxl>=3.1.4`. Python 3.12 is the development baseline; newer compatible dependency versions are not rejected solely for being newer.
- The matching local CAD master assembly under `working/assembly_batch_v6/` and the project's Flow reference assets. These are **not** distributed by Git.
- The locally built `scripts/Run-OneDesign.exe` and required SolidWorks/Flow Interop DLLs. Generated binaries are **not** distributed by Git.
- One interactive SolidWorks instance, started manually at an empty main window. Close all assembly and part documents before running.

Do not copy a `.venv` or Conda environment from another computer. Create the environment on the target computer, then check that `python` and `python -m pip` refer to the same interpreter.

## Install and check

Run these commands from the batch-pipeline folder (the folder containing `scripts/` and `Variables/`):

```powershell
python --version
python -c "import sys,struct; print(sys.executable); print(struct.calcsize('P')*8, 'bit')"
python -m pip install -r ..\requirements.txt
python scripts\Doctor.py
```

`Doctor.py` checks Python dependencies and required local assets. Its version checks accept pywin32 306 or newer and openpyxl 3.1.4 or newer, but a successful check is not a substitute for a real single-case CAD/Flow validation. If SolidWorks is open at the blank main window, use `python scripts\Doctor.py --require-sw` for the additional session check.

## Input workbook

Place one or more `.xlsx` files in `Variables/`, or pass a specific workbook with `--xlsx`. The first row contains headers. Each design has a unique `样本序号` and the seven exact input names below; input columns are matched by **name**, not position.

```text
样本序号, c_mm, e_mm, phi_deg, alpha_deg, Dmax_mm, bm_mm, ds_mm
```

`alpha_deg` is the **full** cone angle. Do not add an opening-angle column to this version of the input contract: the current batch runner uses a fixed 45° assembly opening. Candidate designs from [LHS generation](../data%20generate/README.md) may still fail the CAD topology gate.

## Preview, validate one case, then batch

```powershell
python scripts\Run-Batch.py                         # Read-only preview
python scripts\Run-Batch.py --execute --only 1      # One end-to-end case
python scripts\Run-Batch.py --execute               # Remaining designs
```

Useful options:

```powershell
python scripts\Run-Batch.py --execute --limit 10
python scripts\Run-Batch.py --execute --only 3,7,12
python scripts\Run-Batch.py --xlsx "Variables\designs.xlsx" --execute
python scripts\Run-Batch.py --execute --keep-run-dirs
python scripts\Run-Batch.py --execute --restart-every 30
python scripts\Run-Batch.py --execute --strict-preflight
```

By default the runner restarts SolidWorks after every 50 attempted samples to limit memory accumulation. The initial start is still manual. A normal rerun skips completed cases and retries failed ones; use `--redo` only when you intentionally want to rerun completed work. Run `python scripts\Run-Batch.py --help` for all options.

## Results and limits

The current runner writes `outputs/training_dataset.xlsx`. Batch state and failure evidence are kept under `working/_batch/`; per-case reports are stored under runtime/report directories. Close the output workbook in Excel before a run to avoid file-lock errors. A failed design must not be appended to the training table.

Flow setup is reconstructed through API-accessible settings; it is not a perfect copy of every hidden reference-study setting. Before a large campaign—especially on a different SolidWorks Service Pack—compare one reconstructed study, its mesh, and its solved results against the local reference. The current data path is useful for screening and workflow validation; stronger absolute-accuracy claims need mesh-independence and longer-solve checks.
