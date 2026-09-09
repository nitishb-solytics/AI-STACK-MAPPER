"""CLI entrypoint used by the VS Code extension.

Usage:
    python -m ai_stack_scanner.cli --path . --json-output ai-stack-report.json --markdown-output AI_STACK.md
"""
import argparse
import json
import sys

from .report import render_markdown
from .scanner import AGENT_DISCOVERY_MODES, DISCOVERY_ENTRY_POINTS, scan_directory


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan a Python codebase for local AI agents and AI-stack components."
    )
    parser.add_argument("--path", default=".", help="Root directory to scan.")
    parser.add_argument("--markdown-output", default=None, help="Write Markdown report to this file.")
    parser.add_argument("--json-output", default=None, help="Write JSON report to this file.")
    parser.add_argument(
        "--agent-discovery",
        choices=AGENT_DISCOVERY_MODES,
        default=DISCOVERY_ENTRY_POINTS,
        help=(
            "How local agents are inferred. 'entry-points' (default) finds exposed "
            "routes/tasks/commands and keeps only those whose import closure reaches a real "
            "LLM client, falling back to gated path inference for code no entry point covers. "
            "'path' is the older behaviour: name an agent whenever a file path looks AI-ish."
        ),
    )
    args = parser.parse_args(argv)

    result = scan_directory(
        args.path, scanner_mode="static", agent_discovery=args.agent_discovery
    )
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
