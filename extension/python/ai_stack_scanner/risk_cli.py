"""CLI entrypoint for local AI risk scan reports.

Risk reports are generated locally and are not published to Vault.
"""
import argparse
import json
import sys

from .risk_scanner import render_risk_markdown, scan_risks


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Scan a Python codebase for local code assessment risks.")
    parser.add_argument("--path", default=".", help="Root directory to scan.")
    parser.add_argument(
        "--fail-on",
        choices=["info", "low", "medium", "high", "critical"],
        default="high",
        help="Severity threshold used in the report status.",
    )
    parser.add_argument("--markdown-output", default=None, help="Write Markdown report to this file.")
    parser.add_argument("--json-output", default=None, help="Write JSON report to this file.")
    args = parser.parse_args(argv)

    data = scan_risks(args.path, fail_on=args.fail_on)

    if args.markdown_output:
        with open(args.markdown_output, "w", encoding="utf-8") as f:
            f.write(render_risk_markdown(data))
        print(f"Wrote AI risk report to {args.markdown_output}", file=sys.stderr)

    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        print(f"Wrote AI risk json to {args.json_output}", file=sys.stderr)

    if not args.markdown_output and not args.json_output:
        print(json.dumps(data, indent=2))

    print(
        f"Risk scan complete: {len(data['findings'])} finding(s), status={data['status']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
