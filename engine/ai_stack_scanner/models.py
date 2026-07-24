"""Data models used throughout the scanner."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional


# Categories we bucket every detected component into.
CATEGORY_LLM = "LLM"
CATEGORY_MCP = "MCP"
CATEGORY_TOOL = "TOOL"
CATEGORY_AGENT_FRAMEWORK = "AGENT_FRAMEWORK"
CATEGORY_VECTOR_STORE = "VECTOR_STORE"

ALL_CATEGORIES = [
    CATEGORY_LLM,
    CATEGORY_MCP,
    CATEGORY_TOOL,
    CATEGORY_AGENT_FRAMEWORK,
    CATEGORY_VECTOR_STORE,
]

CATEGORY_LABELS = {
    CATEGORY_LLM: "LLM Providers",
    CATEGORY_MCP: "MCP (Model Context Protocol)",
    CATEGORY_TOOL: "Tools / Function Calling",
    CATEGORY_AGENT_FRAMEWORK: "Agent & Orchestration Frameworks",
    CATEGORY_VECTOR_STORE: "Vector Stores / Memory",
}

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

_CONFIDENCE_RANK = {CONFIDENCE_LOW: 0, CONFIDENCE_MEDIUM: 1, CONFIDENCE_HIGH: 2}


# Where the detected component actually talks to, independent of confidence:
# CLOUD is the default (vendor-hosted API, e.g. api.openai.com). SELF_HOSTED
# means the code points at a local/on-prem/custom endpoint (Ollama, vLLM, a
# base_url override resolved to localhost/a private IP). UNKNOWN means an
# endpoint override was detected but couldn't be statically resolved (e.g.
# set from an env var or built at runtime) -- flagged for manual review
# rather than silently assumed to be cloud.
DEPLOYMENT_CLOUD = "cloud"
DEPLOYMENT_SELF_HOSTED = "self_hosted"
DEPLOYMENT_UNKNOWN = "unknown"

DEPLOYMENT_LABELS = {
    DEPLOYMENT_CLOUD: "Cloud",
    DEPLOYMENT_SELF_HOSTED: "Self-hosted / On-prem",
    DEPLOYMENT_UNKNOWN: "Unknown (verify manually)",
}


@dataclass
class Occurrence:
    file: str
    line: int
    match_type: str  # import | instantiation | decorator | base_class | config | dependency | env_var | literal | usage
    confidence: str
    detail: str = ""
    deployment_target: str = DEPLOYMENT_CLOUD  # cloud | self_hosted | unknown
    # Free, static, always-on context -- the enclosing function/class name
    # and its docstring (context_hint), and any prompt/message text found in
    # a call's keyword arguments (prompt_hint). Both are best-effort hints,
    # not verified summaries, and feed the optional LLM enrichment step.
    context_hint: str = ""
    prompt_hint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Component:
    """A single detected AI-stack component, aggregated across all occurrences."""
    category: str
    name: str
    package: str = ""
    packages: List[str] = field(default_factory=list)
    occurrences: List[Occurrence] = field(default_factory=list)
    # Populated only when --enrich is used (optional, LLM-generated, off by
    # default). Unlike everything else on this class, this is probabilistic
    # and unverified -- treat it as a hint, not a fact.
    ai_enrichment: Optional[Dict[str, str]] = None

    @property
    def confidence(self) -> str:
        if not self.occurrences:
            return CONFIDENCE_LOW
        return max((o.confidence for o in self.occurrences), key=lambda c: _CONFIDENCE_RANK.get(c, 0))

    @property
    def count(self) -> int:
        return len(self.occurrences)

    @property
    def deployment_targets(self) -> List[str]:
        """Distinct deployment targets seen across all occurrences (e.g. a
        repo might use the same SDK against both a cloud and a self-hosted
        endpoint in different files). Empty/no-signal defaults to cloud."""
        if not self.occurrences:
            return [DEPLOYMENT_CLOUD]
        return sorted({o.deployment_target for o in self.occurrences})

    def to_dict(self) -> Dict[str, Any]:
        packages = sorted({p for p in ([self.package] + self.packages) if p})
        d: Dict[str, Any] = {
            "category": self.category,
            "name": self.name,
            "package": ", ".join(packages),
            "packages": packages,
            "confidence": self.confidence,
            "count": self.count,
            "deployment_targets": self.deployment_targets,
            "occurrences": [o.to_dict() for o in sorted(self.occurrences, key=lambda o: (o.file, o.line))],
        }
        if self.ai_enrichment is not None:
            d["ai_enrichment"] = self.ai_enrichment
        return d


@dataclass
class ScanResult:
    root: str
    generated_at: str
    scanned_files: int
    scanner_mode: str = "static"
    skipped_files: List[str] = field(default_factory=list)
    components: Dict[str, Component] = field(default_factory=dict)  # key: category|name

    def add(self, category: str, name: str, package: str, occurrence: Occurrence) -> None:
        key = f"{category}|{name}"
        if key not in self.components:
            self.components[key] = Component(category=category, name=name, package=package)
        elif package and package not in self.components[key].packages and package != self.components[key].package:
            self.components[key].packages.append(package)
        self.components[key].occurrences.append(occurrence)

    def to_dict(self) -> Dict[str, Any]:
        by_category: Dict[str, List[Dict[str, Any]]] = {c: [] for c in ALL_CATEGORIES}
        for comp in self.components.values():
            by_category.setdefault(comp.category, []).append(comp.to_dict())
        for cat in by_category:
            by_category[cat].sort(key=lambda c: (-c["count"], c["name"]))
        return {
            "root": self.root,
            "generated_at": self.generated_at,
            "scanner_mode": self.scanner_mode,
            "scanned_files": self.scanned_files,
            "skipped_files": self.skipped_files,
            "total_components": len(self.components),
            "categories": by_category,
        }
