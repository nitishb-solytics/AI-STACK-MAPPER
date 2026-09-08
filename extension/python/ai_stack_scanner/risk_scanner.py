"""Small static AI/code risk scanner for the VS Code extension.

This is local-only report generation. Findings are not published to Vault.
"""
from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY_RANK = {name: idx for idx, name in enumerate(SEVERITIES)}


@dataclass
class RiskFinding:
    severity: str
    area: str
    file: str
    line: int
    title: str
    suggestion: str
    rule_id: str
    source: str
    evidence_snippet: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "severity": self.severity,
            "area": self.area,
            "file": self.file,
            "line": self.line,
            "title": self.title,
            "suggestion": self.suggestion,
            "rule_id": self.rule_id,
            "source": self.source,
            "evidence_snippet": self.evidence_snippet,
        }


def _iter_py_files(root: str) -> Iterable[str]:
    skip_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules", "dist", "build", "out"}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(base, name)


def _rel(root: str, file_path: str) -> str:
    return os.path.relpath(file_path, root).replace(os.sep, "/")


def _line(lines: List[str], lineno: int) -> str:
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()[:500]
    return ""


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _kw_bool(call: ast.Call, name: str, value: bool) -> bool:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and kw.value.value is value:
            return True
    return False


SECRET_PATTERN = re.compile(
    r"(api[_-]?key|secret|token|password)\s*=\s*['\"][^'\"]{8,}['\"]",
    re.IGNORECASE,
)


def _scan_ast(root: str, file_path: str, source: str, lines: List[str]) -> List[RiskFinding]:
    findings: List[RiskFinding] = []
    try:
      tree = ast.parse(source)
    except SyntaxError:
      return findings

    rel = _rel(root, file_path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in {"eval", "exec"}:
                findings.append(RiskFinding(
                    "high", "Code execution", rel, node.lineno,
                    f"Dynamic code execution via {name}()",
                    "Avoid executing dynamic strings. Use explicit parsers/dispatch tables.",
                    "dynamic-code-execution", "ast", _line(lines, node.lineno),
                ))
            if name.endswith("subprocess.run") or name.endswith("subprocess.Popen") or name in {"run", "Popen"}:
                if _kw_bool(node, "shell", True):
                    findings.append(RiskFinding(
                        "high", "Command execution", rel, node.lineno,
                        "Shell command execution detected",
                        "Avoid shell=True. Pass argument lists and validate user-controlled inputs.",
                        "subprocess-shell-true", "ast", _line(lines, node.lineno),
                    ))
            if name in {"pickle.load", "pickle.loads"}:
                findings.append(RiskFinding(
                    "medium", "Unsafe deserialization", rel, node.lineno,
                    "Pickle deserialization detected",
                    "Do not unpickle untrusted data. Prefer JSON or signed payloads.",
                    "pickle-deserialization", "ast", _line(lines, node.lineno),
                ))
            if name in {"yaml.load"}:
                findings.append(RiskFinding(
                    "medium", "Unsafe deserialization", rel, node.lineno,
                    "yaml.load detected",
                    "Use yaml.safe_load unless trusted constructors are required.",
                    "yaml-load", "ast", _line(lines, node.lineno),
                ))
            if name.endswith("requests.get") or name.endswith("requests.post") or name.endswith("requests.request"):
                if _kw_bool(node, "verify", False):
                    findings.append(RiskFinding(
                        "medium", "Transport security", rel, node.lineno,
                        "TLS certificate verification disabled",
                        "Remove verify=False or configure a trusted CA bundle.",
                        "requests-verify-false", "ast", _line(lines, node.lineno),
                    ))
    return findings


def _scan_text(root: str, file_path: str, lines: List[str]) -> List[RiskFinding]:
    rel = _rel(root, file_path)
    findings: List[RiskFinding] = []
    for idx, text in enumerate(lines, start=1):
        if SECRET_PATTERN.search(text):
            findings.append(RiskFinding(
                "high", "Secret management", rel, idx,
                "Possible hardcoded secret",
                "Move secrets to Vault/secret manager/environment variables and rotate exposed values.",
                "possible-hardcoded-secret", "regex", text.strip()[:500],
            ))
    return findings


def scan_risks(root: str, fail_on: str = "high") -> Dict[str, object]:
    findings: List[RiskFinding] = []
    skipped: List[str] = []
    scanned_files = 0
    for file_path in _iter_py_files(root):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                source = f.read()
        except UnicodeDecodeError:
            skipped.append(_rel(root, file_path))
            continue
        scanned_files += 1
        lines = source.splitlines()
        findings.extend(_scan_ast(root, file_path, source, lines))
        findings.extend(_scan_text(root, file_path, lines))

    counts = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    threshold = SEVERITY_RANK.get(fail_on, SEVERITY_RANK["high"])
    failed = any(SEVERITY_RANK.get(f.severity, 99) <= threshold for f in findings)
    return {
        "root": root,
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "scanned_files": scanned_files,
        "fail_on": fail_on,
        "status": "failed" if failed else "passed",
        "risk_scan_mode": "static",
        "severity_counts": counts,
        "findings": [f.to_dict() for f in findings],
        "skipped_files": skipped,
    }


def render_risk_markdown(data: Dict[str, object]) -> str:
    findings = data.get("findings") or []
    lines = [
        "# AI Risk Report",
        "",
        f"_Generated: {data.get('generated_at')}_",
        "",
        f"_Scanned {data.get('scanned_files')} Python file(s). Status: {data.get('status')}._",
        "",
    ]
    if not findings:
        lines.append("_No code assessment risk findings detected._")
        return "\n".join(lines) + "\n"

    for finding in findings:
        lines.extend([
            f"## {str(finding.get('severity')).upper()} - {finding.get('title')}",
            "",
            f"- File: `{finding.get('file')}:{finding.get('line')}`",
            f"- Rule: `{finding.get('rule_id')}`",
            f"- Area: `{finding.get('area')}`",
            f"- Suggestion: {finding.get('suggestion')}",
            "",
        ])
        evidence = finding.get("evidence_snippet")
        if evidence:
            lines.extend(["```", str(evidence), "```", ""])
    return "\n".join(lines)


def dumps(data: Dict[str, object]) -> str:
    return json.dumps(data, indent=2)
