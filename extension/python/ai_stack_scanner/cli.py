"""CLI entrypoint used by the VS Code extension.

Usage:
    python -m ai_stack_scanner.cli --path . --json-output ai-stack-report.json --markdown-output AI_STACK.md
"""
import argparse
import json
import sys

from .report import render_markdown
from .scanner import scan_directory


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan a Python codebase for local AI agents and AI-stack components."
    )
    parser.add_argument("--path", default=".", help="Root directory to scan.")
    parser.add_argument("--markdown-output", default=None, help="Write Markdown report to this file.")
    parser.add_argument("--json-output", default=None, help="Write JSON report to this file.")
    args = parser.parse_args(argv)

    result = scan_directory(args.path, scanner_mode="static")
    data = result.to_dict()

    if args.markdown_output:
        with open(args.markdown_output, "w", encoding="utf-8") as f:
            f.write(render_markdown(data))
        print(f"Wrote markdown report to {args.markdown_output}", file=sys.stderr)

    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        print(f"Wrote json report to {args.json_output}", file=sys.stderr)

    if not args.markdown_output and not args.json_output:
        print(json.dumps(data, indent=2))

    print(
        f"Scan complete: {data['total_components']} components across {data['scanned_files']} Python files",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
