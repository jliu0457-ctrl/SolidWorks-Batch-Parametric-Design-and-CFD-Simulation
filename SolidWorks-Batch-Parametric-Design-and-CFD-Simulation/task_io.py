"""Scoped authoring helper: writes only inside this task directory."""
from pathlib import Path
import argparse, base64, json
ROOT = Path(__file__).resolve().parent
p=argparse.ArgumentParser()
p.add_argument("command", choices=["write"])
p.add_argument("--payload-b64", required=True)
a=p.parse_args()
items=json.loads(base64.b64decode(a.payload_b64).decode("utf-8"))
if not isinstance(items,list): items=[items]
targets=[]
for item in items:
    rel=Path(item["path"])
    if rel.is_absolute() or ".." in rel.parts: raise ValueError("relative task path required")
    out=(ROOT/rel).resolve()
    if not out.is_relative_to(ROOT) or out==Path(__file__).resolve(): raise ValueError("outside task scope")
    targets.append((out,item["text"]))
for out,body in targets:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body,encoding="utf-8")
    print(str(out))
