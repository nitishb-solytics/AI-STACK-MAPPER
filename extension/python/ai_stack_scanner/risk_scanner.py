"""Code assessment risk scanner and optional LLM control generation."""
from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
import datetime
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


IGNORE_DIRS = {
    ".git", ".hg", ".svn", "venv", ".venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".tox", "site-packages", ".idea", ".vscode-test",
}

SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
AI_PATH_HINTS = ("llm", "prompt", "agent", "rag", "retrieval", "texttosql", "text_to_sql", "query")
PROMPT_WORDS = ("prompt", "system_message", "human_message", "template", "instruction")
USER_INPUT_WORDS = ("question", "query", "user_input", "user_query", "request", "input_text")
SQL_VALIDATION_WORDS = ("sqlparse", "validate_sql", "is_safe_sql", "allowlist", "read_only", "readonly")
READ_ONLY_WORDS = (
    "read-only", "readonly", "select only", "only select", "select statement",
    "select query", "single select", "no insert", "no update", "no delete", "no drop",
    "do not use insert", "do not use insert/update/delete", "never use insert",
    "clean select", "valid postgresql select", "single, safe sql",
)
MUTATING_SQL_WORDS = ("insert", "update", "delete", "drop", "alter", "truncate", "create", "merge")
AGENT_CONSTRUCTORS = {"Agent", "Crew", "AssistantAgent", "GroupChat", "GroupChatManager", "AgentExecutor"}
LLM_CONSTRUCTORS = {
    "OpenAI", "AzureOpenAI", "AsyncOpenAI", "Anthropic", "AsyncAnthropic", "ChatOpenAI",
    "ChatAnthropic", "ChatGoogleGenerativeAI", "ChatOllama", "ChatBedrock", "ChatBedrockConverse",
    "BedrockChat", "Bedrock", "GenerativeModel", "Client",
}
VECTOR_CONSTRUCTORS = {"Milvus", "Chroma", "Pinecone", "Qdrant", "Weaviate", "FAISS", "LanceDB"}
VECTOR_SEARCH_METHODS = {"similarity_search", "similarity_search_with_score", "search", "query", "retrieve", "as_retriever"}
TOOL_DECORATOR_NAMES = {"tool", "function_tool"}
RISK_FUNCTION_NAMES = {
    "eval", "exec", "compile", "open", "remove", "unlink", "rmdir", "removedirs", "rmtree",
    "system", "popen", "run", "call", "check_call", "check_output",
}

DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
LLM_REQUEST_TIMEOUT_SECONDS = 45
MAX_LLM_FILES = 12
MAX_LLM_SNIPPET_CHARS = 1800
MAX_LLM_CONTROL_FINDINGS = 25
MAX_LLM_RETRIES = 2

llm_risk_control_SYSTEM_PROMPT = """
You are an AI risk-control reviewer for repository scans.
Review only the provided static findings and code snapshots. Do not invent files, functions, or new risks.
Your job is not to add findings. Your job is to explain whether each static finding is valid and provide
the best control/remediation for that exact code.

Return strict JSON only, no markdown, with exactly this shape:
{
  "controls": [
    {
      "finding_index": 1,
      "is_valid_risk": true,
      "risk_explanation": "why this exact code is risky, or why it is controlled already",
      "recommended_control": "specific actionable control for this code",
      "safer_code": "short safer code example, or empty string if not useful",
      "confidence": "low|medium|high"
    }
  ]
}

Return one controls item per provided finding_index when possible. If the snippet shows the risk is already
controlled, set is_valid_risk=false and explain the existing control. Keep safer_code compact.
""".strip()


@dataclass
class RiskFinding:
    severity: str
    area: str
    file: str
    line: int
    title: str
    suggestion: str
    rule_id: str
    feature: str = "general"
    source: str = "static"
    control_source: str = "static"
    risk_explanation: str = ""
    recommended_control: str = ""
    safer_code: str = ""
    llm_confidence: str = ""
    is_valid_risk: bool = True
    evidence_snippet: str = ""

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class RiskScanResult:
    root: str
    generated_at: str
    scanned_files: int
    changed_only: bool
    fail_on: str
    status: str
    findings: List[RiskFinding]
    skipped_files: List[str]
    risk_scan_mode: str = "static"
    llm_model: str = ""
    llm_warnings: Optional[List[str]] = None
    report_title: str = "AI Risk Report"

    def to_dict(self) -> Dict[str, object]:
        counts: Dict[str, int] = {key: 0 for key in SEVERITY_ORDER}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return {
            "root": self.root,
            "generated_at": self.generated_at,
            "scanned_files": self.scanned_files,
            "changed_only": self.changed_only,
            "fail_on": self.fail_on,
            "status": self.status,
            "risk_scan_mode": self.risk_scan_mode,
            "report_title": self.report_title,
            "llm_model": self.llm_model,
            "severity_counts": counts,
            "findings": [f.to_dict() for f in self.findings],
            "skipped_files": self.skipped_files,
            "llm_warnings": self.llm_warnings or [],
        }


