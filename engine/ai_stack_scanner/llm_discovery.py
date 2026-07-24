"""
Optional LLM-powered discovery layer for AI-stack inventory.

Unlike ``enrich.py`` (which explains components already found by static
analysis), this module asks an OpenAI-compatible LLM to identify possible AI
stack components from small, redacted repository evidence:

- Python snippets around likely agent/tool/LLM/vector-store code
- dependency/config summaries such as requirements.txt, pyproject.toml,
  package.json, and MCP config names
- .env-style key names only, never values

It is intentionally best-effort and optional. Static scanning remains the
default because it is deterministic and does not send code context anywhere.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import (
    ALL_CATEGORIES,
    CONFIDENCE_HIGH,
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    DEPLOYMENT_CLOUD,
    DEPLOYMENT_SELF_HOSTED,
    DEPLOYMENT_UNKNOWN,
    Occurrence,
    ScanResult,
)
from .registry import DEPENDENCY_FILES, MCP_CONFIG_FILENAMES
from .scanner import ENV_FILENAMES, IGNORE_DIRS, JS_DEPENDENCY_FILENAMES

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
REQUEST_TIMEOUT_SECONDS = 45
MAX_FILES = 80
MAX_SNIPPETS = 60
MAX_SNIPPET_CHARS = 2200
MAX_DEPENDENCY_CHARS = 5000
MAX_RATE_LIMIT_RETRIES = 2
RATE_LIMIT_BACKOFF_SECONDS = 2

ALLOWED_CONFIDENCE = {CONFIDENCE_LOW, CONFIDENCE_MEDIUM, CONFIDENCE_HIGH}
ALLOWED_DEPLOYMENT_TARGETS = {DEPLOYMENT_CLOUD, DEPLOYMENT_SELF_HOSTED, DEPLOYMENT_UNKNOWN}

_JSON_ESCAPE_REPAIR_RE = re.compile(r'\\(?!["\\/bfnrtu])')
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|credential)\b\s*[:=]\s*['\"][^'\"]+['\"]"
)
_LONG_SECRETISH_RE = re.compile(r"(?i)(sk-[A-Za-z0-9_\-]{12,}|[A-Za-z0-9_\-]{32,})")
_ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Z0-9_]+)\s*=")

_PY_HINT_RE = re.compile(
    r"(?i)\b("
    r"agent|crew|task|tool|llm|chat|model|prompt|rag|retriev|embed|vector|memory|"
    r"workflow|orchestrat|planner|executor|mcp|function_call|function calling|"
    r"langchain|langgraph|llamaindex|llama_index|crewai|autogen|semantic_kernel|"
    r"openai|anthropic|gemini|bedrock|ollama|milvus|chroma|pinecone|qdrant|weaviate"
    r")\b"
)
_PATH_HINT_RE = re.compile(
    r"(?i)(agent|crew|task|tool|llm|prompt|rag|retriev|embed|vector|memory|workflow|mcp)"
)

_SYSTEM_PROMPT = (
    "You are an AI stack discovery engine. Identify AI-stack components present "
    "in the supplied repository evidence. Categories allowed: LLM, MCP, TOOL, "
    "AGENT_FRAMEWORK, VECTOR_STORE. Use only the evidence. Do not invent. "
    "Return strict JSON only, no markdown fences, exactly this shape: "
    '{"components":[{"category":"AGENT_FRAMEWORK","name":"CrewAI Agent",'
    '"package":"crewai","confidence":"medium","file":"path.py","line":10,'
    '"match_type":"llm_discovery","detail":"why this was detected",'
    '"deployment_target":"cloud"}]}. '
    "If no components are supported by evidence, return {\"components\":[]}."
)


@dataclass
class LLMDiscoveryConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    timeout: int = REQUEST_TIMEOUT_SECONDS


@dataclass
class EvidenceSnippet:
    file: str
    line: int
    kind: str
    text: str


def _iter_files(root: str) -> Iterable[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.endswith(".egg-info")]
        for filename in filenames:
            yield os.path.join(dirpath, filename)


def _redact(text: str) -> str:
    text = _SECRET_ASSIGNMENT_RE.sub(lambda m: m.group(0).split(m.group(1))[0] + m.group(1) + '="<redacted>"', text)
    return _LONG_SECRETISH_RE.sub("<redacted>", text)


def _line_window(lines: List[str], line_index: int, radius: int = 18) -> Tuple[int, str]:
    start = max(0, line_index - radius)
    end = min(len(lines), line_index + radius + 1)
    numbered = [f"{idx + 1}: {lines[idx]}" for idx in range(start, end)]
    return start + 1, "\n".join(numbered)


def _python_snippets(rel: str, source: str) -> List[EvidenceSnippet]:
    lines = source.splitlines()
    snippets: List[EvidenceSnippet] = []
    seen_windows = set()

    for idx, line in enumerate(lines):
        if not _PY_HINT_RE.search(line):
            continue
        start_line, block = _line_window(lines, idx)
        window_key = (rel, start_line)
        if window_key in seen_windows:
            continue
        seen_windows.add(window_key)
        snippets.append(EvidenceSnippet(rel, start_line, "python_snippet", _redact(block[:MAX_SNIPPET_CHARS])))
        if len(snippets) >= 3:
            break

    if not snippets and _PATH_HINT_RE.search(rel):
        start_line, block = _line_window(lines, 0, radius=30)
        snippets.append(EvidenceSnippet(rel, start_line, "python_path_hint", _redact(block[:MAX_SNIPPET_CHARS])))

    return snippets


def _dependency_summary(rel: str, source: str) -> EvidenceSnippet:
    trimmed = "\n".join(source.splitlines()[:180])[:MAX_DEPENDENCY_CHARS]
    return EvidenceSnippet(rel, 1, "dependency_or_config", _redact(trimmed))


def _env_summary(rel: str, source: str) -> Optional[EvidenceSnippet]:
    keys: List[str] = []
    for line in source.splitlines():
        m = _ENV_LINE_RE.match(line)
        if m:
            keys.append(m.group(1))
    if not keys:
        return None
    return EvidenceSnippet(rel, 1, "env_key_names_only", "\n".join(keys[:120]))


def collect_evidence(root: str) -> Tuple[List[EvidenceSnippet], int, List[str]]:
    """Collect bounded, redacted evidence for LLM discovery."""
    root = os.path.abspath(root)
    snippets: List[EvidenceSnippet] = []
    skipped: List[str] = []
    scanned_python_files = 0
    inspected_files = 0

    for path in _iter_files(root):
        if inspected_files >= MAX_FILES or len(snippets) >= MAX_SNIPPETS:
            break
        inspected_files += 1

        rel = os.path.relpath(path, root)
        filename = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                source = f.read()
        except OSError:
            skipped.append(rel)
            continue

        if filename.endswith(".py"):
            scanned_python_files += 1
            snippets.extend(_python_snippets(rel, source))
        elif filename in DEPENDENCY_FILES or filename in JS_DEPENDENCY_FILENAMES or filename in MCP_CONFIG_FILENAMES:
            snippets.append(_dependency_summary(rel, source))
        elif filename in ENV_FILENAMES:
            env_snippet = _env_summary(rel, source)
            if env_snippet:
                snippets.append(env_snippet)

    return snippets[:MAX_SNIPPETS], scanned_python_files, skipped


def _build_prompt(snippets: List[EvidenceSnippet]) -> str:
    blocks = []
    for snippet in snippets:
        safe_file = snippet.file.replace("\\", "/")
        blocks.append(
            f"FILE: {safe_file}\n"
            f"START_LINE: {snippet.line}\n"
            f"KIND: {snippet.kind}\n"
            "CONTENT:\n"
            f"{snippet.text}"
        )
    return "\n\n---\n\n".join(blocks)


def _extract_json_object(content: str) -> Dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`").strip()
        if content[:4].lower() == "json":
            content = content[4:].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        repaired = _JSON_ESCAPE_REPAIR_RE.sub(r"\\\\", content)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            start = repaired.find("{")
            end = repaired.rfind("}")
            if start >= 0 and end > start:
                return json.loads(repaired[start : end + 1])
            raise


def _call_chat_completion(config: LLMDiscoveryConfig, prompt: str) -> Dict[str, Any]:
    url = config.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    req_data = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=req_data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=config.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < MAX_RATE_LIMIT_RETRIES:
                time.sleep(RATE_LIMIT_BACKOFF_SECONDS * (2 ** attempt))
                attempt += 1
                continue
            raise

    content = body["choices"][0]["message"]["content"]
    return _extract_json_object(content)


def _safe_int(value: Any, default: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, number)


def _validated_findings(response: Dict[str, Any], known_files: set) -> List[Tuple[str, str, str, Occurrence]]:
    findings: List[Tuple[str, str, str, Occurrence]] = []
    components = response.get("components", [])
    if not isinstance(components, list):
        return findings

    for item in components:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip().upper()
        name = str(item.get("name", "")).strip()
        if category not in ALL_CATEGORIES or not name:
            continue

        file_name = str(item.get("file", "")).strip().replace("/", os.sep).replace("\\", os.sep)
        normalized_known = {f.replace("/", os.sep).replace("\\", os.sep) for f in known_files}
        if file_name not in normalized_known and known_files:
            continue

        confidence = str(item.get("confidence", CONFIDENCE_LOW)).strip().lower()
        if confidence not in ALLOWED_CONFIDENCE:
            confidence = CONFIDENCE_LOW

        deployment_target = str(item.get("deployment_target", DEPLOYMENT_UNKNOWN)).strip().lower()
        if deployment_target not in ALLOWED_DEPLOYMENT_TARGETS:
            deployment_target = DEPLOYMENT_UNKNOWN

        package = str(item.get("package", "")).strip()
        detail = str(item.get("detail", "")).strip()[:300]
        match_type = str(item.get("match_type", "llm_discovery")).strip() or "llm_discovery"
        if match_type != "llm_discovery":
            match_type = "llm_discovery"

        findings.append(
            (
                category,
                name,
                package,
                Occurrence(
                    file=file_name,
                    line=_safe_int(item.get("line", 1)),
                    match_type=match_type,
                    confidence=confidence,
                    detail=detail,
                    deployment_target=deployment_target,
                ),
            )
        )
    return findings


def discover_components(root: str, config: LLMDiscoveryConfig) -> Tuple[ScanResult, List[str]]:
    """Run LLM-only AI-stack discovery and return a ScanResult plus warnings."""
    root = os.path.abspath(root)
    result = ScanResult(
        root=root,
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        scanned_files=0,
        scanner_mode="llm",
    )
    warnings: List[str] = []

    snippets, scanned_python_files, skipped = collect_evidence(root)
    result.scanned_files = scanned_python_files
    result.skipped_files.extend(skipped)
    if not snippets:
        warnings.append("LLM discovery found no candidate snippets to review.")
        return result, warnings

    try:
        response = _call_chat_completion(config, _build_prompt(snippets))
    except urllib.error.URLError as exc:
        warnings.append(f"LLM discovery request failed ({exc})")
        return result, warnings
    except TimeoutError as exc:
        warnings.append(f"LLM discovery request timed out ({exc})")
        return result, warnings
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        warnings.append(f"Could not parse LLM discovery response ({exc})")
        return result, warnings

    known_files = {snippet.file for snippet in snippets}
    for category, name, package, occurrence in _validated_findings(response, known_files):
        result.add(category, name, package, occurrence)

    return result, warnings
