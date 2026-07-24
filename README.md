# AI Stack Mapper

Scans a Python codebase and gives you a structured map of every **LLM
provider, MCP server/client, Tool definition, and Agent/orchestration
framework** it uses — as a VS Code sidebar, and as a report that
auto-updates on every push via GitHub Actions.

## How it's put together

```
engine/       Python detection engine (stdlib-only, no dependencies).
              Real static analysis via the `ast` module, plus scanning of
              requirements.txt/pyproject.toml, mcp.json / claude_desktop_
              config.json, and .env KEY NAMES (never values).

extension/    VS Code extension (TypeScript). Spawns a bundled copy of the
              engine as a subprocess and renders results as a tree view:
              Category -> Component -> file:line occurrences (click to jump).

.github/      GitHub Action that re-runs the same engine on every push and
workflows/    commits AI_STACK.md + ai-stack-report.json back to the repo.

sample_project/  A small worked example (OpenAI + Anthropic clients,
              LangChain @tool, an MCP FastMCP server, a CrewAI dependency,
              mcp.json config) you can point the scanner at to see it work.
```

One engine, two consumers (extension + CI) — so the detection logic only
lives in one place and stays in sync between "what I see in VS Code" and
"what's recorded in the repo."

## What it detects, and how confidently

| Signal | Example | Confidence |
|---|---|---|
| Direct instantiation | `client = OpenAI()`, `mcp = FastMCP("x")` | high |
| Import | `from anthropic import Anthropic` | high |
| Decorator | `@tool`, `@mcp.tool()` (correctly told apart) | high |
| Class subclassing | `class MyTool(BaseTool):` | high |
| Declared dependency | `openai>=1.30` in requirements.txt | medium |
| MCP config entry | `mcpServers` block in mcp.json | medium |
| Env var name present | `ANTHROPIC_API_KEY=` in .env.example | low |
| Bare model-name string | `"gpt-4o"`, `"claude-opus-4-1"` | low |

Every finding carries file + line number and a confidence level, so the
extension/report can be filtered down to "only things I'm sure about" if
the low-confidence heuristics get noisy in a large repo.

This is static analysis, not a type checker — it will miss fully dynamic
patterns (tools assembled from a runtime dict of callables, etc.) and can
occasionally mistag an unrelated class literally named `Agent`. Treat it as
a fast, high-recall map to jump off from, not a certified inventory.

## Scanner modes for stack inventory

The stack inventory scanner can run in three modes:

| Mode | What it does | When to use |
|---|---|---|
| `static` | Uses deterministic Python AST scanning plus dependency/config scanning. No LLM call. | Default, safest for CI and private repos. |
| `llm` | Uses an OpenAI-compatible LLM to discover AI stack components from small redacted snippets and dependency/config evidence. | When you want semantic discovery for custom agents/tools that static rules may miss. |
| `hybrid` | Runs static first, then adds LLM-discovered components into the same report. | Recommended when an API key is available and you want better coverage. |

`enrich: true` is separate. It does not control discovery mode. It asks the
LLM to add short explanatory context to components that were already found.

So the clean mental model is:

- `scanner-mode` = how components are detected.
- `enrich` = whether detected components get AI-generated descriptions.

If `scanner-mode` is `llm` or `hybrid` but no API key is configured, the
scanner falls back to `static` so the workflow still produces a report.

## 1. Run the engine standalone

No dependencies beyond Python 3.8+:

```bash
cd engine
pip install -e .          # installs the `ai-stack-scan` command
ai-stack-scan --path ../sample_project --format markdown
ai-stack-scan --path ../sample_project --format json --output report.json
ai-stack-scan --path ../sample_project --scanner-mode static --markdown-output AI_STACK.md --json-output ai-stack-report.json
ai-stack-scan --path ../sample_project --scanner-mode hybrid --llm-api-key "$AI_STACK_LLM_API_KEY" --markdown-output AI_STACK.md --json-output ai-stack-report.json
ai-stack-review --path ../sample_project --format markdown --output ai-quality-report.md --no-fail
ai-stack-review --path ../sample_project --format markdown --output ai-quality-report.md --fail-on high
```

`ai-stack-review` is the CI quality-gate command. It reports prompt, LLM-call,
TextToSQL, and agent/RAG implementation findings, then exits with code `1`
when a finding meets or exceeds `--fail-on`.

Or without installing, straight from source:

```bash
cd engine
PYTHONPATH=. python3 -m ai_stack_scanner.cli --path ../sample_project --format markdown
PYTHONPATH=. python3 -m ai_stack_scanner.review_cli --path ../sample_project --no-fail
```

## 2. Run the VS Code extension