@dataclass
class RiskLLMConfig:
    base_url: str = DEFAULT_LLM_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_LLM_MODEL
    timeout: int = LLM_REQUEST_TIMEOUT_SECONDS


def scan_risks(
    root: str,
    changed_only: bool = False,
    fail_on: str = "high",
    base_ref: str = "",
    head_ref: str = "",
    llm_config: Optional[RiskLLMConfig] = None,
    report_title: str = "AI Risk Report",
) -> RiskScanResult:
    root = os.path.abspath(root)
    selected_files = _changed_python_files(root, base_ref, head_ref) if changed_only else None
    findings: List[RiskFinding] = []
    skipped_files: List[str] = []
    reviewed_sources: List[Tuple[str, str]] = []
    source_by_rel: Dict[str, str] = {}
    scanned_files = 0

    for path in _iter_python_files(root, selected_files):
        rel = os.path.relpath(path, root)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                source = handle.read()
        except OSError:
            skipped_files.append(rel)
            continue

        scanned_files += 1
        source_by_rel[rel] = source
        findings.extend(_review_file(rel, source))
        if _is_llm_risk_control_candidate(rel, source):
            reviewed_sources.append((rel, source))

    llm_warnings: List[str] = []
    if llm_config is not None:
        llm_warnings = _enrich_findings_with_llm_controls(source_by_rel, findings, llm_config)

    threshold = SEVERITY_ORDER.get(fail_on, SEVERITY_ORDER["high"])
    should_fail = any(SEVERITY_ORDER.get(f.severity, 0) >= threshold for f in findings)
    status = "failed" if should_fail else "passed"
    findings.sort(key=lambda f: (-SEVERITY_ORDER.get(f.severity, 0), f.file, f.line, f.rule_id))

    return RiskScanResult(
        root=root,
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        scanned_files=scanned_files,
        changed_only=changed_only,
        fail_on=fail_on,
        status=status,
        findings=findings,
        skipped_files=skipped_files,
        risk_scan_mode="static+llm" if llm_config is not None else "static",
        llm_model=llm_config.model if llm_config is not None else "",
        llm_warnings=llm_warnings,
        report_title=report_title,
    )


def render_risk_markdown(data: Dict[str, object]) -> str:
    findings = data.get("findings", [])
    counts = data.get("severity_counts", {})
    lines = [
        f"# {data.get('report_title') or 'AI Risk Report'}",
        "",
        f"_Generated: {data['generated_at']}_  ",
        f"_Status: {str(data['status']).upper()}_  ",
        f"_Scanned {data['scanned_files']} Python file(s). Fail threshold: {data['fail_on']}._  ",
        f"_Risk scan mode: {data.get('risk_scan_mode', 'static')}_",
    ]
    if data.get("llm_model"):
        lines.append(f"_LLM model: {data['llm_model']}_")
    lines.extend([
        "",
        "## Summary",
        "",
        "| Severity | Count |",
        "|---|---:|",
    ])
    for severity in ("critical", "high", "medium", "low", "info"):
        lines.append(f"| {severity.title()} | {counts.get(severity, 0)} |")
    lines.append("")

    if not findings:
        lines.append("_No code assessment risk findings detected._")
        lines.append("")
        return "\n".join(lines)

    lines.extend([
        "## Findings",
        "",
        "| Severity | Source | Control | Feature | Rule | Area | Location | Finding | Recommended control |",
        "|---|---|---|---|---|---|---|---|---|",
    ])
    for finding in findings:
        location = f"`{finding['file']}:{finding['line']}`"
        if data.get("risk_scan_mode") == "static+llm":
            control = finding.get("recommended_control") or ""
        else:
            control = finding.get("recommended_control") or finding.get("suggestion", "")
        lines.append(
            f"| {finding['severity'].title()} | {finding.get('source', 'static')} | "
            f"{finding.get('control_source', 'static')} | "
            f"{finding.get('feature', 'general')} | `{finding.get('rule_id', '')}` | "
            f"{_md_cell(finding['area'])} | {location} | "
            f"{_md_cell(finding['title'])} | {_md_cell(control)} |"
        )

    enriched = [
        finding for finding in findings
        if finding.get("control_source") == "llm"
        and (finding.get("risk_explanation") or finding.get("safer_code"))
    ]
    if enriched:
        lines.extend(["", "## LLM Risk Controls", ""])
        for index, finding in enumerate(enriched, start=1):
            lines.extend([
                f"### {index}. {finding['severity'].title()} - `{finding.get('rule_id', '')}`",
                "",
                f"Location: `{finding['file']}:{finding['line']}`  ",
                f"LLM confidence: {finding.get('llm_confidence', 'medium')}  ",
                f"Valid risk according to LLM: {finding.get('is_valid_risk', True)}",
                "",
            ])
            if finding.get("risk_explanation"):
                lines.extend(["Risk explanation:", "", str(finding["risk_explanation"]), ""])
            if finding.get("recommended_control"):
                lines.extend(["Recommended control:", "", str(finding["recommended_control"]), ""])
            if finding.get("safer_code"):
                lines.extend(["Safer code example:", "", "```python", str(finding["safer_code"]), "```", ""])

    warnings = data.get("llm_warnings") or []
    if warnings:
        lines.extend(["", "## LLM Control Warnings", ""])
        for warning in warnings:
            lines.append(f"- {warning}")

    return "\n".join(lines)


