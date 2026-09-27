"""Doctor 的版本判定；只提取纯函数，避免导入时运行整套体检。"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


def _load_release_at_least():
    path = Path(__file__).resolve().parents[1] / "scripts" / "Doctor.py"
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(node for node in module.body
                    if isinstance(node, ast.FunctionDef) and node.name == "release_at_least")
    namespace = {"re": re}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["release_at_least"]


@pytest.mark.parametrize("actual,minimum,expected", [
    ("305", (306,), False),
    ("306", (306,), True),
    ("311", (306,), True),
    ("3.1.3", (3, 1, 4), False),
    ("3.1.4", (3, 1, 4), True),
    ("3.1.5", (3, 1, 4), True),
])
def test_doctor_accepts_newer_dependency_versions(actual, minimum, expected):
    assert _load_release_at_least()(actual, minimum) is expected
