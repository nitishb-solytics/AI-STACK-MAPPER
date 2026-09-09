"""Walks a repository and aggregates findings into a ScanResult."""
import os
from typing import List

from .models import ScanResult, utc_timestamp
from .ast_visitor import scan_source
from .config_scanner import scan_dependency_file, scan_mcp_config, scan_env_file, scan_js_dependency_file
from .entry_point_agents import infer_entry_point_agents
from .entry_points import find_entry_points
from .fsutil import prune_dirs
from .import_graph import build_module_index, build_python_import_graph
from .local_agents import infer_local_agents, scan_prompt_source
from .registry import MCP_CONFIG_FILENAMES, DEPENDENCY_FILES
from .runtime_edges import build_runtime_container_edges

# How local agents are inferred from the raw component findings.
#   path         -- name an agent when a file path looks AI-ish (default)
#   entry-points -- name an agent per exposed route/task that can reach an LLM
DISCOVERY_PATH = "path"
DISCOVERY_ENTRY_POINTS = "entry-points"
AGENT_DISCOVERY_MODES = (DISCOVERY_PATH, DISCOVERY_ENTRY_POINTS)

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "venv", ".venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".tox", "site-packages", ".idea", ".vscode-test", "egg-info",
}

ENV_FILENAMES = {".env", ".env.example", ".env.local", ".env.sample"}
JS_DEPENDENCY_FILENAMES = {"package.json"}


def _iter_files(root: str):
    # Directory order is sorted so a scan of the same tree produces
    # byte-identical reports on any filesystem. Vault uses `content_hash` for
    # change detection, so walk order must not be able to invent a "change".
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = prune_dirs(dirpath, dirnames, IGNORE_DIRS)
        for fn in sorted(filenames):
            yield os.path.join(dirpath, fn)


def scan_directory(
    root: str,
    scanner_mode: str = "static",
    agent_discovery: str = DISCOVERY_PATH,
) -> ScanResult:
    root = os.path.abspath(root)
    result = ScanResult(
        root=root,
        generated_at=utc_timestamp(),
        scanned_files=0,
        scanner_mode=scanner_mode,
        agent_discovery=agent_discovery,
    )
    python_sources = {}

    for path in _iter_files(root):
        rel = os.path.relpath(path, root)
        fn = os.path.basename(path)
        findings: List = []

        try:
            if fn.endswith(".py"):
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    source = f.read()
                python_sources[rel] = source
                findings = scan_source(rel, source)
                findings.extend(scan_prompt_source(rel, source))
                result.scanned_files += 1
            elif fn in MCP_CONFIG_FILENAMES:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    source = f.read()
                findings = scan_mcp_config(rel, source)
            elif fn in DEPENDENCY_FILES:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    source = f.read()
                findings = scan_dependency_file(rel, source)
            elif fn in JS_DEPENDENCY_FILENAMES:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    source = f.read()
                findings = scan_js_dependency_file(rel, source)
            elif fn in ENV_FILENAMES:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    source = f.read()
                findings = scan_env_file(rel, source)
            else:
                continue
        except SyntaxError:
            result.skipped_files.append(rel)
            continue
        except (OSError, UnicodeDecodeError):
            result.skipped_files.append(rel)
            continue

        for category, name, package, occurrence in findings:
            result.add(category, name, package, occurrence)

    import_graph = build_python_import_graph(python_sources)
    stack = result.to_dict()

    if agent_discovery == DISCOVERY_ENTRY_POINTS:
        entry_points = find_entry_points(python_sources)
        result.entry_points = [e.to_dict() for e in entry_points]
        result.local_agents = infer_entry_point_agents(
            stack,
            entry_points,
            import_graph=import_graph,
            runtime_edges=build_runtime_container_edges(
                python_sources, build_module_index(python_sources)
            ),
            # Tier B fallback: path-inferred agents no entry point covers, re-gated
            # on the same LLM evidence before they are allowed through.
            path_agents=infer_local_agents(stack, import_graph=import_graph),
        )
    else:
        result.local_agents = infer_local_agents(stack, import_graph=import_graph)
    return result
