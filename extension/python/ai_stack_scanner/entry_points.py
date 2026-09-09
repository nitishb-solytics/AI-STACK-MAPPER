"""Locate application entry points: the roots an AI capability hangs off.

Path-name inference asks "does this folder look like an agent?". This module
asks a different question -- "what does this codebase actually expose?" -- and
leaves the decision about which of those exposures are AI-backed to
``entry_point_agents``. An entry point here is anything a request, message or
operator can arrive through:

    http     Django ``path()``/``re_path()``, DRF routers, FastAPI/Flask routes
    task     Celery ``@shared_task`` / ``@app.task``
    command  Django ``BaseCommand`` subclasses
    mcp      ``@mcp.tool()`` / ``@mcp.resource()`` / ``@mcp.prompt()``
    cli      click / typer commands

Deliberately broader than HTTP: in a service that does ingestion on Celery, the
document pipeline is where customer data reaches a model provider, and an
HTTP-only sweep would not see it at all.

Static only -- nothing here imports or executes the scanned repository.
"""
from __future__ import annotations

import ast
import os
from dataclasses import asdict, dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from .import_graph import build_module_index

KIND_HTTP = "http"
KIND_TASK = "task"
KIND_COMMAND = "command"
KIND_MCP = "mcp"
KIND_CLI = "cli"

# Test and fixture trees expose routes and tasks that describe the test suite,
# not the product. Without this a locustfile registers as a Celery agent.
TEST_PATH_PARTS = {"tests", "test", "__tests__", "testing", "fixtures", "conftest"}

HTTP_VERB_DECORATORS = {"get", "post", "put", "delete", "patch", "head", "options", "websocket"}
TASK_DECORATORS = {"shared_task", "periodic_task"}
TASK_ATTR_DECORATORS = {"task", "shared_task", "periodic_task"}
MCP_DECORATORS = {"tool", "resource", "prompt"}
CLI_DECORATORS = {"command"}
MCP_SERVER_CONSTRUCTORS = {"FastMCP", "Server"}
ROUTER_CONSTRUCTORS = {"APIRouter", "Blueprint"}
DJANGO_ROUTE_FUNCS = {"path", "re_path", "url"}
DJANGO_COMMAND_BASES = {"BaseCommand", "AppCommand", "LabelCommand"}


@dataclass
class EntryPoint:
    """One externally reachable operation, anchored to the module that serves it."""

    kind: str
    label: str
    file: str           # repo-relative module that handles the operation
    line: int
    symbol: str = ""    # handler function/class name, when known
    framework: str = ""
    declared_in: str = ""  # where the registration itself lives (urls.py, main.py)

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def is_test_path(rel_path: str) -> bool:
    parts = [part.lower() for part in rel_path.replace("\\", "/").split("/") if part]
    if set(parts) & TEST_PATH_PARTS:
        return True
    stem = os.path.splitext(parts[-1])[0] if parts else ""
    return stem.startswith("test_") or stem.endswith("_test") or stem == "locustfile"


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("./")


def _const_str(node: Optional[ast.AST]) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _kwarg(node: ast.Call, name: str) -> Optional[ast.AST]:
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _decorator_parts(dec: ast.AST) -> Tuple[str, str, Optional[ast.Call]]:
    """Return (root_symbol, attribute_name, call_node) for a decorator."""
    call = dec if isinstance(dec, ast.Call) else None
    target = dec.func if isinstance(dec, ast.Call) else dec
    if isinstance(target, ast.Attribute):
        root = target.value
        return (root.id if isinstance(root, ast.Name) else ""), target.attr, call
    if isinstance(target, ast.Name):
        return "", target.id, call
    return "", "", call


def _join_url(*parts: str) -> str:
    joined = "/".join(part.strip("/") for part in parts if part and part.strip("/"))
    return "/" + joined if joined else "/"


