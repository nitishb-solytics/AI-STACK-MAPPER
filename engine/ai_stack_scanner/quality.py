"""Static AI quality review rules for CI quality gates."""
from __future__ import annotations

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

DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"
LLM_REQUEST_TIMEOUT_SECONDS = 45
MAX_LLM_FILES = 12
MAX_LLM_SNIPPET_CHARS = 1800
MAX_LLM_RETRIES = 2

LLM_REVIEW_SYSTEM_PROMPT = """
You are an AI code quality reviewer for CI.
Review only the provided code snippets and static findings. Do not invent files, functions, or risks.
Focus on AI implementation quality: prompt safety, TextToSQL safety, generated SQL validation,
agent/tool validation, RAG grounding, LLM retry/timeout behavior, observability, and maintainability.

Return strict JSON only, no markdown, with exactly this shape:
{
  "findings": [
    {
      "severity": "low|medium|high|critical",
      "area": "short area",
      "file": "path from provided snippet",
      "line": 1,
      "title": "specific finding",
      "suggestion": "specific actionable suggestion",
      "feature": "TextToSQL|RAG|Agent|Prompt|general"
    }
  ]
}

Use high or critical only for issues that could plausibly cause unsafe execution, data leakage,
prompt injection, SQL mutation/destruction, serious hallucination risk, or broken production behavior.
If the snippets do not support a finding, return an empty findings array.
""".strip()


@dataclass
class QualityFinding:
    severity: str
    area: str
    file: str
    line: int
    title: str
    suggestion: str
    rule_id: str
    feature: str = "general"
    source: str = "static"

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class QualityReviewResult:
    root: str
    generated_at: str
    scanned_files: int
    changed_only: bool
    fail_on: str
    status: str
    findings: List[QualityFinding]
    skipped_files: List[str]
    review_mode: str = "static"
    llm_model: str = ""
    llm_warnings: Optional[List[str]] = None

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
            "review_mode": self.review_mode,
            "llm_model": self.llm_model,
            "severity_counts": counts,
            "findings": [f.to_dict() for f in self.findings],
            "skipped_files": self.skipped_files,
            "llm_warnings": self.llm_warnings or [],
        }


@dataclass
class QualityLLMConfig:
    base_url: str = DEFAULT_LLM_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_LLM_MODEL
    timeout: int = LLM_REQUEST_TIMEOUT_SECONDS


def review_directory(
    root: str,
    changed_only: bool = False,
    fail_on: str = "high",
    base_ref: str = "",
    head_ref: str = "",
    llm_config: Optional[QualityLLMConfig] = None,
) -> QualityReviewResult:
    root = os.path.abspath(root)
    selected_files = _changed_python_files(root, base_ref, head_ref) if changed_only else None
    findings: List[QualityFinding] = []
    skipped_files: List[str] = []
    reviewed_sources: List[Tuple[str, str]] = []
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
        findings.extend(_review_file(rel, source))
        if _is_llm_review_candidate(rel, source):
            reviewed_sources.append((rel, source))

    llm_warnings: List[str] = []
    if llm_config is not None:
        llm_findings, llm_warnings = _review_with_llm(reviewed_sources, findings, llm_config)
        findings.extend(llm_findings)

    threshold = SEVERITY_ORDER.get(fail_on, SEVERITY_ORDER["high"])
    should_fail = any(SEVERITY_ORDER.get(f.severity, 0) >= threshold for f in findings)
    status = "failed" if should_fail else "passed"
    findings.sort(key=lambda f: (-SEVERITY_ORDER.get(f.severity, 0), f.file, f.line, f.rule_id))

    return QualityReviewResult(
        root=root,
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        scanned_files=scanned_files,
        changed_only=changed_only,
        fail_on=fail_on,
        status=status,
        findings=findings,
        skipped_files=skipped_files,
        review_mode="static+llm" if llm_config is not None else "static",
        llm_model=llm_config.model if llm_config is not None else "",
        llm_warnings=llm_warnings,
    )


