"""CLI entrypoint for AI implementation quality review."""
import argparse
import json
import os
import sys

from .envfile import env_bool, load_env_file
from .quality import (
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MODEL,
    SEVERITY_ORDER,
    QualityLLMConfig,
    render_quality_markdown,
    review_directory,
)


def main(argv=None) -> int:
    load_env_file(os.environ.get("AI_STACK_ENV_FILE", ".env"))

    parser = argparse.ArgumentParser(description="Review AI-related code for CI quality-gate findings.")
    parser.add_argument("--path", default=".", help="Root directory to review (default: current directory)")
    parser.add_argument("--format", choices=["json", "markdown"], default="markdown")
    parser.add_argument("--output", default=None, help="Write output to this file instead of stdout")
    parser.add_argument("--markdown-output", default=None, help="Optional Markdown output path")
    parser.add_argument("--json-output", default=None, help="Optional JSON output path")
    parser.add_argument("--changed-only", action="store_true", help="Review only changed Python files from git diff")
    parser.add_argument("--base-ref", default="", help="Base branch/ref for changed-only mode, e.g. origin/main")
    parser.add_argument("--head-ref", default="", help="Head branch/ref for changed-only mode")
    parser.add_argument(
        "--fail-on",
        choices=sorted(SEVERITY_ORDER, key=lambda s: SEVERITY_ORDER[s]),
        default="high",
        help="Exit with code 1 when findings at or above this severity are present",
    )
    parser.add_argument("--no-fail", action="store_true", help="Always exit 0 after writing the report")
    parser.add_argument(
        "--llm-review",
        action="store_true",
        help="Enable optional LLM quality review. Also enabled by AI_STACK_USE_LLM=true.",
    )
    parser.add_argument(
        "--llm-base-url",
        default=os.environ.get("AI_STACK_LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
        help="OpenAI-compatible chat-completions base URL.",
    )
    parser.add_argument(
        "--llm-api-key",
        default=os.environ.get("AI_STACK_LLM_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
        help="API key for the LLM endpoint.",
    )
    parser.add_argument(
        "--llm-model",
        default=os.environ.get("AI_STACK_LLM_MODEL", DEFAULT_LLM_MODEL),
        help="Model name for optional LLM review.",
    )
    args = parser.parse_args(argv)

    llm_requested = args.llm_review or env_bool("AI_STACK_USE_LLM")
    llm_config = None
    if llm_requested:
        llm_config = QualityLLMConfig(
            base_url=args.llm_base_url or DEFAULT_LLM_BASE_URL,
            api_key=args.llm_api_key,
            model=args.llm_model or DEFAULT_LLM_MODEL,
        )
        if not llm_config.api_key:
            print(
                "Warning: LLM quality review requested but no API key was provided. "
                "Continuing with static review only.",
                file=sys.stderr,
            )
            llm_config = None
        else:
            print(
                f"LLM quality review enabled (model: {llm_config.model}, endpoint: {llm_config.base_url}).",
                file=sys.stderr,
            )

    result = review_directory(
        root=args.path,
        changed_only=args.changed_only,
        fail_on=args.fail_on,
        base_ref=args.base_ref,
        head_ref=args.head_ref,
        llm_config=llm_config,
    )
    data = result.to_dict()

    json_output = json.dumps(data, indent=2)
    markdown_output = render_quality_markdown(data)

    wrote_files = []
    if args.markdown_output:
        with open(args.markdown_output, "w", encoding="utf-8") as handle:
            handle.write(markdown_output)
        wrote_files.append(args.markdown_output)
    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as handle:
            handle.write(json_output)
        wrote_files.append(args.json_output)
    if args.output:
        output = json_output if args.format == "json" else markdown_output
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output)
        wrote_files.append(args.output)

    if wrote_files:
        print(
            f"Wrote AI quality report to {', '.join(wrote_files)} "
            f"({data['status']}, {len(data['findings'])} finding(s), mode: {data['review_mode']})",
            file=sys.stderr,
        )
    elif args.format == "json":
        print(json_output)
    else:
        print(markdown_output)

    if not args.no_fail and data["status"] == "failed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
