"""Turn entry points into agents, gated on reachable LLM evidence.

The path-inference in ``local_agents`` names an agent whenever a *file path*
looks AI-ish, which mints agents for any module holding a variable called
``template``. This layer inverts that: start from what the codebase exposes
(see ``entry_points``), walk the repository-local import graph from each
handler, and keep the entry point only if a real LLM client is reachable from
it. An endpoint that merely does CRUD, or administers a vector store, never
becomes an agent no matter what it is called.

Reachability is import-level, not call-level. That over-approximates -- module
A importing B does not prove the handler calls B's LLM -- but it is the safe
direction for a governance inventory (a missed agent is worse than one a
reviewer declines), it degrades gracefully through dependency-injection
containers that defeat static call graphs, and it reuses the traversal barriers
already tuned in ``local_agents``.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .entry_points import EntryPoint
from .import_graph import reachable_imports
from .runtime_edges import merge_edges
from .local_agents import (
    _is_import_attribution_barrier,
    agent_key as _slug,
    display_name,
)
from .models import (
    CATEGORY_AGENT_FRAMEWORK,
    CATEGORY_LLM,
    CATEGORY_MCP,
    CATEGORY_PROMPT,
    CATEGORY_TOOL,
    CATEGORY_VECTOR_STORE,
)

# How an LLM occurrence was found decides whether it can justify an agent.
# Constructing or calling a client is proof the code talks to a model; a bare
# import is suggestive; a package pin or an env-var name proves nothing about
# this entry point and must never gate on its own.
#
# The occurrence's own confidence is part of the test. The registry maps bare
# `Client(...)`/`Server(...)`/`Agent(...)` to "verify provider" entries at
# medium confidence precisely because the name alone does not identify an LLM --
# a `langsmith.Client()` in a telemetry provider is not a model call, and must
# not carry the same weight as `ChatBedrockConverse(...)`.
STRONG_LLM_MATCHES = {"instantiation", "usage"}
MEDIUM_LLM_MATCHES = {"import"}

BUCKETS = {
    CATEGORY_LLM: "llm_providers",
    CATEGORY_VECTOR_STORE: "knowledge",
    CATEGORY_TOOL: "tools",
    CATEGORY_MCP: "tools",
    CATEGORY_PROMPT: "prompts",
    CATEGORY_AGENT_FRAMEWORK: "frameworks",
}

# Module stems that name a layer rather than a capability -- for these the
# route/task label is the better agent name.
GENERIC_MODULE_STEMS = {
    "__init__", "main", "app", "api", "views", "view", "routes", "route",
    "handlers", "handler", "endpoints", "urls", "worker", "workers", "tasks", "core",
}

MAX_EVIDENCE = 100
DEFAULT_MAX_DEPTH = 3

DISCOVERY_ENTRY_POINT = "entry_point"
DISCOVERY_PATH_FALLBACK = "path_fallback"

# (direct-ownership label, transitive-import label) per discovery tier. Tier B
# reuses the vocabulary `local_agents` already emits, so a reviewer reading
# evidence does not have to learn two names for the same relationship.
ATTRIBUTION_LABELS = {
    DISCOVERY_ENTRY_POINT: ("entry_point", "entry_point_import"),
    DISCOVERY_PATH_FALLBACK: ("direct_path", "local_import"),
}


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/")


def _iter_occurrences(stack: Mapping[str, Any]) -> Iterable[tuple]:
    for category, components in (stack.get("categories") or {}).items():
        for component in components or []:
            for occurrence in component.get("occurrences") or []:
                yield category, component, occurrence


def _label_name(entry_points: Sequence[EntryPoint]) -> str:
    """Business-facing name from the entry point labels, e.g. 'POST /auto-tag'."""
    raw = entry_points[0].label
    raw = re.sub(r"^[A-Z]+\s+", "", raw)                  # strip HTTP verb
    raw = re.sub(r"\{[^}]*\}", " ", raw)                  # strip {path_params}
    parts = [p for p in re.split(r"[/_\-.]+", raw) if p and not p.startswith(":")]
    return display_name(" ".join(parts)) if parts else ""


def _agent_name(module: str, entry_points: Sequence[EntryPoint]) -> str:
    stem = os.path.splitext(os.path.basename(module))[0]
    if stem.lower() not in GENERIC_MODULE_STEMS:
        return display_name(stem)
    return _label_name(entry_points) or display_name(stem)


def _unique_keys(modules: Sequence[str], names: Mapping[str, str]) -> Dict[str, str]:
    """Stable agent keys, disambiguated by parent directory only on collision.

    The key feeds Vault's ``external_id``, so it is derived from the module path
    rather than the route: renaming a URL must not re-key an existing agent.
    """
    base: Dict[str, str] = {module: _slug(names[module]) for module in modules}
    counts: Dict[str, int] = defaultdict(int)
    for key in base.values():
        counts[key] += 1
    resolved: Dict[str, str] = {}
    for module, key in base.items():
        if counts[key] > 1:
            parent = os.path.basename(os.path.dirname(_norm(module)))
            key = _slug(f"{parent} {names[module]}") if parent else key
        resolved[module] = key
    return resolved


def infer_entry_point_agents(
    stack: Mapping[str, Any],
    entry_points: Sequence[EntryPoint],
    import_graph: Mapping[str, set] | None = None,
    max_import_depth: int = DEFAULT_MAX_DEPTH,
    runtime_edges: Mapping[str, set] | None = None,
    path_agents: Sequence[Mapping[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    """Group entry points by handler module and keep those that reach an LLM.

    ``runtime_edges`` carries service-locator links that no import statement
    expresses (see ``runtime_edges``). Reachability is computed twice -- with
    and without them -- so evidence that exists only because of an inferred
    link is labelled as such rather than passed off as an observed import.

    ``path_agents`` are the path-inferred agents from ``local_agents``. Those
    not already accounted for by an entry point are re-gated on the same LLM
    evidence and kept as a lower-confidence tier, so a repository whose
    framework this scanner cannot parse still reports its AI surface.
    """
    if not entry_points and not path_agents:
        return []

    occurrences_by_file: Dict[str, List[tuple]] = defaultdict(list)
    llm_strength: Dict[str, str] = {}
    for category, component, occurrence in _iter_occurrences(stack):
        rel = _norm(occurrence.get("file") or "")
        if not rel:
            continue
        occurrences_by_file[rel].append((category, component, occurrence))
        if category != CATEGORY_LLM:
            continue
        match = occurrence.get("match_type")
        if match in STRONG_LLM_MATCHES and occurrence.get("confidence") == "high":
            llm_strength[rel] = "strong"
        elif match in STRONG_LLM_MATCHES or match in MEDIUM_LLM_MATCHES:
            llm_strength.setdefault(rel, "medium")

    graph = {
        source: {d for d in deps if not _is_import_attribution_barrier(d)}
        for source, deps in (import_graph or {}).items()
        if not _is_import_attribution_barrier(source)
    }
    static_graph = graph
    if runtime_edges:
        graph = merge_edges(graph, {
            source: {d for d in deps if not _is_import_attribution_barrier(d)}
            for source, deps in runtime_edges.items()
            if not _is_import_attribution_barrier(source)
        })

    def build(
        roots: Sequence[str],
        key: str,
        name: str,
        discovery: str,
        eps: Sequence[EntryPoint],
        force_low_confidence: bool = False,
    ) -> Dict[str, Any] | None:
        """Gate ``roots`` on reachable LLM evidence and assemble the agent."""
        roots = [r for r in roots if r in graph or r in occurrences_by_file]
        if not roots:
            return None
        depths = reachable_imports(graph, roots, max_depth=max_import_depth) \
            or {r: 0 for r in roots}
        static_only = reachable_imports(static_graph, roots, max_depth=max_import_depth)

        hits = {rel: llm_strength[rel] for rel in depths if rel in llm_strength}
        if not hits:
            return None  # reachable code, but nothing in its closure talks to a model

        # An agent proved only through a synthesised container link is real but
        # inferred, so it never claims the confidence of an observed import.
        via_runtime = {rel for rel in hits if rel not in static_only}
        inferred_only = bool(via_runtime) and len(via_runtime) == len(hits)
        confidence = "high" if "strong" in hits.values() else "medium"
        if inferred_only and confidence == "high":
            confidence = "medium"
        if force_low_confidence:
            confidence = "low"

        direct_label, import_label = ATTRIBUTION_LABELS[discovery]
        components: Dict[str, set] = defaultdict(set)
        evidence: List[Dict[str, Any]] = []
        dependency_files: set = set()

        for rel, depth in sorted(depths.items(), key=lambda item: (item[1], item[0])):
            for category, component, occurrence in occurrences_by_file.get(rel, []):
                bucket = BUCKETS.get(category)
                if not bucket or not component.get("name"):
                    continue
                components[bucket].add(component["name"])
                if depth:
                    dependency_files.add(rel)
                if len(evidence) < MAX_EVIDENCE:
                    if depth == 0:
                        attribution = direct_label
                    elif rel in static_only:
                        attribution = import_label
                    else:
                        attribution = "runtime_container"
                    evidence.append({
                        "category": category,
                        "component": component.get("name"),
                        "package": component.get("package"),
                        "file": rel,
                        "line": occurrence.get("line"),
                        "match_type": occurrence.get("match_type"),
                        "detail": occurrence.get("detail"),
                        "attribution": attribution,
                        "import_depth": depth,
                        "ownership_confidence": (
                            "high" if depth == 0
                            else "low" if attribution == "runtime_container"
                            else "medium" if depth == 1 else "low"
                        ),
                    })

        component_summary = {bucket: sorted(values) for bucket, values in components.items()}
        return {
            "key": key,
            "name": name,
            "score": len(eps) * 5 + len(evidence) + sum(len(v) for v in component_summary.values()),
            "files": sorted(roots),
            "dependency_files": sorted(dependency_files),
            "components": component_summary,
            "evidence": evidence,
            # Discovery metadata -- ignored by the Vault payload builder, but it
            # is what lets a reviewer see *why* this is an agent.
            "discovery": discovery,
            "confidence": confidence,
            "entry_points": [e.to_dict() for e in sorted(eps, key=lambda e: (e.line, e.label))],
            "reachable_modules": len(depths),
            "llm_evidence_files": sorted(hits),
            "llm_via_runtime_container": sorted(via_runtime),
        }

    by_module: Dict[str, List[EntryPoint]] = defaultdict(list)
    for entry in entry_points:
        by_module[_norm(entry.file)].append(entry)

    names = {module: _agent_name(module, eps) for module, eps in by_module.items()}
    keys = _unique_keys(sorted(by_module), names)

    agents: List[Dict[str, Any]] = []
    claimed: set = set()
    for module in sorted(by_module):
        agent = build(
            [module], keys[module], names[module], DISCOVERY_ENTRY_POINT, by_module[module]
        )
        if agent:
            agents.append(agent)
            claimed.update(agent["files"])
            claimed.update(agent["dependency_files"])

    # Tier B: AI-backed code that no entry point accounts for. Route extraction
    # covers the frameworks it knows; a repo built on one it does not parse, or
    # one where the model is called from a library rather than a request, would
    # otherwise report nothing at all. These clear the same LLM bar as tier A --
    # so the prompt-only false positives path inference is prone to stay out --
    # but nothing external anchors them, hence the lower confidence.
    taken_keys = {agent["key"] for agent in agents}
    for candidate in path_agents or []:
        files = [_norm(f) for f in candidate.get("files") or []]
        if not files or any(f in claimed for f in files):
            continue
        key = candidate.get("key") or ""
        if not key or key in taken_keys:
            continue  # same logical agent already reported under an entry point
        agent = build(
            files, key, candidate.get("name") or key, DISCOVERY_PATH_FALLBACK, (),
            force_low_confidence=True,
        )
        if agent:
            agents.append(agent)
            taken_keys.add(key)
            claimed.update(agent["files"])

    return sorted(agents, key=lambda a: (-a["score"], a["name"]))