class _ModuleScan:
    """Per-file facts the cross-file passes need."""

    def __init__(self) -> None:
        self.import_candidates: Dict[str, List[str]] = {}  # local name -> dotted candidates
        self.router_prefixes: Dict[str, str] = {}          # local router var -> its own prefix
        self.mcp_servers: set = set()
        self.entry_points: List[EntryPoint] = []
        self.django_routes: List[Tuple[str, ast.AST, int]] = []   # url, handler node, line
        self.django_includes: List[Tuple[str, ast.Call, int]] = []
        self.drf_registrations: List[Tuple[str, ast.AST, int]] = []
        self.router_mounts: List[Tuple[ast.AST, str]] = []        # included router expr, prefix


def _scan_module(rel: str, source: str) -> Optional[_ModuleScan]:
    try:
        tree = ast.parse(source, filename=rel)
    except (SyntaxError, ValueError):
        return None

    scan = _ModuleScan()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                local = alias.asname or alias.name
                # `from a.b import c` -- c may be a submodule or a symbol in a.b
                scan.import_candidates[local] = [f"{node.module}.{alias.name}", node.module]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                scan.import_candidates[local] = [alias.name]

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            name = node.value.func
            ctor = name.id if isinstance(name, ast.Name) else getattr(name, "attr", "")
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if ctor in ROUTER_CONSTRUCTORS:
                prefix = _const_str(_kwarg(node.value, "url_prefix") or _kwarg(node.value, "prefix"))
                for target in targets:
                    scan.router_prefixes[target] = prefix
            elif ctor in MCP_SERVER_CONSTRUCTORS:
                scan.mcp_servers.update(targets)

        elif isinstance(node, ast.Call):
            func = node.func
            fname = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if fname in DJANGO_ROUTE_FUNCS and node.args:
                url = _const_str(node.args[0])
                handler = node.args[1] if len(node.args) > 1 else None
                if isinstance(handler, ast.Call) and isinstance(handler.func, ast.Name) \
                        and handler.func.id == "include":
                    scan.django_includes.append((url, handler, node.lineno))
                elif handler is not None:
                    scan.django_routes.append((url, handler, node.lineno))
            elif fname == "register" and node.args:  # DRF router.register(prefix, ViewSet)
                prefix = _const_str(node.args[0])
                if len(node.args) > 1:
                    scan.drf_registrations.append((prefix, node.args[1], node.lineno))
            elif fname in ("include_router", "register_blueprint") and node.args:
                scan.router_mounts.append((node.args[0], _const_str(_kwarg(node, "prefix"))))

        elif isinstance(node, ast.ClassDef):
            bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases}
            if bases & DJANGO_COMMAND_BASES and "management" in _norm(rel).split("/"):
                scan.entry_points.append(EntryPoint(
                    kind=KIND_COMMAND,
                    label=os.path.splitext(os.path.basename(_norm(rel)))[0],
                    file=_norm(rel), line=node.lineno, symbol=node.name, framework="django",
                ))

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                root, attr, call = _decorator_parts(dec)
                first_arg = _const_str(call.args[0]) if call and call.args else ""
                if root and attr in HTTP_VERB_DECORATORS:
                    local_prefix = scan.router_prefixes.get(root, "")
                    scan.entry_points.append(EntryPoint(
                        kind=KIND_HTTP,
                        label=f"{attr.upper()} {_join_url(local_prefix, first_arg)}",
                        file=_norm(rel), line=node.lineno, symbol=node.name,
                        framework="fastapi/flask", declared_in=root,
                    ))
                elif root and attr == "route":  # Flask
                    local_prefix = scan.router_prefixes.get(root, "")
                    scan.entry_points.append(EntryPoint(
                        kind=KIND_HTTP,
                        label=f"ROUTE {_join_url(local_prefix, first_arg)}",
                        file=_norm(rel), line=node.lineno, symbol=node.name,
                        framework="flask", declared_in=root,
                    ))
                elif attr in TASK_DECORATORS or (root and attr in TASK_ATTR_DECORATORS):
                    name = _const_str(_kwarg(call, "name")) if call else ""
                    scan.entry_points.append(EntryPoint(
                        kind=KIND_TASK, label=name or node.name,
                        file=_norm(rel), line=node.lineno, symbol=node.name, framework="celery",
                    ))
                elif root in scan.mcp_servers and attr in MCP_DECORATORS:
                    scan.entry_points.append(EntryPoint(
                        kind=KIND_MCP, label=f"{attr}:{node.name}",
                        file=_norm(rel), line=node.lineno, symbol=node.name, framework="mcp",
                    ))
                elif attr in CLI_DECORATORS:
                    scan.entry_points.append(EntryPoint(
                        kind=KIND_CLI, label=first_arg or node.name,
                        file=_norm(rel), line=node.lineno, symbol=node.name, framework="click/typer",
                    ))
    return scan


