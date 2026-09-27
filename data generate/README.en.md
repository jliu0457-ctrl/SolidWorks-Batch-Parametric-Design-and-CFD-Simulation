# Design-data generation

This directory generates candidate design points for the triple-eccentric butterfly valve project. It does **not** contain the CAD master model or CFD results.

## Directory layout

- `configs/`: design ranges and sampling configuration.
- `outputs/`: generated Excel workbooks (not committed).
- `runs/` and `logs/`: runtime files (not committed).
- [`generate_lhs.mjs`](generate_lhs.mjs): reproducible Latin hypercube sampling (LHS) generator.

## Current sampling bounds

These bounds were tested against the project's CAD topology gate. They are **not** a guarantee that every combination inside the rectangular range is geometrically feasible.

| Input | Meaning | Lower | Upper | Unit |
|---|---|---:|---:|---|
| `c_mm` | Axial eccentricity | 28.8 | 32.2 | mm |
| `e_mm` | Radial eccentricity | 3.33 | 4.07 | mm |
| `phi_deg` | Eccentric angle | 5 | 8.42 | degrees |
| `alpha_deg` | Full cone angle | 20 | 35.78 | degrees |
| `Dmax_mm` | Seal-cone base diameter | 184.1 | 191.40 | mm |
| `bm_mm` | Sealing-sheet thickness | 7 | 8 | mm |
| `ds_mm` | Shaft diameter | 41.47 | 45.0 | mm |

Several upper bounds are close to the reference design, and the feasible domain is coupled rather than rectangular. A sampled design may fail geometry or topology checks without indicating a generator bug. `alpha_deg` is the **full** cone angle; the CAD mapping writes `alpha_deg / 2` to relevant half-angle dimensions. The local [Chinese technical notes](README.md) contain more project-specific background; the detailed validation reports are not included in the source-only Git repository.

## Generate a workbook

The script requires Node.js and `@oai/artifact-tool` in its runtime environment.

```powershell
cd "data generate"
node generate_lhs.mjs --samples 3600 --seed 20260921 --output "outputs\designs.xlsx"
```

The same seed and sample count reproduce the same design points. The workbook contains a numbered sample sheet and a bounds sheet. Candidate designs must still pass CAD rebuild, topology, interference, and internal-fluid-domain checks before they become training data.

The current batch runner expects an input sheet with a unique sample ID and the seven exact field names above. Consult the [English batch guide](../SolidWorks-Batch-Parametric-Design-and-CFD-Simulation/Instruction.en.md) before running SolidWorks.
