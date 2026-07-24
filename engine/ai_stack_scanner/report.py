"""Render a ScanResult dict (from ScanResult.to_dict()) as Markdown."""
from .models import ALL_CATEGORIES, CATEGORY_LABELS, CATEGORY_LLM, DEPLOYMENT_LABELS


def _deployment_str(comp: dict) -> str:
    targets = comp.get("deployment_targets") or ["cloud"]
    labels = [DEPLOYMENT_LABELS.get(t, t) for t in targets]
    return " + ".join(labels) if len(labels) > 1 else labels[0]


def render_markdown(data: dict) -> str:
    lines = []
    lines.append("# AI Stack Report")
    lines.append("")
    lines.append(f"_Generated: {data['generated_at']}_  ")
    lines.append(f"_Scanned {data['scanned_files']} Python file(s), "
                  f"found {data['total_components']} distinct component(s)._")
    lines.append("")

    any_content = False
    for category in ALL_CATEGORIES:
        components = data["categories"].get(category, [])
        if not components:
            continue
        any_content = True
        lines.append(f"## {CATEGORY_LABELS[category]}")
        lines.append("")
        if category == CATEGORY_LLM:
            lines.append("| Component | Confidence | Deployment | Occurrences | Example location |")
            lines.append("|---|---|---|---|---|")
            for comp in components:
                example = comp["occurrences"][0] if comp["occurrences"] else None
                example_str = f"`{example['file']}:{example['line']}`" if example else "-"
                lines.append(
                    f"| {comp['name']} | {comp['confidence']} | {_deployment_str(comp)} | "
                    f"{comp['count']} | {example_str} |"
                )
        else:
            lines.append("| Component | Confidence | Occurrences | Example location |")
            lines.append("|---|---|---|---|")
            for comp in components:
                example = comp["occurrences"][0] if comp["occurrences"] else None
                example_str = f"`{example['file']}:{example['line']}`" if example else "-"
                lines.append(
                    f"| {comp['name']} | {comp['confidence']} | {comp['count']} | {example_str} |"
                )
        lines.append("")

        enriched = [c for c in components if c.get("ai_enrichment")]
        if enriched:
            lines.append("<details><summary>AI-generated context (optional, unverified -- confirm before relying on it)</summary>")
            lines.append("")
            for comp in enriched:
                ai = comp["ai_enrichment"]
                lines.append(f"**{comp['name']}** _(model: {ai.get('model', 'n/a')})_")
                if ai.get("purpose"):
                    lines.append(f"- Purpose: {ai['purpose']}")
                if ai.get("usage_description"):
                    lines.append(f"- Usage: {ai['usage_description']}")
                if ai.get("expected_output"):
                    lines.append(f"- Expected output: {ai['expected_output']}")
                lines.append("")
            lines.append("</details>")
            lines.append("")

    if not any_content:
        lines.append("_No LLM / MCP / Tool / Agent-framework components detected._")
        lines.append("")

    if data.get("skipped_files"):
        lines.append("<details><summary>Skipped files (parse errors)</summary>")
        lines.append("")
        for f in data["skipped_files"]:
            lines.append(f"- `{f}`")
        lines.append("")
        lines.append("</details>")

    return "\n".join(lines)
