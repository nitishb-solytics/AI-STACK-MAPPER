"""Walks a repository and aggregates findings into a ScanResult."""
import os
import datetime
from typing import List

from .models import ScanResult
from .ast_visitor import scan_source
from .config_scanner import scan_dependency_file, scan_mcp_config, scan_env_file, scan_js_dependency_file
from .registry import MCP_CONFIG_FILENAMES, DEPENDENCY_FILES

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "venv", ".venv", "env", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".tox", "site-packages", ".idea", ".vscode-test", "egg-info",
}

ENV_FILENAMES = {".env", ".env.example", ".env.local", ".env.sample"}
JS_DEPENDENCY_FILENAMES = {"package.json"}


def _iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.endswith(".egg-info")]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def scan_directory(root: str, scanner_mode: str = "static") -> ScanResult:
    root = os.path.abspath(root)
    result = ScanResult(
        root=root,
        generated_at=datetime.datetime.utcnow().isoformat() + "Z",
        scanned_files=0,
        scanner_mode=scanner_mode,
    )

    for path in _iter_files(root):
        rel = os.path.relpath(path, root)
        fn = os.path.basename(path)
        findings: List = []

        try:
            if fn.endswith(".py"):
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    source = f.read()
                findings = scan_source(rel, source)
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

    return result
