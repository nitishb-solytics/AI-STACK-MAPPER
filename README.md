# AI Stack Mapper - Local Agent Vault Discovery

VS Code extension POC for discovering local AI agents in a repository and publishing the
discovery inventory into MRM Vault AI Governance.

## Documentation

Two companion documents are maintained outside this repository and shared directly:

- *Internal Local Agent-to-Vault Implementation and Setup Guide* for Vault engineering, DevOps, database, frontend, and QA teams.
- *AI Stack Mapper Client Quick Start*, the shorter guide to give an extension user or client.

This extension does not onboard agents directly. It stages discovered local agents in Vault.
Vault users then open AIAgent → Auto Discovery → Agent Discovery in Local, review the agents,
and import only the selected agent(s).

## Flow

```text
VS Code target repo
  → AI Stack scan
  → Publish Local Agent Discovery to Vault
  → Vault stores staged inventory
  → Vault Auto Discovery lists "Agent Discovery in Local"
  → user selects agents in Vault
  → Vault creates AIAgent/components
```

## Local demo setup

Use two repositories/windows:

```text
ai-stack-mapper
  Runs the VS Code extension in Extension Development Host.

target repository
  The repo being scanned, for example enterprise-rag.
```

Steps:

1. Open this repo in VS Code.
2. Run `npm install` inside `extension/` if dependencies are not installed.
3. Press F5 / Run Extension.
4. In the Extension Development Host window, open the target repository.
5. Run `AI Stack: Scan Workspace`.
6. Run `AI Stack: Publish Local Agent Discovery to Vault`.

The publish command asks for:

- Vault backend base URL, for example `http://127.0.0.1:8000`
- Vault org header, for example `ai-gov-3`
- Vault access token (`Bearer <token>`; entering only the token is also supported)
- optional repo URL and branch metadata

## Vault backend requirement

The Vault backend must expose:

```http
POST /vault/ai-governance/codebase-discovery
```

This endpoint stages scanner inventory only.

The backend must also expose:

```http
POST /vault/ai-governance/providers
```

with body:

```json
{
  "include_agents": true
}
```

Expected provider key:

```text
local_agent
```

Selected import happens from Vault using:

```http
POST /vault/ai-governance/discover
```

with body:

```json
{
  "discover": true,
  "provider": "local_agent",
  "external_ids": ["selected-agent-external-id"]
}
```

## Files that matter

```text
extension/package.json
extension/tsconfig.json
extension/src/extension.ts
extension/src/scannerBridge.ts
extension/src/treeViewProvider.ts
extension/python/ai_stack_scanner/
extension/resources/icon.svg
```
