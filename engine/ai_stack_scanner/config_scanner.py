"""
Scanners for artifacts that aren't Python source: dependency manifests,
MCP server config JSON files, and env-var *names* (never values).
"""
import json
import re
from typing import List, Tuple

from .models import Occurrence, CATEGORY_MCP, CATEGORY_LLM, CONFIDENCE_MEDIUM, CONFIDENCE_LOW
from .models import DEPLOYMENT_CLOUD, DEPLOYMENT_SELF_HOSTED
from .registry import PACKAGE_REGISTRY, ENV_KEY_PATTERNS, SELF_HOSTED_PACKAGES
from .ast_visitor import Finding

_REQUIREMENTS_LINE_RE = re.compile(r"^\s*([A-Za-z0-9_\-\.]+)")
_TOML_DEP_LINE_RE = re.compile(r'"([A-Za-z0-9_\-\.]+)\s*[><=!~\[]')
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Z0-9_]+)\s*=")


def _normalize_pkg(name: str) -> str:
    return name.replace("-", "_").lower()


def scan_dependency_file(filename: str, source: str) -> List[Finding]:
    """requirements.txt, pyproject.toml, Pipfile -> declared-dependency findings."""
    findings: List[Finding] = []
    seen = set()
    for lineno, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidates = []
        m = _REQUIREMENTS_LINE_RE.match(stripped)
        if m:
            candidates.append(m.group(1))
        for m in _TOML_DEP_LINE_RE.finditer(stripped):
            candidates.append(m.group(1))
        for raw in candidates:
            norm = _normalize_pkg(raw)
            reg_key = None
            if norm in PACKAGE_REGISTRY:
                reg_key = norm
            else:
                # try matching against known top-level packages by prefix
                for known in PACKAGE_REGISTRY:
                    if norm == known or norm.startswith(known + "_"):
                        reg_key = known
                        break
            if reg_key and (reg_key, lineno) not in seen:
                seen.add((reg_key, lineno))
                category, display = PACKAGE_REGISTRY[reg_key]
                deployment_target = DEPLOYMENT_SELF_HOSTED if reg_key in SELF_HOSTED_PACKAGES else DEPLOYMENT_CLOUD
                findings.append((
                    category, display, reg_key,
                    Occurrence(
                        filename, lineno, "dependency", CONFIDENCE_MEDIUM,
                        detail=raw, deployment_target=deployment_target,
                    ),
                ))
    return findings


def scan_mcp_config(filename: str, source: str) -> List[Finding]:
    """mcp.json / claude_desktop_config.json -> declared MCP servers."""
    findings: List[Finding] = []
    try:
        data = json.loads(source)
    except json.JSONDecodeError:
        return findings
    servers = data.get("mcpServers") or data.get("mcp_servers") or {}
    if isinstance(servers, dict):
        for server_name, cfg in servers.items():
            command = ""
            if isinstance(cfg, dict):
                command = cfg.get("command", "")
            findings.append((
                CATEGORY_MCP, f"MCP server config: {server_name}", "",
                Occurrence(filename, 1, "config", CONFIDENCE_MEDIUM, detail=command),
            ))
    return findings


def scan_env_file(filename: str, source: str) -> List[Finding]:
    """.env / .env.example -> weak LLM-provider signals from KEY NAMES only.
    We deliberately never read past the `=`, so secret values can't leak in.
    """
    findings: List[Finding] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        for pattern, provider, deployment_target in ENV_KEY_PATTERNS:
            if re.match(pattern, key):
                findings.append((
                    CATEGORY_LLM, f"{provider} (env var configured)", "",
                    Occurrence(
                        filename, lineno, "env_var", CONFIDENCE_LOW,
                        detail=key, deployment_target=deployment_target,
                    ),
                ))
                break
    return findings
