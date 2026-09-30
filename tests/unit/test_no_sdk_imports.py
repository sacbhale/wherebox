"""AC-21: no module outside app/ai/ imports the anthropic SDK."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent.parent / "app"
BANNED = {"anthropic"}


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name | ast.Attribute)
            and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
            in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            roots.add(node.args[0].value.split(".")[0])
    return roots


def test_scanner_detects_every_import_form() -> None:
    source = (
        "import anthropic\nimport anthropic.types as t\nfrom anthropic import Anthropic\n"
        "def f():\n    import importlib\n    importlib.import_module('anthropic')\n"
    )
    assert _imported_roots(ast.parse(source)) >= {"anthropic", "importlib"}
    assert "anthropic" in _imported_roots(ast.parse("__import__('anthropic')"))


def test_no_anthropic_imports_outside_app_ai() -> None:
    files = [p for p in APP.rglob("*.py") if "ai" not in p.relative_to(APP).parts[:1]]
    assert files, "no app modules found"
    offenders = sorted(
        str(p.relative_to(APP))
        for p in files
        if _imported_roots(ast.parse(p.read_text(encoding="utf-8"))) & BANNED
    )
    assert offenders == []


def test_adapter_is_inside_app_ai() -> None:
    adapter = APP / "ai" / "anthropic_adapter.py"
    assert "anthropic" in _imported_roots(ast.parse(adapter.read_text(encoding="utf-8")))
