# AI Stack Mapper

AI Stack Mapper scans a Python repository and generates a structured inventory
of the AI stack used in that codebase.

It identifies:

- LLM providers and model clients
- Agent/orchestration frameworks
- Tool/function-calling definitions
- MCP servers/clients/configuration
- Vector stores and memory layers
- Model-name literals and AI-related dependency/config signals

The output is generated as:

- `AI_STACK.md` - human-readable Markdown report
- `ai-stack-report.json` - structured machine-readable report
- optional `ai-quality-report.md/json` - PR quality-gate report

The same engine can be used locally, from the VS Code extension, or inside
GitHub Actions.

## Why this tool exists

Modern Python repos often use LLMs, agents, vector databases, prompt chains,
MCP servers, and framework wrappers across many files. It becomes difficult to
answer simple questions like:

- Which LLM providers are used?
- Which agent frameworks are used?
- Where are tools or MCP servers registered?
- Which vector stores or memory layers are present?
- Is this usage coming from real code, dependency files, config files, or LLM
  discovery?
- Can the report also explain the likely purpose, usage, and output of each
  model/component?

AI Stack Mapper is built to answer those questions in CI and local developer
workflows.

## Detection modes

The inventory scanner supports three modes.

| Mode | Uses LLM? | Description | Recommended use |
|---|---:|---|---|
| `static` | No | Deterministic Python AST + dependency/config scan. | Default and safest mode. |
| `llm` | Yes | LLM-only semantic discovery from small redacted snippets and config evidence. | Experiments or semantic discovery checks. |
| `hybrid` | Yes | Runs static scan first, then adds LLM-discovered findings into the same report. | Best coverage when an API key is available. |

Default mode is:

```yaml
scanner-mode: "static"
```

So by default the tool does not call any LLM.

To use an LLM, the repo owner must explicitly configure:

```yaml
scanner-mode: "hybrid"
llm-api-key: ${{ secrets.AI_STACK_LLM_API_KEY }}
llm-base-url: "https://openrouter.ai/api/v1"
llm-model: "google/gemma-4-26b-a4b-it:free"
```

If `scanner-mode` is `llm` or `hybrid` but no API key is configured, the
scanner safely falls back to static mode and still produces a report.

## Static scanner

The static scanner does not execute project code. It reads files and uses
deterministic rules.

It scans:

- Python source files with Python `ast`
- `requirements.txt`
- `pyproject.toml`
- `Pipfile`
- `package.json`
- MCP config files such as `mcp.json`, `.mcp.json`,
  `claude_desktop_config.json`
- `.env` / `.env.example` key names only, never secret values

Static detections include:

| Signal | Example | Confidence |
|---|---|---|
| Import | `from openai import OpenAI` | high |
| Instantiation | `ChatOpenAI(...)`, `Agent(...)`, `FastMCP(...)` | high/medium |
| Decorator | `@tool`, `@mcp.tool()` | high |
| Base class | `class MyTool(BaseTool)` | high |
| Dependency | `crewai`, `openai`, `langchain` in dependency files | medium |
| MCP config | `mcpServers` block in config JSON | medium |
| Env key name | `OPENAI_API_KEY=` | low |
| Model literal | `"gpt-4o"`, `"claude-..."`, `"gemini-..."` | low |

Static mode is fast, reproducible, and safe for private repos, but it can miss
custom/dynamic code patterns that do not match known imports/classes/packages.

## LLM scanner / semantic discovery

The optional LLM scanner is used when:

```yaml
scanner-mode: "llm"
```

or:

```yaml
scanner-mode: "hybrid"
```

This is not an autonomous multi-step agent. It is an LLM-assisted semantic
discovery layer. It sends bounded, redacted evidence to an OpenAI-compatible
LLM and asks it to identify AI stack components that static rules may miss.

The LLM receives limited evidence such as:

- snippets around likely agent/tool/LLM/vector-store code
- dependency/config summaries
- repo description or package metadata
- `.env` key names only

It should not receive full source files or secret values.

LLM-discovered findings are marked in JSON as:

```json
"match_type": "llm_discovery"
```

Example:

```json
{
  "file": "pyproject.toml",
  "line": 1,
  "match_type": "llm_discovery",
  "confidence": "high",
  "detail": "The repository description explicitly identifies CrewAI as a framework for orchestrating autonomous AI agents."
}
```

This allows the report consumer to distinguish deterministic static findings
from LLM-predicted findings.

