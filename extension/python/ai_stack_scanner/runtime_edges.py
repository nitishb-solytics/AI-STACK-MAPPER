"""Synthesise import edges for service-locator / DI-container access.

A static import graph sees only ``import`` statements, so it misses wiring that
is resolved at runtime. The shape that defeats it looks like this::

    # api/routes/model_chat.py -- imports nothing from container.py
    container = request.app.state.container
    agent = container.make_model_chat_agent(memory=memory)

    # container.py
    @cached_property
    def llm_provider(self):
        from plugins.llm.registry import build_provider   # the real link

The route reaches an LLM at runtime but has no import path to one, so an
import-only walk reports "no model here" for an endpoint that is entirely about
calling a model.

Resolution is **member-level, not module-level**, and that distinction is the
whole point. Linking a consumer to the container wholesale would hand it every
dependency the container can build, turning a health check that only touches
``container.vector_store`` into an AI agent. Instead each container member is
mapped to the modules imported inside *that member's* body, and a consumer
inherits only the members it actually touches.

Two signals must agree before any edge is created:

1. the consumer binds a container-like value (``.state.container``,
   ``get_container()``, a parameter named ``container``); and
2. it reads a member off that specific binding.

Requiring the binding is what stops an unrelated ``foo.create(...)`` call from
matching a container member that happens to be named ``create``.

Edges produced here are inferred, not observed. Callers mark evidence that
depends on them so a reviewer can tell a synthesised link from a real import.
"""
from __future__ import annotations

import ast
import os
from collections import defaultdict
from typing import Dict, Mapping, Set

# Modules whose job is to construct other things.
PROVIDER_FILENAMES = {
    "container.py", "containers.py", "di.py", "bootstrap.py", "composition_root.py",
    "factory.py", "factories.py", "registry.py", "registries.py", "wiring.py",
}
# `Provider` is deliberately absent: every LLM plugin class ends in Provider,
# and treating those as containers would invert the relationship.
PROVIDER_CLASS_SUFFIXES = ("Container", "Registry", "Factory", "Locator")

# Names that, when read off an attribute chain or bound to a variable, mean
# "this is the application's object graph".
CONTAINER_ATTR_NAMES = {"container", "app_container", "di_container", "injector", "locator"}
CONTAINER_FACTORY_CALLS = {
    "get_container", "build_container", "create_container", "make_container",
    "get_injector", "get_di_container",
}

# A member reachable from too many containers is a generic name rather than a
# capability; requiring a bound receiver already filters most, this caps the rest.
MAX_PROVIDERS_PER_MEMBER = 3


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/")


def _resolve(module: str, module_index: Mapping[str, str]) -> str:
    return module_index.get(module, "")


def _local_imports(node: ast.AST, module_index: Mapping[str, str]) -> Set[str]:
    """Repo-local files imported anywhere inside ``node`` (including nested defs)."""
    found: Set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom) and child.module and not child.level:
            for alias in child.names:
                for candidate in (f"{child.module}.{alias.name}", child.module):
                    resolved = _resolve(candidate, module_index)
                    if resolved:
                        found.add(resolved)
                        break
        elif isinstance(child, ast.Import):
            for alias in child.names:
                resolved = _resolve(alias.name, module_index)
                if resolved:
                    found.add(resolved)
    return found


def _is_provider_module(rel: str, tree: ast.AST) -> bool:
    if os.path.basename(_norm(rel)) in PROVIDER_FILENAMES:
        return True
    return any(
        isinstance(node, ast.ClassDef) and node.name.endswith(PROVIDER_CLASS_SUFFIXES)
        for node in ast.iter_child_nodes(tree)
    )


def _member_map(tree: ast.AST, module_index: Mapping[str, str]) -> Dict[str, Set[str]]:
    """member name -> repo-local files imported inside that member's body."""
    members: Dict[str, Set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    imports = _local_imports(item, module_index)
                    if imports:
                        members.setdefault(item.name, set()).update(imports)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Module-level factory functions, e.g. `def build_provider(...)`.
            imports = _local_imports(node, module_index)
            if imports:
                members.setdefault(node.name, set()).update(imports)
    return members


def _is_container_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr in CONTAINER_ATTR_NAMES
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return name in CONTAINER_FACTORY_CALLS
    return False


def _container_bindings(tree: ast.AST) -> Set[str]:
    """Local names holding a container: assigned from one, or injected as a param."""
    bound: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_container_expr(node.value):
            bound.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.value is not None and _is_container_expr(node.value):
            bound.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs):
                if arg.arg in CONTAINER_ATTR_NAMES:
                    bound.add(arg.arg)
    return bound


def _touched_members(tree: ast.AST, bound: Set[str]) -> Set[str]:
    """Members read off a bound container, or off an inline container chain."""
    touched: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        value = node.value
        # `container.llm_provider`
        if isinstance(value, ast.Name) and value.id in bound:
            touched.add(node.attr)
        # `request.app.state.container.llm_provider` -- no binding needed
        elif _is_container_expr(value):
            touched.add(node.attr)
    return touched


def build_runtime_container_edges(
    sources: Mapping[str, str], module_index: Mapping[str, str]
) -> Dict[str, Set[str]]:
    """Return ``consumer file -> files it reaches through a runtime container``."""
    trees: Dict[str, ast.AST] = {}
    for rel, source in sources.items():
        try:
            trees[_norm(rel)] = ast.parse(source, filename=rel)
        except (SyntaxError, ValueError):
            continue

    providers: Dict[str, Dict[str, Set[str]]] = {}
    for rel, tree in trees.items():
        if _is_provider_module(rel, tree):
            members = _member_map(tree, module_index)
            if members:
                providers[rel] = members

    if not providers:
        return {}

    by_member: Dict[str, list] = defaultdict(list)
    for provider, members in providers.items():
        for member, targets in members.items():
            by_member[member].append((provider, targets))

    edges: Dict[str, Set[str]] = {}
    for rel, tree in trees.items():
        if rel in providers:
            continue  # a container resolving itself adds nothing
        bound = _container_bindings(tree)
        touched = _touched_members(tree, bound)
        if not touched:
            continue
        targets: Set[str] = set()
        for member in touched:
            owners = by_member.get(member, [])
            if not owners or len(owners) > MAX_PROVIDERS_PER_MEMBER:
                continue
            for _provider, files in owners:
                targets.update(files)
        targets.discard(rel)
        if targets:
            edges[rel] = targets
    return edges


def merge_edges(
    graph: Mapping[str, Set[str]], extra: Mapping[str, Set[str]]
) -> Dict[str, Set[str]]:
    """Graph with ``extra`` edges unioned in, leaving the input untouched."""
    merged: Dict[str, Set[str]] = {node: set(deps) for node, deps in graph.items()}
    for node, deps in extra.items():
        merged.setdefault(node, set()).update(d for d in deps if d in merged or d in extra)
    return merged