```bash
cd extension
npm install
npm run compile
```

Then open the `extension/` folder in VS Code and press **F5** — this opens
an Extension Development Host window. Open your Python repo there, click
the new "AI Stack" icon in the Activity Bar, and run **AI Stack: Scan
Workspace** from the command palette (it also runs automatically on
activation and, if `aiStackMapper.scanOnSave` is on, after saving a
relevant file).

If `python3` isn't on your PATH under that name, set
`aiStackMapper.pythonPath` in Settings.

To ship it as an installable `.vsix`:

```bash
npm install -g @vscode/vsce
cd extension
vsce package
```

That produces `ai-stack-mapper-0.1.0.vsix`, installable via **Extensions:
Install from VSIX...** in VS Code.

## 3. Use as a GitHub Action

This repo includes `action.yml`, so after pushing `AI-STACK-MAPPER` to its
own GitHub repository, other repositories can call it as a reusable action.
Keep this project separate from the repositories it scans.

Example workflow for DocSearch:

```yaml
name: AI Quality Gate

on:
  pull_request:
    branches: [india-dev-revamp]
  workflow_dispatch:

permissions:
  contents: read
  pull-requests: read

jobs:
  ai-quality-gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Run AI Stack Mapper
        uses: YOUR-GITHUB-USER/ai-stack-mapper@v1
        env:
          AI_STACK_USE_LLM: "true"
          AI_STACK_LLM_API_KEY: ${{ secrets.AI_STACK_LLM_API_KEY }}
        with:
          path: "."
          mode: "both"
          scanner-mode: "hybrid"
          enrich: "false"
          changed-only: "true"
          base-ref: ${{ github.base_ref }}
          fail-on: "high"
          llm-review: "true"
          llm-api-key: ${{ secrets.AI_STACK_LLM_API_KEY }}
          llm-base-url: "https://openrouter.ai/api/v1"
          llm-model: "google/gemma-4-26b-a4b-it:free"

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: ai-stack-reports
          path: |
            AI_STACK.md
            ai-stack-report.json
            ai-quality-report.md
            ai-quality-report.json
          if-no-files-found: error
```

To actually block merges, add this workflow as a required status check in the
DocSearch branch protection rule. The action fails when `ai-stack-review`
finds issues at or above the selected threshold.

LLM usage is optional and split by job:

- Stack inventory: `scanner-mode: "static"` is fully static. Use
  `scanner-mode: "llm"` for LLM-only discovery, or `"hybrid"` for static +
  LLM discovery.
- Stack inventory enrichment: `enrich: "true"` adds AI-generated context to
  already detected components.
- Quality gate: `llm-review: "true"` adds LLM review findings to the static
  quality rules, then applies the same `fail-on` threshold.

For a strict rollout, use `fail-on: "medium"`. For a safer first rollout,
keep `fail-on: "high"` and review the uploaded report artifacts before
tightening the threshold.

## Legacy copy-into-repo workflow

`.github/workflows/ai-stack-scan.yml` runs on every push to `main` (edit
the `branches:` list for your workflow), installs the engine, and commits
`AI_STACK.md` + `ai-stack-report.json` back to the repo — so there's always
a current, version-controlled, diffable log of the AI stack in use. Copy
the `engine/` folder and the workflow file into your actual repo for this
to work (paths in the workflow assume `engine/` sits at the repo root).

If you'd rather not commit directly to a protected branch, swap the last
step for opening a PR instead (e.g. `peter-evans/create-pull-request`), or
switch the trigger to `pull_request` and post the Markdown as a PR comment.

## Extending detection

Everything the scanner recognizes lives in `engine/ai_stack_scanner/
registry.py` as plain dictionaries — no need to touch the AST-walking code:

- `PACKAGE_REGISTRY` — new SDK to recognize by import → add one line.
- `CONSTRUCTOR_REGISTRY` — new "this call means X" signal → add one line.
- `BASE_CLASS_REGISTRY` — new base class to flag on subclassing.
- `MODEL_NAME_PATTERNS` — new model-name regex fallback.

## Known limitations / next steps

- Python only, by design (per your codebase). The same engine/extension
  pattern extends to JS/TS via `@typescript-eslint/typescript-estree` if
  you later need polyglot support — worth a separate pass since the AST
  shapes differ.
- No caching/incremental scanning yet — every scan walks the whole tree.
  Fine for most repos; for very large monorepos you'd want to cache by
  file mtime/hash.
- The tree view is list-based, not a visual graph. A webview with a
  force-directed graph (e.g. showing which agent calls which tool calls
  which MCP server) is a natural v2 if the flat list stops being enough.