## Optional LLM enrichment

LLM enrichment is separate from LLM discovery.

Discovery answers:

```text
What AI components are present?
```

Enrichment answers:

```text
For each detected component, what does it appear to be used for?
```

Enable enrichment with:

```yaml
enrich: "true"
```

When enabled, the scanner asks the configured LLM to infer textual attributes
for each detected component.

Current enrichment fields:

| Field | Meaning |
|---|---|
| `purpose` | Why this model/component appears to exist in the codebase. |
| `usage_description` | How the code appears to use it. |
| `expected_output` | What output/response it likely produces. |
| `model` | LLM model used to generate the enrichment. |

These map to lead/client-friendly labels:

| Lead-facing label | JSON field |
|---|---|
| Model Purpose | `ai_enrichment.purpose` |
| Model Usage Description | `ai_enrichment.usage_description` |
| Model Output | `ai_enrichment.expected_output` |

Example JSON shape:

```json
{
  "category": "LLM",
  "name": "OpenAI chat model",
  "confidence": "high",
  "ai_enrichment": {
    "purpose": "Used to answer user questions from retrieved document context.",
    "usage_description": "The model is called from the query pipeline after retrieval.",
    "expected_output": "Natural-language answer text.",
    "model": "google/gemma-4-26b-a4b-it:free"
  }
}
```

Important: enrichment is probabilistic and should be treated as an
LLM-generated suggestion for human review, not as a deterministic fact.

## Hybrid mode vs static mode

Static mode is best when you want deterministic, no-network, low-risk scanning.

Hybrid mode is useful when you want better coverage.

Hybrid can help identify:

- custom agent classes not using a known framework
- internal wrappers around LLM clients
- workflow/planner/orchestrator code
- new AI libraries not yet added to the static registry
- semantic use cases visible from descriptions, prompts, or file structure
- extra textual context that static rules cannot infer

Recommended production setup:

```yaml
scanner-mode: "hybrid"
enrich: "true"
```

Use `hybrid` when an API key is available and the repo owner is comfortable
sending bounded, redacted code evidence to the configured LLM endpoint.

Use `static` when the repo must avoid all outbound LLM calls.

## Match types

Each occurrence in `ai-stack-report.json` contains a `match_type` field.

| `match_type` | Meaning | Source |
|---|---|---|
| `import` | Found from Python import statements. | Static AST |
| `instantiation` | Found from constructor/client/model creation. | Static AST |
| `decorator` | Found from decorators such as `@tool` or `@mcp.tool()`. | Static AST |
| `base_class` | Found from class inheritance. | Static AST |
| `usage` | Found from usage of a known client with visible prompt/message text. | Static AST |
| `dependency` | Found from dependency files. | Static config scan |
| `config` | Found from MCP/config files. | Static config scan |
| `env_var` | Found from environment variable key names only. | Static env scan |
| `literal` | Found from model-name string literals. | Static heuristic |
| `llm_discovery` | Found by optional LLM semantic discovery. | LLM scanner |

This makes it clear whether a finding was deterministic or LLM-assisted.

## Code assessment risk scan

AI Stack Mapper also includes a code assessment risk scanner:

```bash
ai-risk-scan
```

It scans Python code for codebase-level data/control risks and produces:

- `AI_RISK_REPORT.md`
- `ai-risk-report.json`

It supports:

- deterministic static AST risk detection
- optional LLM-generated risk controls
- severity threshold for CI/PR quality gates

### Static risk/control checks

The risk scanner performs static AST-based risk checks against Python code.
These rules are deterministic and run without an LLM unless LLM risk controls
are explicitly enabled.

Current static risk coverage includes:

| Area | Example risk detected |
|---|---|
| LLM reliability | LLM client/call without visible timeout or retry configuration |
| LLM cost/control | LLM client/model without visible output token limit |
| Context growth | Message/history list appended without visible trimming, summarization, or token budgeting |
| Prompt injection | User-controlled input passed directly into prompt/messages |
| Prompt quality | Prompt-like content without clear output contract |
| Agent control | Agent/Crew created without max iteration or execution-time limit |
| Agent tool scope | Agent configured with a broad tool list |
| Agent delegation | Delegation enabled without visible iteration bounds |
| Tool safety | Agent tool performs sensitive file/process operations |
| Tool input validation | Tool function parameters missing type annotations/schema |
| RAG retrieval | Vector search missing explicit `k`/`top_k`/`limit` |
| RAG grounding | Vector search missing score threshold or metadata filter |
| TextToSQL safety | Generated SQL execution without visible validation/read-only controls |

