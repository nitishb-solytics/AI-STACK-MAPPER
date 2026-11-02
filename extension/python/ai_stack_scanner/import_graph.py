"""Static, repository-local Python import graph.

The scanner must never import or execute the repository being inspected. This
module resolves only imports whose target is another ``.py`` file in the
scanned workspace. Third-party and unresolved imports are ignored.
"""
from __future__ import annotations

import ast
from collections import deque
from typing import Dict, Iterable, Mapping, Set


_SOURCE_ROOTS = {"src", "python", "lib"}


def _normalise(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("./")


def _module_name(path: str) -> str:
    parts = _normalise(path).split("/")
    if not parts or not parts[-1].endswith(".py"):
        return ""
    parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(part for part in parts if part)


def _module_aliases(module: str) -> Iterable[str]:
    if not module:
        return ()
    aliases = [module]
    parts = module.split(".")
    # Common source layouts expose ``src/project/x.py`` as ``project.x``.
    if len(parts) > 1 and parts[0].lower() in _SOURCE_ROOTS:
        aliases.append(".".join(parts[1:]))
    return aliases


def _absolute_from_module(current_file: str, module: str | None, level: int) -> str:
    if not level:
        return module or ""
    current_module = _module_name(current_file)
    current_parts = current_module.split(".") if current_module else []
    if not current_file.replace("\\", "/").endswith("/__init__.py"):
        current_parts = current_parts[:-1]
    keep = max(0, len(current_parts) - (level - 1))
    prefix = current_parts[:keep]
    if module:
        prefix.extend(module.split("."))
    return ".".join(prefix)


def build_module_index(sources: Mapping[str, str]) -> Dict[str, str]:
    """Map importable module name -> repo-relative file, for unambiguous names.

    Ambiguous names (two files claiming one module path) are dropped rather
    than guessed, for the same reason ``build_python_import_graph`` refuses to
    resolve them: a wrong edge is worse than a missing one.
    """
    module_paths: dict[str, set[str]] = {}
    for path in sources:
        normalised = _normalise(path)
        for alias in _module_aliases(_module_name(normalised)):
            module_paths.setdefault(alias, set()).add(normalised)
    return {module: next(iter(paths)) for module, paths in module_paths.items() if len(paths) == 1}


def build_python_import_graph(sources: Mapping[str, str]) -> Dict[str, Set[str]]:
    """Return ``source file -> directly imported local files``.

    Ambiguous aliases are deliberately not resolved: a wrong relationship is
    more damaging to governance inventory than a relationship requiring review.
    """
    normalised_sources = {_normalise(path): source for path, source in sources.items()}
    module_paths: dict[str, set[str]] = {}
    for path in normalised_sources:
        for alias in _module_aliases(_module_name(path)):
            module_paths.setdefault(alias, set()).add(path)

    def resolve(module: str) -> str | None:
        candidates = module_paths.get(module) or set()
        return next(iter(candidates)) if len(candidates) == 1 else None

    graph: Dict[str, Set[str]] = {path: set() for path in normalised_sources}
    for path, source in normalised_sources.items():
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = _absolute_from_module(path, node.module, node.level)
                # ``from package import child`` can refer to child.py or to a
                # symbol exported by package/__init__.py; try both forms.
                targets.extend(
                    f"{base}.{alias.name}" if base else alias.name
                    for alias in node.names
                    if alias.name != "*"
                )
                if base:
                    targets.append(base)
            for target in targets:
                resolved = resolve(target)
                if resolved and resolved != path:
                    graph[path].add(resolved)
    return graph


def reachable_imports(
    graph: Mapping[str, Set[str]], roots: Iterable[str], max_depth: int = 4
) -> Dict[str, int]:
    """Return local files reachable from roots and their shortest import depth."""
    depths: Dict[str, int] = {}
    queue = deque()
    for root in roots:
        path = _normalise(root)
        if path in graph and path not in depths:
            depths[path] = 0
            queue.append(path)

    while queue:
        current = queue.popleft()
        depth = depths[current]
        if depth >= max_depth:
            continue
        for dependency in sorted(graph.get(current) or ()):
            if dependency not in depths or depth + 1 < depths[dependency]:
                depths[dependency] = depth + 1
                queue.append(dependency)
    return depths
