"""Plan 3.3: SQL writes (INSERT/UPDATE/DELETE/REPLACE) live only in repository modules.

Route handlers, templates helpers, the CLI and the AI layer must call repository functions
(which wrap writes in ``write_tx``) instead of issuing write SQL themselves.
"""

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent.parent / "app"

# Repository modules: the only places allowed to issue write SQL.
ALLOWED = (
    "db/*.py",  # migrations runner, write_tx
    "inventory/*.py",  # locations, items, categories, search, shortcode (not routes.py)
    "auth/passwords.py",
    "auth/sessions.py",
    "capture/*.py",  # capture service, worker, apply, budget ledger (not routes.py)
    "compare/*.py",  # comparison cases, runs and results (not routes.py)
)
EXECUTE_METHODS = {"execute", "executemany", "executescript"}
WRITE_SQL = re.compile(
    r"^\s*(?:--[^\n]*\n\s*)*(?:INSERT|UPDATE|DELETE|REPLACE)\b"
    r"|^\s*WITH\b.*\b(?:INSERT|UPDATE|DELETE|REPLACE)\b",
    re.IGNORECASE | re.DOTALL,
)


def _literal_text(node: ast.AST) -> str | None:
    """The static text of a string literal or f-string (placeholders dropped)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_text(node.left), _literal_text(node.right)
        if left is not None or right is not None:
            return (left or "") + (right or "")
    return None


def write_calls(source: str) -> list[int]:
    """Line numbers of ``<x>.execute*("INSERT|UPDATE|DELETE|REPLACE ...")`` calls."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in EXECUTE_METHODS
            and node.args
        ):
            text = _literal_text(node.args[0])
            if text is not None and WRITE_SQL.search(text):
                lines.append(node.lineno)
    return lines


def is_allowed(relative: Path) -> bool:
    if relative.name == "routes.py" or relative.name.endswith("_routes.py"):
        return False
    return any(relative.match(pattern) for pattern in ALLOWED)


def test_scanner_detects_write_forms():
    source = """
conn.execute("INSERT INTO items (name) VALUES (?)", x)
conn.execute("update items set name = ?", x)
cur.executemany(
    "DELETE FROM items "
    "WHERE id = ?", rows)
conn.execute(f"UPDATE items SET updated_at = {NOW} WHERE id = ?", x)
conn.execute("  -- comment\\n  REPLACE INTO settings VALUES (1)")
conn.execute("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x")
conn.execute("SELECT 1")
conn.execute("SELECT * FROM items WHERE note = 'UPDATE'")
conn.execute(sql_variable)
"""
    assert write_calls(source) == [2, 3, 4, 7, 8, 9]


def test_allowed_set():
    assert is_allowed(Path("inventory/items.py"))
    assert is_allowed(Path("capture/budget.py"))
    assert not is_allowed(Path("inventory/routes.py"))
    assert not is_allowed(Path("capture/routes.py"))
    assert is_allowed(Path("compare/runs.py"))
    assert not is_allowed(Path("compare/routes.py"))
    assert not is_allowed(Path("settings_routes.py"))
    assert not is_allowed(Path("auth/routes.py"))
    assert not is_allowed(Path("api/routes.py"))
    assert not is_allowed(Path("ai/fake.py"))
    assert not is_allowed(Path("cli.py"))


def test_no_write_sql_outside_repository_modules():
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(APP)
        if is_allowed(relative):
            continue
        offenders += [f"{relative}:{line}" for line in write_calls(path.read_text("utf-8"))]
    assert offenders == [], "write SQL outside repository modules: " + ", ".join(offenders)


def test_repository_modules_are_scanned_and_contain_writes():
    """Guard against the scan silently matching nothing (e.g. a moved APP path)."""
    assert write_calls((APP / "inventory" / "items.py").read_text("utf-8"))
    assert write_calls((APP / "auth" / "sessions.py").read_text("utf-8"))
