# Variables — where the input workbooks live

**The runner only reads tables from this directory; it never writes data here.** Tables are produced by
[`../../data generate/`](../../data%20generate/), or supplied directly by someone else.

Roughly 50 rows is a good size for a trial run. The full 3600-candidate set lives in `data generate/`;
point at it explicitly with `--xlsx` when you actually want to queue all of it, so a full-scale run never
starts by accident.

## Workbook format

- **Row 1 is the header.**
- **Column 1 is `样本序号`** — the unique ID of each sample.
- The remaining columns are the seven design variables, and **the header names must match character for character**:
  `c_mm`, `e_mm`, `phi_deg`, `alpha_deg`, `Dmax_mm`, `bm_mm`, `ds_mm`

**Do not add an opening-angle column.** The opening is fixed at 45° and is not a design variable;
these eight columns are the whole contract.

Columns are **matched by header name, so the order is free.** Example:

| 样本序号 | c_mm | e_mm | phi_deg | alpha_deg | Dmax_mm | bm_mm | ds_mm |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 32.1949 | 3.6221 | 6.2944 | 36.0531 | 188.0258 | 7.3574 | 45.2942 |

⚠️ **`alpha_deg` is the full cone angle**: a physical half-angle of 10°–20° must be written as
`alpha_deg = 20–40`, not 10–20.

## Other conventions

- You may drop in **several** `.xlsx` files; they are merged automatically. **Duplicate IDs are an error**
  (the same ID appearing in two tables).
- You may supply **only part** of the set — two rows is fine for a smoke test.
- Fully blank rows are skipped. **A row with data but no ID, or a blank cell, is a hard error** — it will
  not be skipped silently.
- IDs are written through to the first column of the training table unchanged.

## Running

```bash
cd ..                                  # into SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/
python scripts/Run-Batch.py            # preview only (read-only)
python scripts/Run-Batch.py --execute  # actually run
```

Everything lands in the single `outputs/training_dataset.xlsx` (17 columns = sample ID + the seven design
variables + the seven Flow goals + ΔP + Cv).

The full flow (generate data → drop it here → run) is in [../Instruction.md](../Instruction.md)
under *Preview, validate one case, then batch*.

> `*.xlsx` files in this directory **never enter git** — tables are data, not code.
