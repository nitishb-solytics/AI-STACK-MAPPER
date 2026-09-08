"""Best-effort logical local-agent discovery.

This layer groups low-level stack evidence (LLMs, vector stores, tools,
prompts) into application-level "agents" such as TextToSql, AutoTag, or
RegulatoryMapping. It is intentionally deterministic and conservative: it
does not execute code and only uses paths/file names plus existing scanner
occurrences.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List

from .import_graph import reachable_imports
from .models import (
    CATEGORY_AGENT_FRAMEWORK,
    CATEGORY_LLM,
    CATEGORY_MCP,
    CATEGORY_PROMPT,
    CATEGORY_TOOL,
    CATEGORY_VECTOR_STORE,
    Occurrence,
)

GENERIC_TOP_LEVEL = {
    ".github",
    "application",
    "constants",
    "docs",
    "logs",
    "middleware",
    "models",
    "tests",
    "test",
    "utils",
    "utilities",
    "common",
    "config",
    "configs",
    "scripts",
    "scanner_reports",
    "service",
    "services",
    "plugin",
    "plugins",
    "api",
    "apis",
    "db",
    "database",
    "databases",
    "shared",
    "pkg",
    "packages",
    "routes",
    "route",
    "vector-db",
    "vectordb",
    "llm",
    "embeddings",
    "prompts",
}

AGENT_NAME_HINTS = (
    "agent",
    "autotag",
    "assess",
    "chat",
    "doc",
    "filter",
    "mapping",
    "query",
    "rag",
    "regulatory",
    "retrieval",
    "search",
    "sql",
    "suggest",
    "tag",
    "text",
    "workflow",
)

PROMPT_WORDS = (
    "prompt",
    "system_prompt",
    "human_prompt",
    "chatprompttemplate",
    "prompttemplate",
    "guideline",
)
INFRA_TOP_LEVEL = {"api", "apis", "db", "database", "databases", "shared", "plugin", "plugins", "pkg", "packages"}
IMPORT_ATTRIBUTION_BARRIER_PARTS = {
    "tests",
    "test",
    "__tests__",
    "migrations",
    "versions",
    "telemetry",
    "observability",
    "monitoring",
    "instrumentation",
}
IMPORT_ATTRIBUTION_BARRIER_FILES = {
    "logging.py",
    "logger.py",
    "tracing.py",
    "metrics.py",
}


def display_name(value: str) -> str:
    base = os.path.splitext(os.path.basename(value.replace("\\", "/")))[0] or value
    base = re.sub(r"[_\-]+", " ", base)
    base = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", base)
    return " ".join(part.capitalize() for part in base.split())


def agent_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", display_name(value).lower()).strip("-") or "codebase"


def _is_import_attribution_barrier(rel_path: str) -> bool:
    """True for support modules whose imports do not describe agent capability.

    A logging module importing ``transformers`` to silence its logger must not
    make every caller a Hugging Face agent. The module is also a traversal
    barrier, preventing broad utility dependencies from polluting ownership.
    """
    parts = [part.lower() for part in rel_path.replace("\\", "/").split("/") if part]
    return bool(
        set(parts) & IMPORT_ATTRIBUTION_BARRIER_PARTS
        or (parts and parts[-1] in IMPORT_ATTRIBUTION_BARRIER_FILES)
    )


def infer_agent_from_file(rel_path: str) -> str | None:
    normalized = rel_path.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    if not parts:
        return None

    filename = parts[-1]
    stem = os.path.splitext(filename)[0]
    first = parts[0]
    first_lower = first.lower()
    stem_lower = stem.lower()
    lower_parts = [part.lower() for part in parts]

    if any(part in {"test", "tests", "__tests__", "migrations", "versions"} for part in lower_parts):
        return None
    if stem_lower.startswith("test") or stem_lower.endswith("_test") or stem_lower.endswith("test"):
        return None
    if re.match(r"^\d{8}[_-]\d{4}", stem_lower):
        return None
    if first_lower in INFRA_TOP_LEVEL:
        return None
    if first_lower in {"service", "services"} and "agents" not in lower_parts and "agent" not in stem_lower:
        return None
    if first_lower in {"service", "services"} and "agents" in lower_parts:
        agent_idx = lower_parts.index("agents")
        if len(parts) > agent_idx + 1 and lower_parts[agent_idx + 1] not in {"__init__", "agent", "agents"}:
            return parts[agent_idx + 1]

    if first_lower == "prompts" and stem_lower not in {"__init__", "prompts", "prompt"}:
        return stem

    if first_lower not in GENERIC_TOP_LEVEL:
        if any(hint in first_lower for hint in AGENT_NAME_HINTS):
            return first

    if stem_lower not in {"__init__", "main", "manage", "config", "settings", "providers"}:
        if any(hint in stem_lower for hint in AGENT_NAME_HINTS):
            return stem

    return None


def scan_prompt_source(rel_path: str, source: str) -> list[tuple[str, str, str, Occurrence]]:
    lower_path = rel_path.lower().replace("\\", "/")
    lower_source = source.lower()
    has_prompt_path = "prompt" in lower_path or "guideline" in lower_path
    has_prompt_code = bool(
        re.search(r"\b(chatprompttemplate|prompttemplate|system_prompt|human_prompt)\b", lower_source)
        # Generic ``message =`` state/error/logging variables are not prompts.
        or re.search(r"\b[a-zA-Z_]*(prompt|template)[a-zA-Z_]*\s*=", source)
    )
    if not has_prompt_path and not has_prompt_code:
        return []

    line_no = 1
    detail = "Prompt-like file/content detected"
    for idx, line in enumerate(source.splitlines(), start=1):
        lowered = line.lower()
        if any(word in lowered for word in PROMPT_WORDS):
            line_no = idx
            detail = line.strip()[:300] or detail
            break

    owner = infer_agent_from_file(rel_path) or os.path.splitext(os.path.basename(rel_path))[0] or "Codebase"
    occurrence = Occurrence(
        file=rel_path,
        line=line_no,
        match_type="prompt",
        confidence="medium",
        detail=detail,
        context_hint=display_name(owner),
    )
    return [(CATEGORY_PROMPT, f"{display_name(owner)} Prompt", "", occurrence)]


def _iter_component_occurrences(stack: dict) -> Iterable[tuple[str, dict, dict]]:
    for category, components in (stack.get("categories") or {}).items():
        for component in components or []:
            for occurrence in component.get("occurrences") or []:
                yield category, component, occurrence


def infer_local_agents(
    stack: dict,
    import_graph: dict[str, set[str]] | None = None,
    max_import_depth: int = 4,
) -> List[Dict[str, Any]]:
    """Infer agents and attribute components through bounded local imports.

    Direct path ownership establishes the agents. Once established, an agent
    also receives components found in repository-local modules reachable from
    its files. Evidence owned by another named agent is never borrowed.
    """
    grouped: dict[str, dict] = {}
    bucket_names = {
        CATEGORY_LLM: "llm_providers",
        CATEGORY_VECTOR_STORE: "knowledge",
        CATEGORY_TOOL: "tools",
        CATEGORY_MCP: "tools",
        CATEGORY_PROMPT: "prompts",
        CATEGORY_AGENT_FRAMEWORK: "frameworks",
    }

    for category, component, occurrence in _iter_component_occurrences(stack):
        rel_file = occurrence.get("file") or ""
        owner = infer_agent_from_file(rel_file)
        if not owner:
            continue
        key = agent_key(owner)
        row = grouped.setdefault(
            key,
            {
                "key": key,
                "name": display_name(owner),
                "files": set(),
                "occurrences": 0,
                "components": defaultdict(set),
                "evidence": [],
            },
        )
        row["files"].add(rel_file)
        row["occurrences"] += 1
        bucket = bucket_names.get(category, "components")
        if component.get("name"):
            row["components"][bucket].add(component["name"])
        if len(row["evidence"]) < 25:
            row["evidence"].append(
                {
                    "category": category,
                    "component": component.get("name"),
                    "package": component.get("package"),
                    "file": rel_file,
                    "line": occurrence.get("line"),
                    "match_type": occurrence.get("match_type"),
                    "detail": occurrence.get("detail"),
                    "attribution": "direct_path",
                    "import_depth": 0,
                    "ownership_confidence": "high",
                }
            )

    if import_graph:
        attribution_graph = {
            source: {
                dependency
                for dependency in dependencies
                if not _is_import_attribution_barrier(dependency)
            }
            for source, dependencies in import_graph.items()
            if not _is_import_attribution_barrier(source)
        }
        occurrences_by_file: dict[str, list[tuple[str, dict, dict]]] = defaultdict(list)
        for category, component, occurrence in _iter_component_occurrences(stack):
            rel_file = (occurrence.get("file") or "").replace("\\", "/")
            if rel_file:
                occurrences_by_file[rel_file].append((category, component, occurrence))

        for key, row in grouped.items():
            depths = reachable_imports(
                attribution_graph, row["files"], max_depth=max_import_depth
            )
            seen = {
                (
                    evidence.get("category"),
                    evidence.get("component"),
                    (evidence.get("file") or "").replace("\\", "/"),
                    evidence.get("line"),
                )
                for evidence in row["evidence"]
            }
            dependency_files = set()
            for rel_file, depth in sorted(depths.items(), key=lambda item: (item[1], item[0])):
                if depth == 0:
                    continue
                file_owner = infer_agent_from_file(rel_file)
                if file_owner and agent_key(file_owner) != key:
                    continue
                for category, component, occurrence in occurrences_by_file.get(rel_file, []):
                    bucket = bucket_names.get(category)
                    if not bucket or not component.get("name"):
                        continue
                    row["components"][bucket].add(component["name"])
                    evidence_key = (
                        category,
                        component.get("name"),
                        rel_file,
                        occurrence.get("line"),
                    )
                    if evidence_key in seen:
                        continue
                    seen.add(evidence_key)
                    dependency_files.add(rel_file)
                    row["occurrences"] += 1
                    if len(row["evidence"]) < 100:
                        row["evidence"].append(
                            {
                                "category": category,
                                "component": component.get("name"),
                                "package": component.get("package"),
                                "file": rel_file,
                                "line": occurrence.get("line"),
                                "match_type": occurrence.get("match_type"),
                                "detail": occurrence.get("detail"),
                                "attribution": "local_import",
                                "import_depth": depth,
                                "ownership_confidence": "medium" if depth == 1 else "low",
                            }
                        )
            row["dependency_files"] = dependency_files

    agents = []
    for row in grouped.values():
        component_summary = {k: sorted(v) for k, v in row["components"].items()}
        score = row["occurrences"] + len(row["files"]) + sum(len(v) for v in component_summary.values())
        agents.append(
            {
                "key": row["key"],
                "name": row["name"],
                "score": score,
                "files": sorted(row["files"]),
                "dependency_files": sorted(row.get("dependency_files") or []),
                "components": component_summary,
                "evidence": row["evidence"],
            }
        )

    return sorted(agents, key=lambda a: (-a["score"], a["name"]))
