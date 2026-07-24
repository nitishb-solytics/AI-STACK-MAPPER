"""
Single-file AST walker. Given the parsed AST of one .py file, produces a
list of (category, name, package, Occurrence) tuples.

Design notes
------------
We do a single `ast.walk` pass but first build an `import_map` up front
(local name -> (top_level_package, full_dotted_path)) so later nodes can
resolve aliased/renamed imports, e.g.:

    from langchain.tools import tool as lc_tool
    @lc_tool
    def search(...): ...

We also maintain a light `symbol_table` for module/function-level variables
assigned directly from a known constructor call, e.g.:

    mcp = FastMCP("my-server")
    @mcp.tool()
    def search(...): ...

so that `@mcp.tool()` is correctly attributed to MCP rather than treated as
an arbitrary attribute-decorator.

This is a best-effort static analyzer, not a full type checker -- it will
miss dynamic patterns (e.g. tools built from a dict of callables) and can
occasionally mis-tag ambiguous names (e.g. a locally defined `Agent` class
unrelated to any AI framework). Occurrences are tagged with a confidence
level so downstream consumers can filter.
"""
import ast
import re
from typing import Dict, List, Tuple, Optional

from .models import Occurrence, CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW
from .models import DEPLOYMENT_CLOUD, DEPLOYMENT_SELF_HOSTED, DEPLOYMENT_UNKNOWN
from .registry import (
    PACKAGE_REGISTRY,
    CONSTRUCTOR_REGISTRY,
    BASE_CLASS_REGISTRY,
    GENERIC_TOOL_DECORATORS,
    MCP_METHOD_DECORATORS,
    MODEL_NAME_PATTERNS,
    SELF_HOSTED_PACKAGES,
    SELF_HOSTED_CONSTRUCTORS,
    ENDPOINT_OVERRIDE_KWARGS,
    LOCAL_HOST_PATTERNS,
    KNOWN_CLOUD_DOMAINS,
    PROMPT_BEARING_KWARGS,
)
from .models import CATEGORY_MCP, CATEGORY_TOOL

_MODEL_NAME_RE = [(re.compile(p, re.IGNORECASE), label) for p, label in MODEL_NAME_PATTERNS]
_LOCAL_HOST_RE = [re.compile(p, re.IGNORECASE) for p in LOCAL_HOST_PATTERNS]

Finding = Tuple[str, str, str, Occurrence]  # category, name, package, occurrence