Each finding includes file/line, severity, rule ID, feature area, and a fallback
static suggestion. When LLM controls are enabled, the scanner extracts a focused
code snapshot around each detected risk and sends only that snapshot plus risk
metadata to the configured LLM. The LLM returns risk explanation, recommended
control, safer code, confidence, and whether it agrees the finding is valid.

In CI, `fail-on: "high"` blocks only high/critical findings; `fail-on:
"medium"` is stricter.

Example:

```yaml
mode: "risk"
changed-only: "true"
base-ref: ${{ github.base_ref }}
fail-on: "high"
llm-risk-control: "true"
```

If a finding is at or above the configured threshold, the action exits with a
non-zero status and can block merge when configured as a required GitHub check.

## GitHub Action usage

The GitHub Action supports three main modes:

| Mode | Purpose | Typical output |
|---|---|---|
| `stack` | Component discovery only | `AI_STACK.md`, `ai-stack-report.json` |
| `risk` | Code assessment risk scan only | `AI_RISK_REPORT.md`, `ai-risk-report.json` |
| `both` | Run stack and risk scans together | all four reports |

Example workflow for stack inventory on push, risk report on push, and PR
quality gate on pull requests:

```yaml
name: AI Stack and Risk Scan

on:
  push:
    branches:
      - main
    paths-ignore:
      - "AI_STACK.md"
      - "ai-stack-report.json"
      - "AI_RISK_REPORT.md"
      - "ai-risk-report.json"

  pull_request:
    branches:
      - main

  workflow_dispatch:

permissions:
  contents: write
  pull-requests: read

jobs:
  stack-scan:
    if: github.event_name == 'push' || github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Run AI Stack Mapper scan
        uses: nitishb-solytics/AI-STACK-MAPPER@main
        with:
          path: "."
          mode: "stack"
          scanner-mode: "hybrid"
          enrich: "true"
          llm-api-key: ${{ secrets.AI_STACK_LLM_API_KEY }}
          llm-base-url: "https://openrouter.ai/api/v1"
          llm-model: "google/gemma-4-26b-a4b-it:free"
          markdown-output: "AI_STACK.md"
          json-output: "ai-stack-report.json"

      - name: Validate stack reports
        shell: bash
        run: |
          test -s AI_STACK.md
          test -s ai-stack-report.json
          python -m json.tool ai-stack-report.json > /dev/null

      - name: Commit updated stack reports
        shell: bash
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add AI_STACK.md ai-stack-report.json
          if git diff --cached --quiet; then
            echo "No AI stack changes detected."
            exit 0
          fi
          git commit -m "chore: update AI stack report"
          git push

  risk-scan:
    if: github.event_name == 'push' || github.event_name == 'workflow_dispatch'
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Run code assessment risk scan
        uses: nitishb-solytics/AI-STACK-MAPPER@main
        with:
          path: "."
          mode: "risk"
          risk-no-fail: "true"
          llm-risk-control: "true"
          llm-api-key: ${{ secrets.AI_STACK_LLM_API_KEY }}
          llm-base-url: "https://openrouter.ai/api/v1"
          llm-model: "google/gemma-4-26b-a4b-it:free"
          risk-output: "AI_RISK_REPORT.md"
          risk-json-output: "ai-risk-report.json"

      - name: Validate risk reports
        shell: bash
        run: |
          test -s AI_RISK_REPORT.md
          test -s ai-risk-report.json
          python -m json.tool ai-risk-report.json > /dev/null

      - name: Commit updated risk reports
        shell: bash
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add AI_RISK_REPORT.md ai-risk-report.json
          if git diff --cached --quiet; then
            echo "No AI risk changes detected."
            exit 0
          fi
          git commit -m "chore: update AI risk report"
          git push

  pr-quality-gate:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest

    permissions:
      contents: read
      pull-requests: read

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Run PR risk quality gate
        uses: nitishb-solytics/AI-STACK-MAPPER@main
        with:
          path: "."
          mode: "risk"
          changed-only: "true"
          base-ref: ${{ github.base_ref }}
          fail-on: "high"
          llm-risk-control: "true"
          llm-api-key: ${{ secrets.AI_STACK_LLM_API_KEY }}
          llm-base-url: "https://openrouter.ai/api/v1"
          llm-model: "google/gemma-4-26b-a4b-it:free"
          risk-output: "AI_RISK_REPORT.md"
          risk-json-output: "ai-risk-report.json"

      - name: Upload PR risk report
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: ai-risk-report
          path: |
            AI_RISK_REPORT.md
            ai-risk-report.json
          if-no-files-found: error
```

