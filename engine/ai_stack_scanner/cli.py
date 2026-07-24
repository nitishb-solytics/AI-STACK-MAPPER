"""
CLI entrypoint.

Usage:
    python -m ai_stack_scanner.cli --path . --format json --output report.json
    python -m ai_stack_scanner.cli --path . --format markdown --output AI_STACK.md
    python -m ai_stack_scanner.cli --path . --markdown-output AI_STACK.md --json-output ai-stack-report.json
    python -m ai_stack_scanner.cli --path .                      # prints JSON to stdout

Scanner modes:
    --scanner-mode static   Deterministic AST/config scan only (default)
    --scanner-mode llm      LLM discovery only
    --scanner-mode hybrid   Static scan + LLM discovery

Optional LLM enrichment (OFF by default -- see ai_stack_scanner/enrich.py):
    python -m ai_stack_scanner.cli --path . --enrich --llm-api-key sk-...
    python -m ai_stack_scanner.cli --path . --enrich --llm-base-url http://localhost:11434/v1 --llm-model llama3

Or configure it via a `.env` file in the current directory instead of flags
(copy engine/.env.example to engine/.env and fill it in -- .env is
gitignored, never commit it):

    AI_STACK_USE_LLM=true
    AI_STACK_LLM_API_KEY=sk-...

Explicit CLI flags / real environment variables always win over `.env`.
"""
import argparse
import datetime
import json
import os
import sys

from .models import ScanResult
from .scanner import scan_directory
from .report import render_markdown
from .envfile import load_env_file, env_bool


