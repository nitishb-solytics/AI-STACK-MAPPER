"""Build the Vault staged local-agent discovery payload from scan reports.

This is the single source of truth for agent identity (`external_id`) and
change detection (`content_hash`). Every discovery entry point -- VS Code
extension, local file-path CLI, and (later) a GitHub-fetch job -- must call
this module rather than reimplementing the hashing rules, otherwise the same
repo scanned via two different methods would produce two different
`external_id`s and Vault would create duplicate AIAgent records for one
logical agent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional


def normalise_repo_url(repo_url: str) -> str:
    """Reduce cosmetically different spellings of one repo URL to one key.

    The extension asks the user to free-type this URL, so the same repository
    reaches Vault as ``https://github.com/Org/Repo.git``, ``.../org/repo``, or
    ``git@github.com:org/repo.git`` depending on who ran the scan. Feeding
    those straight into ``agent_external_id`` would mint a different
    ``external_id`` per spelling and leave Vault holding duplicate AIAgent
    records for a single logical agent -- the exact failure this module exists
    to prevent. Scheme, credentials, ``.git`` suffix, trailing slash and case
    are all dropped, so every form above collapses to ``github.com/org/repo``.
    """
    url = (repo_url or "").strip()
    if not url:
        return ""
    url = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", "", url)  # scheme
    url = re.sub(r"^[^/@]*@", "", url)                       # user[:password]@ / git@
    url = re.sub(r"^([^/]+):(?=[^0-9])", r"\1/", url)        # scp-style host:path (not host:port)
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[: -len(".git")]
    return url.rstrip("/").lower()


def repo_identity(repo_root: str, repo_url: str = "") -> str:
    normalised = normalise_repo_url(repo_url)
    if normalised:
        return normalised
    # No URL given: fall back to the local path. This is machine-specific, so
    # two people scanning the same checkout stage separate agents -- pass a
    # repo URL whenever a stable cross-machine identity is wanted.
    return repo_root.replace("\\", "/").rstrip("/").lower()


def _stable(value: Any) -> Any:
    if isinstance(value, list):
        return [_stable(v) for v in value]
    if isinstance(value, dict):
        return {k: _stable(value[k]) for k in sorted(value.keys())}
    return value


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def agent_external_id(identity: str, agent_key: str) -> str:
    return _sha1(f"{identity}:{agent_key}")


def agent_content_hash(agent: Dict[str, Any]) -> str:
    fingerprint = {
        "files": agent.get("files") or [],
        "dependency_files": agent.get("dependency_files") or [],
        "components": agent.get("components") or {},
        "evidence": agent.get("evidence") or [],
    }
    return _sha1(json.dumps(_stable(fingerprint), sort_keys=True))


def build_local_agents_payload(
    identity: str,
    agents: List[Dict[str, Any]],
    selected_keys: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    if selected_keys is not None:
        selected = set(selected_keys)
        agents = [a for a in agents if a.get("key") in selected]
    return [
        {
            "external_id": agent_external_id(identity, agent["key"]),
            "content_hash": agent_content_hash(agent),
            "key": agent.get("key"),
            "name": agent.get("name"),
            "score": agent.get("score"),
            "files": agent.get("files") or [],
            "dependency_files": agent.get("dependency_files") or [],
            "components": agent.get("components") or {},
            "evidence": agent.get("evidence") or [],
        }
        for agent in agents
    ]


def build_vault_payload(
    repo_root: str,
    stack: Dict[str, Any],
    repo_url: str = "",
    branch: str = "",
    selected_keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    identity = repo_identity(repo_root, repo_url)
    agents = stack.get("local_agents") or []
    return {
        "provider_key": "local_agent",
        "source_type": "vscode_extension",
        "repo_name": os.path.basename(repo_root.replace("\\", "/").rstrip("/")) or repo_root,
        "repo_path": repo_root,
        "repo_url": repo_url or "",
        "branch": branch or "",
        "stack": stack,
        "risk": {},
        "local_agents": build_local_agents_payload(identity, agents, selected_keys),
        "discover": False,
        "include_risks": False,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a Vault staged local-agent discovery payload from an AI Stack JSON report."
    )
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--stack-json", required=True, help="Path to ai-stack-report.json")
    parser.add_argument("--repo-url", default="")
    parser.add_argument("--branch", default="")
    parser.add_argument(
        "--selected-keys", default=None,
        help="Comma-separated local agent keys to include (default: all detected agents).",
    )
    parser.add_argument("--output", default=None, help="Write payload JSON here instead of stdout.")
    args = parser.parse_args(argv)

    with open(args.stack_json, "r", encoding="utf-8") as f:
        stack = json.load(f)

    selected_keys = None
    if args.selected_keys is not None:
        selected_keys = [k for k in args.selected_keys.split(",") if k]

    payload = build_vault_payload(
        repo_root=args.repo_root,
        stack=stack,
        repo_url=args.repo_url,
        branch=args.branch,
        selected_keys=selected_keys,
    )

    text = json.dumps(payload)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
