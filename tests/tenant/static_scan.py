"""Static scan for workspace-owned models used outside repositories (acceptance #16).

Any runtime reference to a workspace-owned model in ``src/`` outside the allowed packages is a
violation: in a query (``select(M)``, ``session.get(M, ...)``, ``join(M)``, ...), as attribute
access (``M.workspace_id == x``), or as instantiation (``M(...)``). Import aliases
(``import ... as``, ``from ... import M as X``, module aliases like ``models.M``) are resolved.
Imports and annotations are not references.

Outside ``assetboard.db``, raw SQL is banned outright, whatever it names: any call to
``sqlalchemy.text(...)`` (aliases resolved) or ``.exec_driver_sql(...)``, and any
``.execute(...)`` whose first argument is a string literal or f-string. SQLAlchemy 2's
``execute`` refuses plain strings, so raw SQL through SQLAlchemy needs one of the first two.
Limitation: a DB-API cursor's ``execute(sql)`` with SQL built in a variable is not detected;
cursors are only used inside ``assetboard.db``.
"""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import sqlalchemy

Kind = Literal["instantiation", "attribute", "query", "raw_sql"]

# Packages (or modules) where workspace-owned models may be used freely; matched exactly or as
# a parent package, never as a bare string prefix.
ALLOWED_PACKAGES = (
    "assetboard.db.base",
    "assetboard.db.models",
    "assetboard.db.repositories",
    "assetboard.db.migrations",
)

# Bootstrap code that runs before any Context exists: (module, function) -> reason.
BOOTSTRAP_ALLOWLIST: dict[tuple[str, str], str] = {
    ("assetboard.services.auth", "_create_user_records"): (
        "first-run setup, registration and create-admin create a workspace and its owner "
        "membership before a Context can exist"
    ),
    ("assetboard.services.auth", "_audit"): (
        "auth and admin audit events are written outside any workspace Context, usually with "
        "workspace_id NULL (instance-level); workspace events use AuditEventRepository"
    ),
}

# Raw SQL is banned outside this package.
RAW_SQL_EXEMPT_PACKAGE = "assetboard.db"


def in_package(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


@dataclass(frozen=True)
class Violation:
    module: str
    function: str
    line: int
    model: str
    kind: Kind

    def __str__(self) -> str:
        return f"{self.module}:{self.line} in {self.function}: {self.kind} of {self.model}"


def _resolve_dotted(dotted: str) -> object | None:
    parts = dotted.split(".")
    for split in range(len(parts), 0, -1):
        try:
            obj: Any = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        for attribute in parts[split:]:
            obj = getattr(obj, attribute, None)
            if obj is None:
                return None
        return obj
    return None


class _Scanner(ast.NodeVisitor):
    def __init__(self, module: str, models: Mapping[str, type[Any]]) -> None:
        self.module = module
        self.models = models  # dotted path of each model's definition -> class
        self.check_raw_sql = not in_package(module, RAW_SQL_EXEMPT_PACKAGE)
        self.aliases: dict[str, str] = {}  # local name -> dotted path
        self.functions: list[str] = []
        self.violations: list[Violation] = []
        self._counted: set[int] = set()

    # Imports define aliases; they are not references.

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self.aliases[alias.asname] = alias.name
            else:
                root = alias.name.split(".")[0]
                self.aliases[root] = root

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        if node.level:
            package = self.module.rsplit(".", node.level)[0]
            base = f"{package}.{base}" if base else package
        for alias in node.names:
            self.aliases[alias.asname or alias.name] = f"{base}.{alias.name}"

    # Annotations are not references.

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        arguments = node.args
        for default in [*arguments.defaults, *arguments.kw_defaults]:
            if default is not None:
                self.visit(default)
        self.functions.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)

    def _model_for(self, node: ast.expr) -> type[Any] | None:
        dotted = self._dotted(node)
        if dotted is None:
            return None
        resolved = _resolve_dotted(dotted)
        for model in self.models.values():
            if resolved is model:
                return model
        return None

    def _dotted(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id)
        if isinstance(node, ast.Attribute):
            parent = self._dotted(node.value)
            return f"{parent}.{node.attr}" if parent else None
        return None

    def _report(self, node: ast.expr, subject: str, kind: Kind) -> None:
        if id(node) in self._counted:
            return
        self._counted.add(id(node))
        function = ".".join(self.functions) or "<module>"
        self.violations.append(Violation(self.module, function, node.lineno, subject, kind))

    def visit_Call(self, node: ast.Call) -> None:
        model = self._model_for(node.func)
        if model is not None:
            self._report(node.func, model.__name__, "instantiation")
        if self.check_raw_sql and self._is_raw_sql(node):
            self._report(node, "raw SQL", "raw_sql")
        self.generic_visit(node)

    def _is_raw_sql(self, node: ast.Call) -> bool:
        dotted = self._dotted(node.func)
        if dotted is not None and _resolve_dotted(dotted) is sqlalchemy.text:
            return True
        if isinstance(node.func, ast.Attribute):
            if node.func.attr == "exec_driver_sql":
                return True
            if node.func.attr == "execute" and node.args:
                return isinstance(node.args[0], ast.Constant | ast.JoinedStr)
        return False

    def visit_Attribute(self, node: ast.Attribute) -> None:
        model = self._model_for(node)  # ``models.Membership``: the model itself
        if model is not None:
            self._report(node, model.__name__, "query")
            return
        model = self._model_for(node.value)  # ``Membership.workspace_id``
        if model is not None:
            self._report(node.value, model.__name__, "attribute")
            return
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        model = self._model_for(node)
        if model is not None:
            self._report(node, model.__name__, "query")


def scan_source(source: str, module: str, models: Iterable[type[Any]]) -> list[Violation]:
    by_path = {f"{model.__module__}.{model.__qualname__}": model for model in models}
    scanner = _Scanner(module, by_path)
    scanner.visit(ast.parse(source))
    return scanner.violations


def _module_name(path: Path, src_root: Path) -> str:
    parts = list(path.relative_to(src_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def scan_tree(src_root: Path, models: Iterable[type[Any]]) -> list[Violation]:
    models = list(models)
    violations: list[Violation] = []
    for path in sorted(src_root.rglob("*.py")):
        module = _module_name(path, src_root)
        if any(in_package(module, package) for package in ALLOWED_PACKAGES):
            continue
        violations.extend(scan_source(path.read_text(encoding="utf-8"), module, models))
    return violations


def split_allowed(
    violations: Iterable[Violation], allowlist: Mapping[tuple[str, str], str]
) -> tuple[list[Violation], set[tuple[str, str]]]:
    """Violations not covered by the allowlist, and the allowlist entries that were used."""
    remaining: list[Violation] = []
    used: set[tuple[str, str]] = set()
    for violation in violations:
        key = (violation.module, violation.function)
        if key in allowlist:
            used.add(key)
        else:
            remaining.append(violation)
    return remaining, used