def _dotted_name(node: ast.AST) -> Optional[str]:
    """Best-effort reconstruction of a dotted attribute chain, e.g. `a.b.c`."""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class FileVisitor(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.findings: List[Finding] = []
        # local_name -> (top_level_package, full_dotted_path)
        self.import_map: Dict[str, Tuple[str, str]] = {}
        # local_name -> (category, display_name, package)  (tracks e.g. `mcp = FastMCP(...)`)
        self.symbol_table: Dict[str, Tuple[str, str, str]] = {}
        # Stack of human-readable "function `foo` -- \"docstring\"" / "class `Bar`"
        # labels for whatever function/class body we're currently inside.
        # Purely free/static context -- feeds Occurrence.context_hint.
        self._scope_stack: List[str] = []

    def _describe_def(self, node, kind: str) -> str:
        doc = ast.get_docstring(node)
        hint = f"{kind} `{node.name}`"
        if doc:
            snippet = " ".join(doc.split())[:140]
            hint += f' -- "{snippet}"'
        return hint

    def _current_context_hint(self) -> str:
        """Innermost one or two enclosing scopes, e.g. 'class `Foo` > function `run`'."""
        if not self._scope_stack:
            return ""
        return " > ".join(self._scope_stack[-2:])

    # -- pass 1: imports --------------------------------------------------
    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            local = alias.asname or alias.name.split(".")[0]
            self.import_map[local] = (top, alias.name)
            if top in PACKAGE_REGISTRY:
                category, display = PACKAGE_REGISTRY[top]
                deployment_target = DEPLOYMENT_SELF_HOSTED if top in SELF_HOSTED_PACKAGES else DEPLOYMENT_CLOUD
                self.findings.append((
                    category, display, top,
                    Occurrence(
                        self.filename, node.lineno, "import", CONFIDENCE_HIGH,
                        detail=alias.name, deployment_target=deployment_target,
                    ),
                ))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if not node.module:
            self.generic_visit(node)
            return
        top = node.module.split(".")[0]
        for alias in node.names:
            local = alias.asname or alias.name
            full = f"{node.module}.{alias.name}"
            self.import_map[local] = (top, full)
        if top in PACKAGE_REGISTRY:
            category, display = PACKAGE_REGISTRY[top]
            deployment_target = DEPLOYMENT_SELF_HOSTED if top in SELF_HOSTED_PACKAGES else DEPLOYMENT_CLOUD
            self.findings.append((
                category, display, top,
                Occurrence(
                    self.filename, node.lineno, "import", CONFIDENCE_HIGH,
                    detail=node.module, deployment_target=deployment_target,
                ),
            ))
        self.generic_visit(node)

    # -- pass 2: instantiations / assignments ------------------------------
    def visit_Assign(self, node: ast.Assign):
        if isinstance(node.value, ast.Call):
            info = self._classify_call(node.value)
            if info and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                self.symbol_table[node.targets[0].id] = (info[0], info[1], info[2])
        elif isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            self._check_model_literal(node.value.value, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        info = self._classify_call(node)
        if info:
            category, display, package, confidence, deployment_target = info
            detail = ""
            override = self._detect_endpoint_override(node)
            if override:
                deployment_target, override_detail, confidence_override = override
                detail = override_detail
                if confidence_override:
                    confidence = confidence_override
            self.findings.append((
                category, display, package,
                Occurrence(
                    self.filename, node.lineno, "instantiation", confidence,
                    detail=detail, deployment_target=deployment_target,
                    context_hint=self._current_context_hint(),
                ),
            ))
        else:
            # Not a constructor we track, but if it's a method call on a
            # variable we already know is an LLM/MCP client/agent (e.g.
            # `client.chat.completions.create(messages=[...])`), capture any
            # prompt/message text as a low-confidence "usage" occurrence on
            # that same component -- free static context for --enrich.
            dotted = _dotted_name(node.func) if isinstance(node.func, ast.Attribute) else None
            root = dotted.split(".")[0] if dotted else None
            if root and root in self.symbol_table:
                prompt_hint = self._extract_prompt_hint(node)
                if prompt_hint:
                    category, display, package = self.symbol_table[root]
                    self.findings.append((
                        category, display, package,
                        Occurrence(
                            self.filename, node.lineno, "usage", CONFIDENCE_LOW,
                            context_hint=self._current_context_hint(), prompt_hint=prompt_hint,
                        ),
                    ))
        # low-confidence fallback: bare model-name string literal arguments
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                self._check_model_literal(arg.value, node.lineno)
        self.generic_visit(node)

    def _classify_call(self, node: ast.Call):
        """Return (category, display, package, confidence, deployment_target) or None."""
        func = node.func
        name: Optional[str] = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr

        if name and name in CONSTRUCTOR_REGISTRY:
            category, display = CONSTRUCTOR_REGISTRY[name]
            package = ""
            if isinstance(func, ast.Name) and func.id in self.import_map:
                package = self.import_map[func.id][0]
            elif isinstance(func, ast.Attribute):
                dotted = _dotted_name(func)
                root = dotted.split(".")[0] if dotted else ""
                if root in self.import_map:
                    package = self.import_map[root][0]
                    full_import = self.import_map[root][1]
                    if name == "Client" and full_import.startswith("google.genai"):
                        display = "Google GenAI client"
            confidence = CONFIDENCE_HIGH if name not in ("Client", "Server", "Agent") else CONFIDENCE_MEDIUM
            if name == "Client" and package == "google":
                confidence = CONFIDENCE_HIGH
            deployment_target = (
                DEPLOYMENT_SELF_HOSTED
                if (name in SELF_HOSTED_CONSTRUCTORS or package in SELF_HOSTED_PACKAGES)
                else DEPLOYMENT_CLOUD
            )
            return category, display, package, confidence, deployment_target
        return None

    def _detect_endpoint_override(self, node: ast.Call):
        """Inspect constructor keyword arguments for an endpoint override
        (base_url=, api_base=, endpoint=, azure_endpoint=, host=). Returns
        (deployment_target, detail, confidence_override) or None if no
        override keyword is present.

        - Literal value matching localhost/a private IP -> confidently self-hosted.
        - Literal value containing a known vendor cloud domain -> stays cloud.
        - Literal value that's some other custom domain, or a non-literal
          (env var / variable / f-string) -> can't resolve statically, so
          flagged UNKNOWN at medium confidence for manual review rather than
          silently assumed to be cloud.
        """
        for kw in node.keywords:
            if kw.arg not in ENDPOINT_OVERRIDE_KWARGS:
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                val = kw.value.value
                if any(regex.search(val) for regex in _LOCAL_HOST_RE):
                    return DEPLOYMENT_SELF_HOSTED, f"{kw.arg}='{val}'", CONFIDENCE_HIGH
                if any(domain in val for domain in KNOWN_CLOUD_DOMAINS):
                    return DEPLOYMENT_CLOUD, f"{kw.arg}='{val}'", None
                return (
                    DEPLOYMENT_UNKNOWN,
                    f"{kw.arg}='{val}' -- custom endpoint, verify manually",
                    CONFIDENCE_MEDIUM,
                )
            return (
                DEPLOYMENT_UNKNOWN,
                f"{kw.arg} set via non-literal value (env var/variable) -- verify manually",
                CONFIDENCE_MEDIUM,
            )
        return None

    def _extract_prompt_hint(self, node: ast.Call) -> str:
        """Best-effort extraction of prompt/message text from call keyword
        arguments (e.g. `messages=[{"role": "user", "content": "..."}]`,
        `prompt="..."`, `system="..."`). Static only -- never executed,
        never sent anywhere unless --enrich is explicitly enabled.
        """
        for kw in node.keywords:
            if kw.arg not in PROMPT_BEARING_KWARGS:
                continue
            val = kw.value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                return " ".join(val.value.split())[:160]
            if isinstance(val, ast.List):
                for elt in val.elts:
                    if isinstance(elt, ast.Dict):
                        for k, v in zip(elt.keys, elt.values):
                            if (
                                isinstance(k, ast.Constant) and k.value == "content"
                                and isinstance(v, ast.Constant) and isinstance(v.value, str)
                            ):
                                return " ".join(v.value.split())[:160]
        return ""

    def _check_model_literal(self, value: str, lineno: int):
        for regex, label in _MODEL_NAME_RE:
            if regex.match(value.strip()):
                from .models import CATEGORY_LLM
                self.findings.append((
                    CATEGORY_LLM, label, "",
                    Occurrence(
                        self.filename, lineno, "literal", CONFIDENCE_LOW, detail=value,
                        context_hint=self._current_context_hint(),
                    ),
                ))
                break

    # -- pass 3: decorators (tool / MCP registration) -----------------------
    def _visit_function(self, node):
        func_hint = self._describe_def(node, "function")
        for dec in node.decorator_list:
            self._classify_decorator(dec, node.lineno, func_hint)
        self._scope_stack.append(func_hint)
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._visit_function(node)

    def _classify_decorator(self, dec: ast.AST, lineno: int, context_hint: str = ""):
        # @tool  /  @tool(...)
        call_target = dec.func if isinstance(dec, ast.Call) else dec

        if isinstance(call_target, ast.Name):
            if call_target.id in GENERIC_TOOL_DECORATORS:
                src = self.import_map.get(call_target.id, ("", ""))[0]
                self.findings.append((
                    CATEGORY_TOOL, "Tool definition (@tool)", src,
                    Occurrence(self.filename, lineno, "decorator", CONFIDENCE_HIGH, context_hint=context_hint),
                ))
        elif isinstance(call_target, ast.Attribute):
            base_name = _dotted_name(call_target.value)
            attr = call_target.attr
            if base_name in self.symbol_table:
                cat, display, _package = self.symbol_table[base_name]
                if cat == CATEGORY_MCP and attr in MCP_METHOD_DECORATORS:
                    self.findings.append((
                        CATEGORY_MCP, f"MCP {attr} registration (@{base_name}.{attr})", "mcp",
                        Occurrence(self.filename, lineno, "decorator", CONFIDENCE_HIGH, context_hint=context_hint),
                    ))
            elif attr in GENERIC_TOOL_DECORATORS:
                self.findings.append((
                    CATEGORY_TOOL, f"Tool definition (@{base_name}.{attr})", "",
                    Occurrence(self.filename, lineno, "decorator", CONFIDENCE_MEDIUM, context_hint=context_hint),
                ))

    # -- pass 4: class bases ------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef):
        class_hint = self._describe_def(node, "class")
        for base in node.bases:
            base_name = None
            if isinstance(base, ast.Name):
                base_name = base.id
            elif isinstance(base, ast.Attribute):
                base_name = base.attr
            if base_name and base_name in BASE_CLASS_REGISTRY:
                category, display = BASE_CLASS_REGISTRY[base_name]
                self.findings.append((
                    category, f"{display}: {node.name}", "",
                    Occurrence(
                        self.filename, node.lineno, "base_class", CONFIDENCE_HIGH, detail=base_name,
                        context_hint=class_hint,
                    ),
                ))
        self._scope_stack.append(class_hint)
        self.generic_visit(node)
        self._scope_stack.pop()


def scan_source(filename: str, source: str) -> List[Finding]:
    """Parse `source` and return all findings. Raises SyntaxError on bad input."""
    tree = ast.parse(source, filename=filename)
    visitor = FileVisitor(filename)
    visitor.visit(tree)
    return visitor.findings