def main(argv=None) -> int:
    load_env_file(os.environ.get("AI_STACK_ENV_FILE", ".env"))

    parser = argparse.ArgumentParser(description="Scan a Python codebase for LLM/MCP/Tool/Agent components.")
    parser.add_argument("--path", default=".", help="Root directory to scan (default: current directory)")
    parser.add_argument(
        "--scanner-mode",
        choices=["static", "llm", "hybrid"],
        default=os.environ.get("AI_STACK_SCANNER_MODE", "static"),
        help="Inventory scanner mode: static, llm, or hybrid (default: static, or $AI_STACK_SCANNER_MODE).",
    )
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--output", default=None, help="Write output to this file instead of stdout")
    parser.add_argument("--markdown-output", default=None, help="Write Markdown report to this file.")
    parser.add_argument("--json-output", default=None, help="Write JSON report to this file.")
    parser.add_argument(
        "--enrich", action="store_true",
        help="Optional: ask an LLM to infer each component's likely purpose/usage/expected "
             "output. OFF by default (can also be enabled by setting AI_STACK_USE_LLM=true in "
             "the environment or a .env file). Sends only code-derived context (file:line, "
             "enclosing function/class name + docstring, prompt text) to the configured LLM "
             "endpoint -- never full source files or secret values. Point --llm-base-url at a "
             "self-hosted endpoint if code confidentiality matters.",
    )
    parser.add_argument(
        "--llm-base-url", default=os.environ.get("AI_STACK_LLM_BASE_URL", ""),
        help="OpenAI-compatible chat-completions base URL (default: https://api.openai.com/v1, "
             "or $AI_STACK_LLM_BASE_URL). Point this at a self-hosted/on-prem endpoint to avoid "
             "sending code context to a public cloud LLM.",
    )
    parser.add_argument(
        "--llm-api-key", default=os.environ.get("AI_STACK_LLM_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
        help="API key for the LLM endpoint (default: $AI_STACK_LLM_API_KEY or $OPENAI_API_KEY).",
    )
    parser.add_argument(
        "--llm-model", default=os.environ.get("AI_STACK_LLM_MODEL", "gpt-4o-mini"),
        help="Model name to request (default: gpt-4o-mini, or $AI_STACK_LLM_MODEL).",
    )
    args = parser.parse_args(argv)

    if args.scanner_mode not in {"static", "llm", "hybrid"}:
        print(
            f"Warning: invalid scanner mode '{args.scanner_mode}', falling back to static.",
            file=sys.stderr,
        )
        args.scanner_mode = "static"

    result = _run_inventory_scan(args)

    enrich_requested = args.enrich
    enrich_source = "--enrich"
    if not enrich_requested and env_bool("AI_STACK_USE_LLM"):
        enrich_requested = True
        enrich_source = "AI_STACK_USE_LLM=true"

    if enrich_requested:
        from .enrich import enrich_components, LLMConfig, DEFAULT_BASE_URL

        config = LLMConfig(
            base_url=args.llm_base_url or DEFAULT_BASE_URL,
            api_key=args.llm_api_key,
            model=args.llm_model,
        )
        if not config.api_key:
            print(
                f"Warning: AI enrichment requested ({enrich_source}) but no API key was provided "
                "(--llm-api-key / $AI_STACK_LLM_API_KEY / $OPENAI_API_KEY). Skipping enrichment.",
                file=sys.stderr,
            )
        else:
            print(f"AI enrichment enabled via {enrich_source} (model: {config.model}).", file=sys.stderr)
            warnings = enrich_components(result.components, config)
            for w in warnings:
                print(f"Warning: enrichment failed for component {w}", file=sys.stderr)

    data = result.to_dict()

    if args.markdown_output or args.json_output:
        if args.markdown_output:
            with open(args.markdown_output, "w", encoding="utf-8") as f:
                f.write(render_markdown(data))
            print(f"Wrote markdown report to {args.markdown_output}", file=sys.stderr)
        if args.json_output:
            with open(args.json_output, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
            print(f"Wrote json report to {args.json_output}", file=sys.stderr)
        print(
            f"Scan complete ({data['scanner_mode']} mode): "
            f"{data['total_components']} components across {data['scanned_files']} Python files",
            file=sys.stderr,
        )
        return 0

    if args.format == "json":
        output = json.dumps(data, indent=2)
    else:
        output = render_markdown(data)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Wrote {args.format} report to {args.output} "
              f"({data['total_components']} components across {data['scanned_files']} files)",
              file=sys.stderr)
    else:
        print(output)

    return 0


def _run_inventory_scan(args) -> ScanResult:
    """Run static, LLM-only, or hybrid inventory scanning."""
    if args.scanner_mode == "static":
        return scan_directory(args.path, scanner_mode="static")

    from .llm_discovery import discover_components, LLMDiscoveryConfig, DEFAULT_BASE_URL

    config = LLMDiscoveryConfig(
        base_url=args.llm_base_url or DEFAULT_BASE_URL,
        api_key=args.llm_api_key,
        model=args.llm_model,
    )
    if not config.api_key:
        print(
            "Warning: LLM scanner mode requested but no API key was provided "
            "(--llm-api-key / $AI_STACK_LLM_API_KEY / $OPENAI_API_KEY). "
            "Falling back to static scan.",
            file=sys.stderr,
        )
        return scan_directory(args.path, scanner_mode="static")

    if args.scanner_mode == "llm":
        print(f"LLM inventory discovery enabled (model: {config.model}).", file=sys.stderr)
        llm_result, warnings = discover_components(args.path, config)
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        return llm_result

    static_result = scan_directory(args.path, scanner_mode="hybrid")
    print(f"Hybrid inventory scan enabled: static + LLM discovery (model: {config.model}).", file=sys.stderr)
    llm_result, warnings = discover_components(args.path, config)
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    for component in llm_result.components.values():
        for occurrence in component.occurrences:
            static_result.add(component.category, component.name, component.package, occurrence)
    static_result.scanned_files = max(static_result.scanned_files, llm_result.scanned_files)
    static_result.skipped_files = sorted(set(static_result.skipped_files + llm_result.skipped_files))
    static_result.generated_at = datetime.datetime.utcnow().isoformat() + "Z"
    return static_result


if __name__ == "__main__":
    raise SystemExit(main())