def render_quality_markdown(data: Dict[str, object]) -> str:
    findings = data.get("findings", [])
    counts = data.get("severity_counts", {})
    lines = [
        "# AI Quality Gate Report",
        "",
        f"_Generated: {data['generated_at']}_  ",
        f"_Status: {str(data['status']).upper()}_  ",
        f"_Scanned {data['scanned_files']} Python file(s). Fail threshold: {data['fail_on']}._  ",
        f"_Review mode: {data.get('review_mode', 'static')}_",
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
        lines.append("_No AI quality-gate findings detected._")
        lines.append("")
        return "\n".join(lines)

    lines.extend([
        "## Findings",
        "",
        "| Severity | Source | Area | Location | Finding | Suggestion |",
        "|---|---|---|---|---|---|",
    ])
    for finding in findings:
        location = f"`{finding['file']}:{finding['line']}`"
        lines.append(
            f"| {finding['severity'].title()} | {finding.get('source', 'static')} | "
            f"{finding['area']} | {location} | "
            f"{finding['title']} | {finding['suggestion']} |"
        )

    warnings = data.get("llm_warnings") or []
    if warnings:
        lines.extend(["", "## LLM Review Warnings", ""])
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


def _review_file(rel: str, source: str) -> List[QualityFinding]:
    lines = source.splitlines()
    lower_source = source.lower()
    findings: List[QualityFinding] = []
    is_text_to_sql = _is_text_to_sql_file(rel, lower_source)
    is_ai_file = is_text_to_sql or any(hint in rel.lower() for hint in AI_PATH_HINTS)

    if is_text_to_sql:
        findings.extend(_review_text_to_sql(rel, lines, lower_source))
    if is_ai_file:
        findings.extend(_review_ai_general(rel, lines))

    return findings


def _review_with_llm(
    reviewed_sources: Sequence[Tuple[str, str]],
    static_findings: Sequence[QualityFinding],
    config: QualityLLMConfig,
) -> Tuple[List[QualityFinding], List[str]]:
    warnings: List[str] = []
    if not config.api_key:
        return [], ["LLM review requested but no API key was provided."]
    if not reviewed_sources:
        return [], []

    evidence = _build_llm_review_evidence(reviewed_sources, static_findings)
    try:
        response = _call_quality_llm(config, evidence)
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return [], [f"LLM review request failed: {exc}"]
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        return [], [f"Could not parse LLM review response: {exc}"]

    raw_findings = response.get("findings", [])
    if not isinstance(raw_findings, list):
        return [], ["LLM review response did not contain a findings array."]

    allowed_files = {rel for rel, _source in reviewed_sources}
    findings: List[QualityFinding] = []
    for index, item in enumerate(raw_findings, start=1):
        if not isinstance(item, dict):
            warnings.append(f"LLM finding {index} was not an object; skipped.")
            continue
        severity = str(item.get("severity", "")).strip().lower()
        if severity not in SEVERITY_ORDER:
            warnings.append(f"LLM finding {index} had invalid severity; skipped.")
            continue
        file_name = str(item.get("file", "")).strip().replace("/", os.sep)
        if file_name not in allowed_files:
            warnings.append(f"LLM finding {index} referenced unknown file '{file_name}'; skipped.")
            continue
        line = _safe_positive_int(item.get("line"), default=1)
        findings.append(QualityFinding(
            severity=severity,
            area=_clean_llm_text(item.get("area"), "LLM review"),
            file=file_name,
            line=line,
            title=_clean_llm_text(item.get("title"), "LLM-generated quality finding"),
            suggestion=_clean_llm_text(item.get("suggestion"), "Review this AI implementation manually."),
            rule_id=f"llm-review-{index}",
            feature=_clean_llm_text(item.get("feature"), "general"),
            source="llm",
        ))
    return findings, warnings


def _build_llm_review_evidence(
    reviewed_sources: Sequence[Tuple[str, str]],
    static_findings: Sequence[QualityFinding],
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


def _call_quality_llm(config: QualityLLMConfig, evidence: str) -> Dict[str, object]:
    url = config.base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": LLM_REVIEW_SYSTEM_PROMPT},
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
            content = body["choices"][0]["message"]["content"].strip()
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


def _safe_positive_int(value: object, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _is_llm_review_candidate(rel: str, source: str) -> bool:
    lower_rel = rel.lower()
    lower_source = source.lower()
    return any(hint in lower_rel for hint in AI_PATH_HINTS) or any(
        word in lower_source
        for word in ("llm", "prompt", "invoke(", "openai", "anthropic", "bedrock", "langchain", "text2sql")
    )


def _review_text_to_sql(rel: str, lines: Sequence[str], lower_source: str) -> List[QualityFinding]:
    findings: List[QualityFinding] = []
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
        findings.append(QualityFinding(
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
        findings.append(QualityFinding(
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
        findings.append(QualityFinding(
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


def _review_ai_general(rel: str, lines: Sequence[str]) -> List[QualityFinding]:
    findings: List[QualityFinding] = []
    joined = "\n".join(lines).lower()

    for index, line in enumerate(lines, start=1):
        lower = line.lower()
        if _looks_like_prompt_interpolation(line):
            findings.append(QualityFinding(
                severity="medium",
                area="Prompt quality",
                file=rel,
                line=index,
                title="Prompt appears to interpolate user input directly.",
                suggestion="Separate trusted instructions from user content and add prompt-injection guardrails around the user-provided value.",
                rule_id="prompt-direct-user-input",
            ))
        if _looks_like_llm_call(lower) and "timeout" not in lower:
            findings.append(QualityFinding(
                severity="medium",
                area="LLM reliability",
                file=rel,
                line=index,
                title="LLM call does not show an inline timeout.",
                suggestion="Set request timeout and retry/fallback behavior near the model call or provider client.",
                rule_id="llm-timeout-missing",
            ))

    if _contains_prompt(joined) and not _contains_output_contract(joined):
        findings.append(QualityFinding(
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
