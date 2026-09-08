"""
Optional LLM-powered enrichment layer -- OFF by default, must be explicitly
enabled via `--enrich`.

Given the deterministic scan results, this asks an LLM (any OpenAI-compatible
chat completions endpoint -- OpenAI, Azure OpenAI, or a self-hosted/on-prem
gateway such as vLLM/Ollama/LiteLLM proxy) to infer, per component:
  - purpose:            what this component appears to be used for
  - usage_description:  a short human-readable description of how it's wired up
  - expected_output:    what kind of output/response it likely produces

This is fundamentally different from the rest of the engine: it is the only
part of ai-stack-scanner that makes an outbound network call, and it sends
code-derived context (enclosing function/class names, docstrings, and any
prompt/message text found in the code -- never full source files, never
secret values) to the configured LLM endpoint.

If code confidentiality matters, point --llm-base-url at an internal/
self-hosted model (vLLM, Ollama, LiteLLM proxy, Azure OpenAI in your own
tenant, etc.) instead of a public cloud endpoint -- this module doesn't care
which, as long as it speaks the OpenAI chat-completions JSON shape.

Results are probabilistic and unverified. Unlike the rest of the report,
they are not deterministic/reproducible -- treat `ai_enrichment` as a hint
for a human to confirm, not a fact.
"""
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional

from .models import Component

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
MAX_OCCURRENCES_PER_COMPONENT = 4
REQUEST_TIMEOUT_SECONDS = 30
MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = 2

# Matches a backslash NOT already part of a valid JSON escape sequence --
# used to repair a common LLM mistake: echoing a Windows-style file path
# (e.g. `agent_app\crew_agent.py`) back inside a JSON string value without
# escaping the backslash, which breaks strict JSON parsing.
_INVALID_JSON_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')

_SYSTEM_PROMPT = (
    "You are analyzing static-analysis findings from a codebase scanner. "
    "For the given AI-stack component, infer its likely purpose based ONLY "
    "on the provided evidence (file locations, enclosing function/class "
    "names, docstrings, and any prompt text). Do not invent details that "
    "aren't supported by the evidence. Respond with strict JSON only, no "
    "markdown code fences, matching exactly this shape: "
    '{"purpose": "...", "usage_description": "...", "expected_output": "..."}. '
    "If the evidence is too thin to infer a field, use an empty string for "
    "it rather than guessing."
)


@dataclass
class LLMConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    timeout: int = REQUEST_TIMEOUT_SECONDS


def _build_evidence_block(component: Component) -> str:
    """Assemble the (small, static-only) context sent to the LLM for one
    component -- file:line locations, match type, enclosing scope, and any
    prompt text already extracted during the AST walk. Never the raw source
    file itself."""
    lines = [
        f"Component: {component.name} "
        f"(category={component.category}, package={component.package or 'n/a'})"
    ]
    shown = sorted(component.occurrences, key=lambda o: (o.file, o.line))[:MAX_OCCURRENCES_PER_COMPONENT]
    for occ in shown:
        # Always forward slashes, even on Windows -- avoids the model
        # echoing a raw backslash path back into its own JSON response and
        # producing an invalid \escape (e.g. `agent_app\crew_agent.py` -> \c).
        file_display = occ.file.replace("\\", "/")
        parts = [f"- {file_display}:{occ.line} ({occ.match_type}, confidence={occ.confidence})"]
        if occ.context_hint:
            parts.append(f"  context: {occ.context_hint}")
        if occ.prompt_hint:
            parts.append(f'  prompt/text seen: "{occ.prompt_hint}"')
        if occ.detail:
            parts.append(f"  detail: {occ.detail}")
        lines.append("\n".join(parts))
    remaining = component.count - len(shown)
    if remaining > 0:
        lines.append(f"... and {remaining} more occurrence(s) not shown.")
    return "\n".join(lines)


def _call_chat_completion(config: LLMConfig, evidence: str) -> Optional[Dict[str, str]]:
    url = config.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": evidence},
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
                # Rate-limited (or, on some accounts, insufficient quota --
                # which OpenAI also surfaces as 429). Back off and retry a
                # few times before giving up on this component.
                time.sleep(RATE_LIMIT_BACKOFF_SECONDS * (2 ** attempt))
                attempt += 1
                continue
            raise
    content = body["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content[:4].lower() == "json":
            content = content[4:].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Best-effort repair for a stray unescaped backslash the model put
        # inside a JSON string value (e.g. a file path). If this second
        # attempt also fails, let the JSONDecodeError propagate as normal --
        # the caller treats it as a per-component enrichment failure, not a
        # fatal error.
        return json.loads(_INVALID_JSON_ESCAPE_RE.sub(r"\\\\", content))


def enrich_components(components: Dict[str, Component], config: LLMConfig) -> List[str]:
    """Mutates each Component in place, setting `.ai_enrichment`. Returns a
    list of human-readable warnings for components that failed (network
    error, bad JSON, etc.) so the caller can surface them without aborting
    the whole scan -- enrichment is best-effort and optional; a failure here
    must never break the deterministic scan it's layered on top of.
    """
    warnings: List[str] = []
    for component in components.values():
        evidence = _build_evidence_block(component)
        try:
            result = _call_chat_completion(config, evidence)
        except urllib.error.URLError as exc:
            warnings.append(f"{component.name}: LLM request failed ({exc})")
            continue
        except TimeoutError as exc:
            warnings.append(f"{component.name}: LLM request timed out ({exc})")
            continue
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            warnings.append(f"{component.name}: could not parse LLM response ({exc})")
            continue
        if not isinstance(result, dict):
            warnings.append(f"{component.name}: LLM response was not a JSON object")
            continue
        component.ai_enrichment = {
            "purpose": str(result.get("purpose", "")).strip(),
            "usage_description": str(result.get("usage_description", "")).strip(),
            "expected_output": str(result.get("expected_output", "")).strip(),
            "model": config.model,
        }
    return warnings