def _resolve_handler_file(
    handler: ast.AST, scan: _ModuleScan, module_index: Mapping[str, str]
) -> Tuple[str, str]:
    """Resolve a Django/DRF handler reference to (file, symbol).

    Handles `View.as_view()`, a bare `view_func`, and `views.View.as_view()`.
    An unresolved handler returns ("", symbol) and the caller drops it -- the
    urls module imports every view, so falling back to it would hand one agent
    every route in the file.
    """
    node = handler.func.value if isinstance(handler, ast.Call) and isinstance(handler.func, ast.Attribute) \
        else handler.func if isinstance(handler, ast.Call) else handler

    symbol = ""
    root = ""
    if isinstance(node, ast.Name):
        symbol = root = node.id
    elif isinstance(node, ast.Attribute):
        symbol = node.attr
        base = node.value
        while isinstance(base, ast.Attribute):
            base = base.value
        root = base.id if isinstance(base, ast.Name) else ""

    for candidate in scan.import_candidates.get(root, []):
        resolved = module_index.get(candidate)
        if resolved:
            return resolved, symbol
    return "", symbol


def find_entry_points(sources: Mapping[str, str]) -> List[EntryPoint]:
    """Extract every entry point from a repo-relative {path: source} mapping."""
    module_index = build_module_index(sources)
    scans: Dict[str, _ModuleScan] = {}
    for rel, source in sources.items():
        rel = _norm(rel)
        if is_test_path(rel):
            continue
        scan = _scan_module(rel, source)
        if scan is not None:
            scans[rel] = scan

    # Django/DRF prefixes compose through include(); resolve each urls module's
    # mounted prefix before labelling its routes.
    mounted_prefix: Dict[str, str] = {}
    for rel, scan in scans.items():
        for url, include_call, _line in scan.django_includes:
            target = include_call.args[0] if include_call.args else None
            dotted = _const_str(target)
            resolved = module_index.get(dotted) if dotted else None
            if not resolved and isinstance(target, ast.Name):
                for candidate in scan.import_candidates.get(target.id, []):
                    resolved = module_index.get(candidate)
                    if resolved:
                        break
            if resolved and resolved not in mounted_prefix:
                mounted_prefix[resolved] = _join_url(mounted_prefix.get(rel, ""), url)

    entry_points: List[EntryPoint] = []
    for rel, scan in scans.items():
        entry_points.extend(scan.entry_points)

        prefix = mounted_prefix.get(rel, "")
        for url, handler, line in scan.django_routes:
            target_file, symbol = _resolve_handler_file(handler, scan, module_index)
            if not target_file or is_test_path(target_file):
                continue
            entry_points.append(EntryPoint(
                kind=KIND_HTTP, label=_join_url(prefix, url), file=target_file, line=line,
                symbol=symbol, framework="django", declared_in=rel,
            ))
        for route_prefix, viewset, line in scan.drf_registrations:
            target_file, symbol = _resolve_handler_file(viewset, scan, module_index)
            if not target_file or is_test_path(target_file):
                continue
            entry_points.append(EntryPoint(
                kind=KIND_HTTP, label=_join_url(prefix, route_prefix), file=target_file, line=line,
                symbol=symbol, framework="drf", declared_in=rel,
            ))

    entry_points.sort(key=lambda e: (e.file, e.line, e.label))
    return entry_points