## Local usage

Install the engine:

```bash
cd engine
pip install -e .
```

Static scan:

```bash
ai-stack-scan --path . --scanner-mode static --markdown-output AI_STACK.md --json-output ai-stack-report.json
```

Hybrid scan with LLM discovery:

```bash
ai-stack-scan --path . \
  --scanner-mode hybrid \
  --llm-api-key "$AI_STACK_LLM_API_KEY" \
  --llm-base-url "https://openrouter.ai/api/v1" \
  --llm-model "google/gemma-4-26b-a4b-it:free" \
  --markdown-output AI_STACK.md \
  --json-output ai-stack-report.json
```

Hybrid scan with LLM discovery plus enrichment:

```bash
ai-stack-scan --path . \
  --scanner-mode hybrid \
  --enrich \
  --llm-api-key "$AI_STACK_LLM_API_KEY" \
  --llm-base-url "https://openrouter.ai/api/v1" \
  --llm-model "google/gemma-4-26b-a4b-it:free" \
  --markdown-output AI_STACK.md \
  --json-output ai-stack-report.json
```

Risk scan:

```bash
ai-risk-scan --path . --report-title "AI Risk Report" --fail-on high --markdown-output AI_RISK_REPORT.md --json-output ai-risk-report.json
```

Risk scan with LLM controls:

```bash
ai-risk-scan --path . \
  --report-title "AI Risk Report" \
  --fail-on high \
  --llm-risk-control \
  --llm-api-key "$AI_STACK_LLM_API_KEY" \
  --llm-base-url "https://openrouter.ai/api/v1" \
  --llm-model "google/gemma-4-26b-a4b-it:free" \
  --markdown-output AI_RISK_REPORT.md \
  --json-output ai-risk-report.json
```

## VS Code extension

The VS Code extension uses the same Python engine and currently runs safely in
static mode by default.

Current extension behavior:

```text
VS Code Extension -> ai-stack-scan -> static scan
```

The engine already supports `llm` and `hybrid` modes. To expose those in the
extension UI, extension settings can be added later for:

- scanner mode
- LLM base URL
- LLM model
- LLM API key/secret handling
- enrichment enable/disable

## How to explain the LLM part to leads

Short answer:

```text
Yes, LLM usage is supported, but it is optional.
By default the scanner is static. If enabled, the LLM is used for semantic
discovery and enrichment.
```

More detailed answer:

```text
AI Stack Mapper has a deterministic static scanner and an optional LLM-assisted
scanner.

The static scanner finds known packages, imports, constructors, decorators,
model literals, vector stores, MCP config, and dependency signals.

The optional LLM scanner can identify semantic/custom agent or model usage that
static rules may miss. LLM-discovered findings are marked as match_type =
llm_discovery.

The optional enrichment step can predict textual attributes such as Model
Purpose, Model Usage Description, and Model Output. These are added under the
ai_enrichment field and should be reviewed as suggestions.
```

## Privacy and safety

- Static mode makes no LLM/API calls.
- LLM mode and hybrid mode require explicit configuration.
- Secrets should be passed through GitHub Secrets, not committed in `.env`.
- `.env` files in target repos are scanned by key name only, not value.
- LLM discovery uses bounded/redacted evidence, not full raw repository dumps.
- LLM-generated fields are marked separately and should be treated as
  suggestions.

## Extending static detection

Static detection rules live in:

```text
engine/ai_stack_scanner/registry.py
```

To add support for a new SDK/framework, update:

- `PACKAGE_REGISTRY`
- `CONSTRUCTOR_REGISTRY`
- `BASE_CLASS_REGISTRY`
- `MODEL_NAME_PATTERNS`
- JS/package fallback registries if package.json support is needed

## Known limitations

- The main AST scanner is focused on Python repositories.
- `package.json` is scanned as dependency/config evidence only; JS/TS AST
  scanning is not currently implemented.
- LLM discovery and enrichment are probabilistic.
- LLM-only mode can return no results if the model/API is rate-limited.
  Production CI should prefer `hybrid`.
- Very dynamic runtime patterns may still need manual review.
