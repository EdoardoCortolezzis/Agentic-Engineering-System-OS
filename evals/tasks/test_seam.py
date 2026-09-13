import ast
from pathlib import Path

import pytest


TASKS_DIR = Path(__file__).resolve().parents[2] / "harness" / "scripts" / "tasks"


@pytest.mark.parametrize("module_name", ["contract.py", "readiness.py"])
def test_pure_modules_do_not_import_provider_or_subprocess(module_name: str) -> None:
    source = (TASKS_DIR / module_name).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert "subprocess" not in imported_modules
    assert not any(
        name == "provider_github" or name.endswith(".provider_github")
        for name in imported_modules
    )