def _iter_python_files(root: str, selected_files: Optional[Sequence[str]]) -> Iterable[str]:
    selected = None if selected_files is None else {p.replace("/", os.sep) for p in selected_files}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.endswith(".egg-info")]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, root)
            if selected is not None and rel not in selected:
                continue
            yield path


def _changed_python_files(root: str, base_ref: str, head_ref: str) -> List[str]:
    candidates = []
    if base_ref:
        base = base_ref
        if not base.startswith(("origin/", "refs/")):
            base = f"origin/{base}"
        candidates.append([base + "...HEAD"])
    if base_ref and head_ref:
        candidates.append([base_ref + "..." + head_ref])
    candidates.append(["--cached"])
    candidates.append([])

    for args in candidates:
        command = ["git", "-C", root, "diff", "--name-only"] + args
        try:
            completed = subprocess.run(command, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError):
            continue
        files = [
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip().endswith(".py") and not _is_ignored_path(line.strip())
        ]
        if files:
            return files
    return []


def _review_file(rel: str, source: str) -> List[RiskFinding]:
    lines = source.splitlines()
    lower_source = source.lower()
    findings: List[RiskFinding] = []
    is_text_to_sql = _is_text_to_sql_file(rel, lower_source)
    is_ai_file = is_text_to_sql or any(hint in rel.lower() for hint in AI_PATH_HINTS)

    if is_text_to_sql:
        findings.extend(_review_text_to_sql(rel, lines, lower_source))
    if is_ai_file:
        findings.extend(_review_ai_general(rel, lines))
    findings.extend(_review_ai_ast(rel, source))

    return findings


class _AIRiskAstVisitor(ast.NodeVisitor):
    def __init__(self, rel: str, source: str):
        self.rel = rel
        self.source = source
        self.findings: List[RiskFinding] = []
        self.import_map: Dict[str, str] = {}
        self.ai_symbols: Dict[str, str] = {}
        self.message_symbols: Dict[str, int] = {}
        self._scope_stack: List[str] = []

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.import_map[alias.asname or alias.name.split(".")[0]] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if not node.module:
            self.generic_visit(node)
            return
        for alias in node.names:
            self.import_map[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        if isinstance(node.value, ast.Call):
            component = self._classify_call(node.value)
            if component:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.ai_symbols[target.id] = component
        if _looks_like_message_list_assignment(node):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.message_symbols[target.id] = node.lineno
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._review_tool_function(node)
        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._review_tool_function(node)
        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_Call(self, node: ast.Call):
        component = self._classify_call(node)
        if component == "LLM":
            self._review_llm_constructor(node)
        elif component == "Agent":
            self._review_agent_constructor(node)
        elif component == "Vector":
            self._review_vector_constructor(node)

        if self._is_llm_usage_call(node):
            self._review_llm_usage(node)
        if self._is_vector_search_call(node):
            self._review_vector_search(node)
        if self._is_sql_execution_call(node):
            self._review_sql_execution_call(node)
        if self._is_message_append_call(node):
            self._review_message_append(node)

        self.generic_visit(node)

    def _review_tool_function(self, node: ast.AST):
        decorators = getattr(node, "decorator_list", [])
        if not any(_decorator_name(dec) in TOOL_DECORATOR_NAMES or _decorator_attr(dec) == "tool" for dec in decorators):
            return

        args = getattr(node, "args", None)
        untyped = []
        if args:
            for arg in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
                if arg.arg in {"self", "cls"}:
                    continue
                if arg.annotation is None:
                    untyped.append(arg.arg)

        if untyped:
            self._add(
                "medium",
                "Tool input validation",
                node.lineno,
                "Tool function has parameters without type annotations.",
                "Add explicit type annotations or a schema model so agent/tool inputs are validated before execution.",
                "tool-input-schema-missing",
                feature="Tool",
            )

        if _function_contains_risky_call(node):
            self._add(
                "high",
                "Tool safety",
                node.lineno,
                "Tool function performs potentially sensitive file/process operation.",
                "Add strict input validation, allowlists, and path/command restrictions before exposing this as an agent tool.",
                "tool-dangerous-capability",
                feature="Tool",
            )

    def _review_llm_constructor(self, node: ast.Call):
        if not _has_any_keyword(node, {"timeout", "request_timeout", "max_retries"}):
            self._add(
                "medium",
                "LLM reliability",
                node.lineno,
                "LLM client/model is created without visible timeout or retry configuration.",
                "Configure timeout and retry/fallback behavior on the client/model or wrapper.",
                "llm-constructor-timeout-retry-missing",
                feature="LLM",
            )

        if not _has_any_keyword(node, {"max_tokens", "max_output_tokens", "max_completion_tokens"}):
            self._add(
                "low",
                "LLM cost/control",
                node.lineno,
                "LLM client/model is created without visible output token limit.",
                "Set max_tokens/max_output_tokens where supported to reduce runaway cost and oversized responses.",
                "llm-max-tokens-missing",
                feature="LLM",
            )

    def _review_llm_usage(self, node: ast.Call):
        if not _has_any_keyword(node, {"timeout", "request_timeout"}):
            self._add(
                "medium",
                "LLM reliability",
                node.lineno,
                "LLM call does not show a timeout at the call site.",
                "Pass a timeout/request_timeout or ensure the wrapped client enforces one.",
                "llm-call-timeout-missing",
                feature="LLM",
            )

        prompt_node = _keyword_value(node, {"messages", "prompt", "input", "instructions"})
        if prompt_node is not None and _node_mentions_user_input(prompt_node):
            self._add(
                "medium",
                "Prompt injection",
                node.lineno,
                "LLM call appears to pass user-controlled input directly into prompt/messages.",
                "Separate trusted system instructions from user content and add sanitization/guardrails before model invocation.",
                "llm-user-input-direct-to-prompt",
                feature="Prompt",
            )

    def _review_agent_constructor(self, node: ast.Call):
        if not _has_any_keyword(node, {"max_iter", "max_iterations", "max_execution_time"}):
            self._add(
                "medium",
                "Agent control",
                node.lineno,
                "Agent/Crew is created without visible iteration or execution-time limit.",
                "Set max_iter/max_iterations/max_execution_time to prevent runaway agent loops.",
                "agent-loop-bound-missing",
                feature="Agent",
            )

        tools_node = _keyword_value(node, {"tools"})
        if tools_node is not None and _literal_list_len(tools_node) >= 4:
            self._add(
                "medium",
                "Agent tool scope",
                node.lineno,
                "Agent is configured with a broad tool list.",
                "Limit tools per agent/task and validate tool outputs before passing them back to the LLM.",
                "agent-too-many-tools",
                feature="Agent",
            )

        if _keyword_bool(node, "allow_delegation") is True and not _has_any_keyword(node, {"max_iter", "max_iterations"}):
            self._add(
                "high",
                "Agent delegation",
                node.lineno,
                "Agent delegation is enabled without visible iteration bounds.",
                "Add strict delegation limits and task/tool guardrails before enabling autonomous delegation.",
                "agent-delegation-unbounded",
                feature="Agent",
            )

        verbose_value = _keyword_bool(node, "verbose")
        if verbose_value is True:
            self._add(
                "low",
                "Agent observability",
                node.lineno,
                "Agent verbose logging is enabled.",
                "Ensure logs do not expose prompts, retrieved context, credentials, or customer data.",
                "agent-verbose-logging",
                feature="Agent",
            )

    def _review_vector_constructor(self, node: ast.Call):
        if not _has_any_keyword(node, {"metadata_field", "metadata_schema", "collection_metadata"}):
            self._add(
                "low",
                "Vector store governance",
                node.lineno,
                "Vector store initialization does not show metadata governance.",
                "Store source, tenant, document type, and permission metadata so retrieval can be filtered safely.",
                "vector-metadata-governance-missing",
                feature="RAG",
            )

    def _review_vector_search(self, node: ast.Call):
        k_node = _keyword_value(node, {"k", "top_k", "limit"})
        if k_node is None:
            self._add(
                "medium",
                "RAG retrieval control",
                node.lineno,
                "Vector retrieval/search call does not specify k/top_k/limit.",
                "Set an explicit retrieval limit and tune it per use case.",
                "rag-top-k-missing",
                feature="RAG",
            )
        elif isinstance(k_node, ast.Constant) and isinstance(k_node.value, int) and k_node.value > 20:
            self._add(
                "medium",
                "RAG retrieval control",
                node.lineno,
                "Vector retrieval/search uses a high top_k/limit.",
                "Use a smaller top_k or add re-ranking/score filtering to avoid noisy context and context-window growth.",
                "rag-top-k-too-large",
                feature="RAG",
            )

        if not _has_any_keyword(node, {"score_threshold", "similarity_threshold", "filter", "expr", "where"}):
            self._add(
                "low",
                "RAG grounding",
                node.lineno,
                "Vector retrieval/search does not show score threshold or metadata filter.",
                "Add score thresholds and metadata filters for tenant/permission/source-aware retrieval.",
                "rag-filter-threshold-missing",
                feature="RAG",
            )

    def _review_sql_execution_call(self, node: ast.Call):
        if _call_has_generated_sql_arg(node):
            self._add(
                "high",
                "TextToSQL validation",
                node.lineno,
                "Generated SQL-like value appears to be executed directly.",
                "Validate with a SQL parser/allowlist, enforce SELECT-only, and apply row limits before execution.",
                "texttosql-generated-sql-executed",
                feature="TextToSQL",
            )

    def _review_message_append(self, node: ast.Call):
        root = _call_root_name(node)
        if not root or root not in self.message_symbols:
            return
        if not _scope_has_trim_or_limit(self._scope_stack, self.source):
            self._add(
                "medium",
                "Context window growth",
                node.lineno,
                "Message/history list is appended before LLM usage without visible trimming or summarization.",
                "Apply token budgeting, summarization, or last-N message windowing before sending history to the model.",
                "llm-context-growth-unbounded",
                feature="LLM",
            )

    def _classify_call(self, node: ast.Call) -> str:
        name = _call_name(node)
        if name in AGENT_CONSTRUCTORS:
            return "Agent"
        if name in LLM_CONSTRUCTORS:
            return "LLM"
        if name in VECTOR_CONSTRUCTORS:
            return "Vector"

        dotted = _dotted_name(node.func) if isinstance(node.func, ast.Attribute) else ""
        if dotted:
            lower = dotted.lower()
            if any(word in lower for word in ("openai", "anthropic", "bedrock", "gemini", "llm")):
                return "LLM"
            if any(word in lower for word in ("milvus", "chroma", "pinecone", "qdrant", "weaviate", "vector")):
                return "Vector"
        return ""

    def _is_llm_usage_call(self, node: ast.Call) -> bool:
        name = _call_name(node)
        dotted = (_dotted_name(node.func) or "").lower()
        if name in {"invoke", "predict", "generate", "complete", "completion", "create", "chat"}:
            if any(word in dotted for word in ("llm", "model", "chat", "completion", "openai", "anthropic", "bedrock")):
                return True
            root = _call_root_name(node)
            return bool(root and root in self.ai_symbols and self.ai_symbols[root] == "LLM")
        return False

    def _is_vector_search_call(self, node: ast.Call) -> bool:
        name = _call_name(node)
        if name not in VECTOR_SEARCH_METHODS:
            return False
        dotted = (_dotted_name(node.func) or "").lower()
        if any(word in dotted for word in ("vector", "retriever", "milvus", "chroma", "pinecone", "qdrant", "weaviate")):
            return True
        root = _call_root_name(node)
        return bool(root and root in self.ai_symbols and self.ai_symbols[root] == "Vector")

    def _is_sql_execution_call(self, node: ast.Call) -> bool:
        name = _call_name(node)
        dotted = (_dotted_name(node.func) or "").lower()
        return name in {"execute", "raw"} or dotted.endswith(".execute") or dotted.endswith(".raw")

    def _is_message_append_call(self, node: ast.Call) -> bool:
        return _call_name(node) in {"append", "extend"} and bool(_call_root_name(node))

    def _add(
        self,
        severity: str,
        area: str,
        line: int,
        title: str,
        suggestion: str,
        rule_id: str,
        feature: str = "general",
    ):
        self.findings.append(RiskFinding(
            severity=severity,
            area=area,
            file=self.rel,
            line=line,
            title=title,
            suggestion=suggestion,
            rule_id=rule_id,
            feature=feature,
        ))


def _enrich_findings_with_llm_controls(
    source_by_rel: Dict[str, str],
    static_findings: Sequence[RiskFinding],
    config: RiskLLMConfig,
) -> List[str]:
    warnings: List[str] = []
    if not config.api_key:
        return ["LLM risk controls requested but no API key was provided."]
    if not static_findings:
        return []

    evidence, indexed_findings = _build_llm_control_evidence(source_by_rel, static_findings)
    if not indexed_findings:
        return ["LLM control enrichment skipped because no code snapshots were available."]
    try:
        response = _call_quality_llm(config, evidence)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        _mark_llm_control_fallback(indexed_findings)
        return [f"LLM control request failed: {exc}"]
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        _mark_llm_control_fallback(indexed_findings)
        return [f"Could not parse LLM control response: {exc}"]

    raw_controls = response.get("controls", [])
    if not isinstance(raw_controls, list):
        _mark_llm_control_fallback(indexed_findings)
        return ["LLM control response did not contain a controls array."]

    by_index = {index: finding for index, finding in indexed_findings}
    for position, item in enumerate(raw_controls, start=1):
        if not isinstance(item, dict):
            warnings.append(f"LLM control {position} was not an object; skipped.")
            continue
        finding_index = _safe_positive_int(item.get("finding_index"), default=-1)
        finding = by_index.get(finding_index)
        if finding is None:
            warnings.append(f"LLM control {position} referenced unknown finding_index {finding_index}; skipped.")
            continue

        is_valid = item.get("is_valid_risk")
        finding.is_valid_risk = bool(is_valid) if isinstance(is_valid, bool) else True
        finding.control_source = "llm"
        finding.risk_explanation = _clean_llm_text(
            item.get("risk_explanation"),
            finding.title,
        )
        finding.recommended_control = _clean_llm_text(
            item.get("recommended_control"),
            finding.suggestion,
        )
        finding.safer_code = _clean_llm_code(item.get("safer_code"))
        confidence = str(item.get("confidence", "")).strip().lower()
        finding.llm_confidence = confidence if confidence in {"low", "medium", "high"} else "medium"

    for _index, finding in indexed_findings:
        if finding.control_source == "static":
            finding.control_source = "static_fallback"

    return warnings


def _mark_llm_control_fallback(indexed_findings: Sequence[Tuple[int, RiskFinding]]) -> None:
    for _index, finding in indexed_findings:
        if finding.control_source == "static":
            finding.control_source = "static_fallback"


def _review_ai_ast(rel: str, source: str) -> List[RiskFinding]:
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError:
        return []
    visitor = _AIRiskAstVisitor(rel, source)
    visitor.visit(tree)
    return _dedupe_findings(visitor.findings)


def _dedupe_findings(findings: Sequence[RiskFinding]) -> List[RiskFinding]:
    seen = set()
    unique: List[RiskFinding] = []
    for finding in findings:
        key = (finding.rule_id, finding.file, finding.line, finding.title)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def _dotted_name(node: ast.AST) -> str:
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _call_root_name(node: ast.Call) -> str:
    dotted = _dotted_name(node.func)
    return dotted.split(".")[0] if dotted else ""


def _decorator_name(node: ast.AST) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    return ""


def _decorator_attr(node: ast.AST) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _has_any_keyword(node: ast.Call, names: set) -> bool:
    return any(kw.arg in names for kw in node.keywords if kw.arg)


def _keyword_value(node: ast.Call, names: set) -> Optional[ast.AST]:
    for kw in node.keywords:
        if kw.arg in names:
            return kw.value
    return None


def _keyword_bool(node: ast.Call, name: str) -> Optional[bool]:
    value = _keyword_value(node, {name})
    if isinstance(value, ast.Constant) and isinstance(value.value, bool):
        return value.value
    return None


def _literal_list_len(node: ast.AST) -> int:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return len(node.elts)
    return -1


def _node_mentions_user_input(node: ast.AST) -> bool:
    text = ""
    try:
        text = ast.unparse(node).lower()
    except Exception:
        return False
    return any(word in text for word in USER_INPUT_WORDS)


def _looks_like_message_list_assignment(node: ast.Assign) -> bool:
    if not isinstance(node.value, ast.List):
        return False
    for target in node.targets:
        if isinstance(target, ast.Name) and any(word in target.id.lower() for word in ("message", "history", "conversation")):
            return True
    return False


def _function_contains_risky_call(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = _call_name(child)
        dotted = _dotted_name(child.func).lower()
        if name in RISK_FUNCTION_NAMES:
            return True
        if any(dotted.endswith("." + risky) for risky in RISK_FUNCTION_NAMES):
            return True
        if dotted.startswith(("subprocess.", "os.", "shutil.", "pathlib.")) and name in RISK_FUNCTION_NAMES:
            return True
    return False


def _call_has_generated_sql_arg(node: ast.Call) -> bool:
    candidates = list(node.args) + [kw.value for kw in node.keywords]
    for candidate in candidates:
        try:
            text = ast.unparse(candidate).lower()
        except Exception:
            continue
        if any(word in text for word in ("generated_sql", "sql_query", "query_sql", "llm_sql", "sql")):
            return True
    return False


def _scope_has_trim_or_limit(scope_stack: Sequence[str], source: str) -> bool:
    # Conservative file-level check. It intentionally favors fewer false
    # positives until we add proper data-flow/scope slicing.
    lower = source.lower()
    return any(
        word in lower
        for word in (
            "max_tokens", "token_budget", "trim", "truncate", "summarize", "summary",
            "last_n", "[-", "deque(", "maxlen", "num_tokens", "tiktoken",
        )
    )


def _build_llm_control_evidence(
    source_by_rel: Dict[str, str],
    static_findings: Sequence[RiskFinding],
) -> Tuple[str, List[Tuple[int, RiskFinding]]]:
    blocks = []
    indexed_findings: List[Tuple[int, RiskFinding]] = []
    for index, finding in enumerate(static_findings[:MAX_LLM_CONTROL_FINDINGS], start=1):
        source = source_by_rel.get(finding.file)
        if source is None:
            source = source_by_rel.get(finding.file.replace("/", os.sep))
        if source is None:
            continue

        snippet = _extract_finding_snapshot(source, finding.line)
        finding.evidence_snippet = snippet
        indexed_findings.append((index, finding))
        blocks.append(
            "\n".join([
                f"FINDING_INDEX: {index}",
                f"RULE_ID: {finding.rule_id}",
                f"SEVERITY: {finding.severity}",
                f"FEATURE: {finding.feature}",
                f"AREA: {finding.area}",
                f"FILE: {finding.file.replace(os.sep, '/')}",
                f"LINE: {finding.line}",
                f"STATIC_FINDING: {finding.title}",
                f"STATIC_FALLBACK_CONTROL: {finding.suggestion}",
                "CODE_SNAPSHOT:",
                "```python",
                snippet,
                "```",
            ])
        )

    evidence = "\n\n---\n\n".join([
        "For each static finding below, return one JSON controls item with the same FINDING_INDEX.",
        *blocks,
    ])
    return evidence, indexed_findings


def _extract_finding_snapshot(source: str, line: int, context: int = 8) -> str:
    lines = source.splitlines()
    if not lines:
        return ""
    target = max(1, line)
    start = max(1, target - context)
    end = min(len(lines), target + context)

    # If the finding is inside a function/class, include the full surrounding
    # block when it is still compact. That gives the LLM enough control context
    # without sending a whole file.
    try:
        tree = ast.parse(source)
        container = _smallest_container_for_line(tree, target)
        if container is not None:
            node_start = getattr(container, "lineno", start)
            node_end = getattr(container, "end_lineno", end)
            if node_end and (node_end - node_start) <= 80:
                start = min(start, node_start)
                end = max(end, node_end)
    except SyntaxError:
        pass

    snippet = "\n".join(f"{line_no}: {lines[line_no - 1]}" for line_no in range(start, end + 1))
    if len(snippet) > MAX_LLM_SNIPPET_CHARS:
        return snippet[:MAX_LLM_SNIPPET_CHARS] + "\n...<truncated>"
    return snippet


def _smallest_container_for_line(tree: ast.AST, line: int) -> Optional[ast.AST]:
    best = None
    best_size = 10**9
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if start is None or end is None or not (start <= line <= end):
            continue
        size = end - start
        if size < best_size:
            best = node
            best_size = size
    return best


def _build_llm_risk_control_evidence(
    reviewed_sources: Sequence[Tuple[str, str]],
    static_findings: Sequence[RiskFinding],
) -> str:
    static_lines = []
    for finding in static_findings[:30]:
        static_lines.append(
            f"- {finding.severity} {finding.file}:{finding.line} "
            f"[{finding.area}] {finding.title} Suggestion: {finding.suggestion}"
        )

    snippet_blocks = []
    for rel, source in reviewed_sources[:MAX_LLM_FILES]:
        snippet = _select_relevant_snippet(source)
        snippet_blocks.append(f"FILE: {rel.replace(os.sep, '/')}\n```python\n{snippet}\n```")

    return "\n\n".join([
        "Static findings:",
        "\n".join(static_lines) if static_lines else "None.",
        "Code snippets:",
        "\n\n".join(snippet_blocks),
    ])


def _select_relevant_snippet(source: str) -> str:
    lines = source.splitlines()
    matched_indexes = []
    needles = AI_PATH_HINTS + PROMPT_WORDS + ("invoke(", "execute(", "raw(", "schema", "sql", "tool")
    for index, line in enumerate(lines):
        lower = line.lower()
        if any(needle in lower for needle in needles):
            matched_indexes.append(index)

    if not matched_indexes:
        snippet = "\n".join(lines[:80])
    else:
        ranges = []
        for index in matched_indexes[:8]:
            start = max(0, index - 6)
            end = min(len(lines), index + 12)
            ranges.append((start, end))
        merged = []
        for start, end in ranges:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        selected = []
        for start, end in merged:
            selected.extend(f"{line_no + 1}: {lines[line_no]}" for line_no in range(start, end))
        snippet = "\n".join(selected)

    if len(snippet) > MAX_LLM_SNIPPET_CHARS:
        return snippet[:MAX_LLM_SNIPPET_CHARS] + "\n...<truncated>"
    return snippet


def _call_quality_llm(config: RiskLLMConfig, evidence: str) -> Dict[str, object]:
    url = config.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": llm_risk_control_SYSTEM_PROMPT},
            {"role": "user", "content": evidence},
        ],
        "temperature": 0,
    }
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    data = json.dumps(payload).encode("utf-8")

    last_error = None
    for attempt in range(MAX_LLM_RETRIES + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=config.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            if "error" in body:
                raise ValueError(f"LLM API error response: {body['error']}")
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError(f"LLM API response did not include choices: {str(body)[:500]}")
            content = choices[0]["message"]["content"].strip()
            return _parse_llm_json(content)
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_LLM_RETRIES:
                time.sleep(2 ** attempt)
                continue
            raise
    if last_error:
        raise last_error
    raise ValueError("LLM request failed without an error response")


def _parse_llm_json(content: str) -> Dict[str, object]:
    if content.startswith("```"):
        content = content.strip("`")
        if content[:4].lower() == "json":
            content = content[4:].strip()
    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response was not a JSON object")
    return parsed


def _clean_llm_text(value: object, fallback: str) -> str:
    text = str(value or "").strip().replace("|", "/")
    return text or fallback


def _clean_llm_code(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:6].lower().startswith("python"):
            text = text[6:].strip()
    return text


def _md_cell(value: object) -> str:
    return str(value or "").replace("|", "/").replace("\n", "<br>")


def _safe_positive_int(value: object, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _is_llm_risk_control_candidate(rel: str, source: str) -> bool:
    lower_rel = rel.lower()
    lower_source = source.lower()
    return any(hint in lower_rel for hint in AI_PATH_HINTS) or any(
        word in lower_source
        for word in ("llm", "prompt", "invoke(", "openai", "anthropic", "bedrock", "langchain", "text2sql")
    )


def _review_text_to_sql(rel: str, lines: Sequence[str], lower_source: str) -> List[RiskFinding]:
    findings: List[RiskFinding] = []
    if _is_thin_module(lines):
        return findings

    has_sql_validation = any(word in lower_source for word in SQL_VALIDATION_WORDS)
    has_read_only_guardrail = any(word in lower_source for word in READ_ONLY_WORDS)
    has_generation_logic = any(
        word in lower_source
        for word in ("prompt", "final_prompt", "text2sql", "llm", "invoke(")
    )

    if has_generation_logic and not has_read_only_guardrail:
        prompt_line = _first_line_matching(lines, PROMPT_WORDS) or 1
        findings.append(RiskFinding(
            severity="high",
            area="TextToSQL safety",
            file=rel,
            line=prompt_line,
            title="TextToSQL prompt/code does not clearly enforce read-only SQL generation.",
            suggestion="Add explicit guardrails: SELECT-only SQL, schema-bound generation, and rejection of INSERT/UPDATE/DELETE/DROP/ALTER.",
            rule_id="texttosql-readonly-guardrail",
            feature="TextToSQL",
        ))

    if _has_sql_execution(lines) and not has_sql_validation:
        findings.append(RiskFinding(
            severity="high",
            area="TextToSQL validation",
            file=rel,
            line=_first_line_matching(lines, ("execute(", "raw(")) or 1,
            title="Generated SQL appears to be executed without a visible validation step.",
            suggestion="Validate generated SQL with an allowlist/parser before execution and block destructive statements.",
            rule_id="texttosql-sql-validation",
            feature="TextToSQL",
        ))

    if has_generation_logic and "schema" not in lower_source and any(word in lower_source for word in ("sql", "query")):
        findings.append(RiskFinding(
            severity="medium",
            area="TextToSQL grounding",
            file=rel,
            line=_first_line_matching(lines, ("sql", "query")) or 1,
            title="TextToSQL logic references SQL/query generation without visible schema grounding.",
            suggestion="Pass an explicit table/column schema or allowlist into the prompt and validator.",
            rule_id="texttosql-schema-grounding",
            feature="TextToSQL",
        ))

    return findings


def _review_ai_general(rel: str, lines: Sequence[str]) -> List[RiskFinding]:
    findings: List[RiskFinding] = []
    joined = "\n".join(lines).lower()

    for index, line in enumerate(lines, start=1):
        lower = line.lower()
        if _looks_like_prompt_interpolation(line):
            findings.append(RiskFinding(
                severity="medium",
                area="Prompt quality",
                file=rel,
                line=index,
                title="Prompt appears to interpolate user input directly.",
                suggestion="Separate trusted instructions from user content and add prompt-injection guardrails around the user-provided value.",
                rule_id="prompt-direct-user-input",
            ))
        if _looks_like_llm_call(lower) and "timeout" not in lower:
            findings.append(RiskFinding(
                severity="medium",
                area="LLM reliability",
                file=rel,
                line=index,
                title="LLM call does not show an inline timeout.",
                suggestion="Set request timeout and retry/fallback behavior near the model call or provider client.",
                rule_id="llm-timeout-missing",
            ))

    if _contains_prompt(joined) and not _contains_output_contract(joined):
        findings.append(RiskFinding(
            severity="low",
            area="Prompt quality",
            file=rel,
            line=_first_line_matching(lines, PROMPT_WORDS) or 1,
            title="Prompt-like content does not show a clear output contract.",
            suggestion="State the required output shape, for example JSON fields, markdown sections, or SQL-only output.",
            rule_id="prompt-output-contract",
        ))

    return findings


def _is_text_to_sql_file(rel: str, lower_source: str) -> bool:
    normalized = rel.replace("\\", "/").lower()
    return "texttosql" in normalized or "text_to_sql" in normalized or "text to sql" in lower_source


def _is_thin_module(lines: Sequence[str]) -> bool:
    meaningful_lines = [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]
    return bool(meaningful_lines) and all(
        line.startswith(("import ", "from ")) for line in meaningful_lines
    )


def _has_sql_execution(lines: Sequence[str]) -> bool:
    for line in lines:
        lower = line.lower()
        if ".execute(" in lower or ".raw(" in lower:
            return True
    return False


def _looks_like_prompt_interpolation(line: str) -> bool:
    lower = line.lower()
    if not any(word in lower for word in PROMPT_WORDS):
        return False
    if not any(word in lower for word in USER_INPUT_WORDS):
        return False
    return "{" in line or ".format(" in lower or "%" in line


def _looks_like_llm_call(lower_line: str) -> bool:
    call_words = ("chat(", "invoke(", "predict(", "generate(", "complete(", "completion(")
    provider_words = ("llm", "model", "openai", "anthropic", "bedrock", "chat")
    return any(call in lower_line for call in call_words) and any(word in lower_line for word in provider_words)


def _contains_prompt(lower_source: str) -> bool:
    return any(word in lower_source for word in PROMPT_WORDS)


def _contains_output_contract(lower_source: str) -> bool:
    return any(word in lower_source for word in ("json", "schema", "format", "return only", "output"))


def _first_line_matching(lines: Sequence[str], needles: Sequence[str]) -> Optional[int]:
    lowered_needles = tuple(n.lower() for n in needles)
    for index, line in enumerate(lines, start=1):
        lower = line.lower()
        if any(needle in lower for needle in lowered_needles):
            return index
    return None


def _is_ignored_path(path: str) -> bool:
    parts = re.split(r"[\\/]+", path)
    return any(part in IGNORE_DIRS or part.endswith(".egg-info") for part in parts)
