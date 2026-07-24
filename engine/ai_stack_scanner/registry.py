"""
Knowledge base of known packages, constructors, decorators and base classes
for LLM / MCP / Tool / Agent-framework / Vector-store detection.

This is intentionally a plain data file (dicts of dicts) so it's easy to
extend without touching the AST-walking logic in ast_visitor.py. To add a
new SDK, add one line to PACKAGE_REGISTRY. To add a new "clearly this is an
LLM client instantiation" style signal, add one line to CONSTRUCTOR_REGISTRY.
"""
from .models import (
    CATEGORY_LLM,
    CATEGORY_MCP,
    CATEGORY_TOOL,
    CATEGORY_AGENT_FRAMEWORK,
    CATEGORY_VECTOR_STORE,
    DEPLOYMENT_CLOUD,
    DEPLOYMENT_SELF_HOSTED,
    DEPLOYMENT_UNKNOWN,
)

# ---------------------------------------------------------------------------
# 1) Top-level import package -> (category, display name)
#    Matches `import X` / `from X import ...` / `from X.sub import ...`
#    Keyed on the FIRST dotted component of the imported module.
# ---------------------------------------------------------------------------
PACKAGE_REGISTRY = {
    # LLM providers / SDKs
    "openai": (CATEGORY_LLM, "OpenAI"),
    "anthropic": (CATEGORY_LLM, "Anthropic"),
    "google": (CATEGORY_LLM, "Google Gemini / Vertex AI"),  # google.generativeai, google.genai, google.cloud.aiplatform
    "cohere": (CATEGORY_LLM, "Cohere"),
    "mistralai": (CATEGORY_LLM, "Mistral AI"),
    "groq": (CATEGORY_LLM, "Groq"),
    "together": (CATEGORY_LLM, "Together AI"),
    "litellm": (CATEGORY_LLM, "LiteLLM (multi-provider proxy)"),
    "ollama": (CATEGORY_LLM, "Ollama (local)"),
    "replicate": (CATEGORY_LLM, "Replicate"),
    "transformers": (CATEGORY_LLM, "Hugging Face Transformers"),
    "huggingface_hub": (CATEGORY_LLM, "Hugging Face Hub"),
    "vertexai": (CATEGORY_LLM, "Google Vertex AI"),
    "vllm": (CATEGORY_LLM, "vLLM (local inference server)"),
    "localai": (CATEGORY_LLM, "LocalAI (self-hosted)"),
    "langchain_aws": (CATEGORY_LLM, "AWS Bedrock (via LangChain)"),

    # MCP
    "mcp": (CATEGORY_MCP, "Model Context Protocol SDK"),
    "fastmcp": (CATEGORY_MCP, "FastMCP"),

    # Agent / orchestration frameworks
    "langchain": (CATEGORY_AGENT_FRAMEWORK, "LangChain"),
    "langchain_core": (CATEGORY_AGENT_FRAMEWORK, "LangChain"),
    "langchain_community": (CATEGORY_AGENT_FRAMEWORK, "LangChain"),
    "langchain_openai": (CATEGORY_AGENT_FRAMEWORK, "LangChain"),
    "langchain_anthropic": (CATEGORY_AGENT_FRAMEWORK, "LangChain"),
    "langchain_text_splitters": (CATEGORY_AGENT_FRAMEWORK, "LangChain"),
    "langchain_huggingface": (CATEGORY_AGENT_FRAMEWORK, "LangChain"),
    "langchain_google_genai": (CATEGORY_AGENT_FRAMEWORK, "LangChain"),
    "langgraph": (CATEGORY_AGENT_FRAMEWORK, "LangGraph"),
    "llama_index": (CATEGORY_AGENT_FRAMEWORK, "LlamaIndex"),
    "haystack": (CATEGORY_AGENT_FRAMEWORK, "Haystack"),
    "semantic_kernel": (CATEGORY_AGENT_FRAMEWORK, "Semantic Kernel"),
    "autogen": (CATEGORY_AGENT_FRAMEWORK, "AutoGen"),
    "pyautogen": (CATEGORY_AGENT_FRAMEWORK, "AutoGen"),
    "ag2": (CATEGORY_AGENT_FRAMEWORK, "AG2 (AutoGen)"),
    "crewai": (CATEGORY_AGENT_FRAMEWORK, "CrewAI"),
    "dspy": (CATEGORY_AGENT_FRAMEWORK, "DSPy"),
    "guidance": (CATEGORY_AGENT_FRAMEWORK, "Guidance"),
    "pydantic_ai": (CATEGORY_AGENT_FRAMEWORK, "Pydantic AI"),
    "agno": (CATEGORY_AGENT_FRAMEWORK, "Agno"),
    "phi": (CATEGORY_AGENT_FRAMEWORK, "Phidata"),
    "instructor": (CATEGORY_AGENT_FRAMEWORK, "Instructor (structured outputs)"),

    # Vector stores / memory
    "chromadb": (CATEGORY_VECTOR_STORE, "Chroma"),
    "pinecone": (CATEGORY_VECTOR_STORE, "Pinecone"),
    "weaviate": (CATEGORY_VECTOR_STORE, "Weaviate"),
    "qdrant_client": (CATEGORY_VECTOR_STORE, "Qdrant"),
    "faiss": (CATEGORY_VECTOR_STORE, "FAISS"),
    "lancedb": (CATEGORY_VECTOR_STORE, "LanceDB"),
    "pgvector": (CATEGORY_VECTOR_STORE, "pgvector"),
    "pymilvus": (CATEGORY_VECTOR_STORE, "Milvus"),
    "langchain_milvus": (CATEGORY_VECTOR_STORE, "Milvus (via LangChain)"),
}

# ---------------------------------------------------------------------------
# 2) Known constructor / client class names -> (category, display name)
#    Matches direct instantiation calls like `OpenAI(...)`, `ChatOpenAI(...)`,
#    regardless of how the name was imported/aliased. Gives higher-confidence,
#    precise file:line usage sites (as opposed to just "this file imports X").
# ---------------------------------------------------------------------------
CONSTRUCTOR_REGISTRY = {
    "OpenAI": (CATEGORY_LLM, "OpenAI client"),
    "AzureOpenAI": (CATEGORY_LLM, "Azure OpenAI client"),
    "AsyncOpenAI": (CATEGORY_LLM, "OpenAI client (async)"),
    "Anthropic": (CATEGORY_LLM, "Anthropic client"),
    "AnthropicVertex": (CATEGORY_LLM, "Anthropic client (Vertex)"),
    "AsyncAnthropic": (CATEGORY_LLM, "Anthropic client (async)"),
    "ChatOpenAI": (CATEGORY_LLM, "OpenAI chat model (LangChain)"),
    "ChatAnthropic": (CATEGORY_LLM, "Anthropic chat model (LangChain)"),
    "ChatGoogleGenerativeAI": (CATEGORY_LLM, "Gemini chat model (LangChain)"),
    "ChatOllama": (CATEGORY_LLM, "Ollama chat model"),
    "GenerativeModel": (CATEGORY_LLM, "Gemini GenerativeModel"),
    "Client": (CATEGORY_LLM, "Generic LLM client (verify provider)"),

    # AWS Bedrock (via the langchain-aws integration package -- boto3 alone
    # is too generic/noisy to flag on its own, since it's equally used for
    # S3/DynamoDB/etc. unrelated to LLMs).
    "ChatBedrockConverse": (CATEGORY_LLM, "AWS Bedrock chat model (LangChain, Converse API)"),
    "ChatBedrock": (CATEGORY_LLM, "AWS Bedrock chat model (LangChain)"),
    "BedrockChat": (CATEGORY_LLM, "AWS Bedrock chat model (LangChain, legacy)"),
    "BedrockEmbeddings": (CATEGORY_LLM, "AWS Bedrock embeddings (LangChain)"),
    "Bedrock": (CATEGORY_LLM, "AWS Bedrock LLM (LangChain, legacy)"),

    "FastMCP": (CATEGORY_MCP, "FastMCP server"),
    "Server": (CATEGORY_MCP, "MCP low-level server (verify import source)"),
    "ClientSession": (CATEGORY_MCP, "MCP client session"),

    "AgentExecutor": (CATEGORY_AGENT_FRAMEWORK, "LangChain AgentExecutor"),
    "StateGraph": (CATEGORY_AGENT_FRAMEWORK, "LangGraph StateGraph"),
    "Crew": (CATEGORY_AGENT_FRAMEWORK, "CrewAI Crew"),
    "Agent": (CATEGORY_AGENT_FRAMEWORK, "Agent (verify framework)"),
    "AssistantAgent": (CATEGORY_AGENT_FRAMEWORK, "AutoGen AssistantAgent"),
    "GroupChat": (CATEGORY_AGENT_FRAMEWORK, "AutoGen GroupChat"),
    "GroupChatManager": (CATEGORY_AGENT_FRAMEWORK, "AutoGen GroupChatManager"),
}

# ---------------------------------------------------------------------------
# 3) Known base classes for `class Foo(BaseX):` detection.
# ---------------------------------------------------------------------------
BASE_CLASS_REGISTRY = {
    "BaseTool": (CATEGORY_TOOL, "Custom Tool (subclasses BaseTool)"),
    "StructuredTool": (CATEGORY_TOOL, "Custom Structured Tool"),
    "Chain": (CATEGORY_AGENT_FRAMEWORK, "Custom LangChain Chain"),
    "Runnable": (CATEGORY_AGENT_FRAMEWORK, "Custom LangChain Runnable"),
    "AssistantAgent": (CATEGORY_AGENT_FRAMEWORK, "Custom AutoGen Agent subclass"),
}

# ---------------------------------------------------------------------------
# 4) Decorator names that register a tool / MCP capability.
#    "generic_tool_decorators" fire regardless of source (e.g. @tool).
#    "mcp_method_decorators" only fire when called as an attribute of a
#    symbol we've tracked as an MCP server instance, e.g. `@mcp.tool()`.
# ---------------------------------------------------------------------------
GENERIC_TOOL_DECORATORS = {"tool", "function_tool"}
MCP_METHOD_DECORATORS = {"tool", "resource", "prompt"}

# ---------------------------------------------------------------------------
# 5) Fallback: bare model-name string literals (low confidence; catches raw
#    HTTP usage, hard-coded model strings, config dicts, etc.)
# ---------------------------------------------------------------------------
MODEL_NAME_PATTERNS = [
    (r"^gpt-4", "OpenAI GPT-4 family (string literal)"),
    (r"^gpt-3\.5", "OpenAI GPT-3.5 family (string literal)"),
    (r"^o[13](-mini)?$", "OpenAI o-series (string literal)"),
    (r"^claude-", "Anthropic Claude family (string literal)"),
    (r"^gemini-", "Google Gemini family (string literal)"),
    (r"^llama-?[23]", "Meta Llama family (string literal)"),
    (r"^mistral-", "Mistral family (string literal)"),
    (r"^mixtral-", "Mistral Mixtral family (string literal)"),
    (r"^command-r", "Cohere Command-R family (string literal)"),
]

# Filenames recognized as MCP server/client configuration.
MCP_CONFIG_FILENAMES = {
    "mcp.json",
    ".mcp.json",
    "claude_desktop_config.json",
    "mcp_config.json",
}

# Env var name patterns treated as (weak) LLM-provider signals, tagged with
# the deployment target implied by the KEY NAME alone (never the value -- we
# only ever read the key). *_BASE_URL/*_API_BASE keys mean "an endpoint
# override exists" but we can't know what it points to from the name alone,
# so those are tagged UNKNOWN rather than assumed cloud or self-hosted.
ENV_KEY_PATTERNS = [
    (r"^OPENAI_API_KEY", "OpenAI", DEPLOYMENT_CLOUD),
    (r"^ANTHROPIC_API_KEY", "Anthropic", DEPLOYMENT_CLOUD),
    (r"^GOOGLE_API_KEY|^GEMINI_API_KEY", "Google Gemini", DEPLOYMENT_CLOUD),
    (r"^COHERE_API_KEY", "Cohere", DEPLOYMENT_CLOUD),
    (r"^MISTRAL_API_KEY", "Mistral AI", DEPLOYMENT_CLOUD),
    (r"^GROQ_API_KEY", "Groq", DEPLOYMENT_CLOUD),
    (r"^TOGETHER_API_KEY", "Together AI", DEPLOYMENT_CLOUD),
    (r"^HUGGINGFACE(HUB)?_API_(KEY|TOKEN)", "Hugging Face", DEPLOYMENT_CLOUD),
    (r"^PINECONE_API_KEY", "Pinecone", DEPLOYMENT_CLOUD),
    (r"^OPENAI_BASE_URL|^OPENAI_API_BASE", "OpenAI (custom endpoint configured)", DEPLOYMENT_UNKNOWN),
    (r"^AZURE_OPENAI_ENDPOINT", "Azure OpenAI (custom endpoint configured)", DEPLOYMENT_UNKNOWN),
    (r"^OLLAMA_HOST", "Ollama", DEPLOYMENT_SELF_HOSTED),
    (r"^VLLM_", "vLLM", DEPLOYMENT_SELF_HOSTED),
]

# Dependency-file package names we cross-reference against PACKAGE_REGISTRY
# (handles the case where a package is declared but not yet imported/used).
DEPENDENCY_FILES = {"requirements.txt", "pyproject.toml", "Pipfile"}

# ---------------------------------------------------------------------------
# 6) Deployment-target detection: packages/constructors that are inherently
#    self-hosted, keyword arguments on cloud SDK constructors that indicate
#    a custom endpoint, and patterns to classify a literal endpoint string.
# ---------------------------------------------------------------------------
SELF_HOSTED_PACKAGES = {"ollama", "vllm", "localai"}
SELF_HOSTED_CONSTRUCTORS = {"ChatOllama"}

# Constructor keyword arguments that override where an SDK's HTTP calls go.
ENDPOINT_OVERRIDE_KWARGS = {"base_url", "api_base", "endpoint", "azure_endpoint", "host"}

# A literal endpoint value matching any of these is confidently self-hosted.
LOCAL_HOST_PATTERNS = [
    r"localhost",
    r"127\.0\.0\.1",
    r"0\.0\.0\.0",
    r"^https?://10\.",
    r"^https?://172\.(1[6-9]|2\d|3[01])\.",
    r"^https?://192\.168\.",
    r"\.local(?:host)?(?::|/|$)",
]

# A literal endpoint value containing any of these is confidently cloud,
# even though a base_url/endpoint override was explicitly set (e.g. pointing
# at a specific Azure OpenAI resource or an alternate OpenAI-compatible
# region/mirror still hosted by the vendor).
KNOWN_CLOUD_DOMAINS = [
    "openai.com",
    "openai.azure.com",
    "azure.com",
    "anthropic.com",
    "googleapis.com",
    "amazonaws.com",
    "cloud.google.com",
]

# Call keyword arguments that typically carry prompt/message text (e.g.
# `messages=[...]`, `prompt="..."`, `system="..."`). Used to capture a
# free, static "prompt_hint" -- purely for context; never sent anywhere
# unless the optional --enrich LLM step is explicitly enabled.
PROMPT_BEARING_KWARGS = {"messages", "prompt", "system", "system_prompt", "input", "instructions"}

# ---------------------------------------------------------------------------
# 7) JS/TS package.json dependency names -> (category, display name).
#
# This engine is Python-only for real AST analysis (imports, instantiation,
# decorators, base classes) -- a JS/TS repo's actual agent/orchestration
# code is invisible to it. This registry closes part of that gap cheaply:
# declared npm dependencies are a MEDIUM-confidence signal (same as Python's
# requirements.txt/pyproject.toml handling), without needing a full JS/TS
# AST parser. Keyed on the exact package name as it appears in
# package.json's dependencies/devDependencies/peerDependencies.
# ---------------------------------------------------------------------------
JS_PACKAGE_REGISTRY = {
    # LLM providers / SDKs
    "openai": (CATEGORY_LLM, "OpenAI (JS/TS SDK)"),
    "@anthropic-ai/sdk": (CATEGORY_LLM, "Anthropic (JS/TS SDK)"),
    "@anthropic-ai/vertex-sdk": (CATEGORY_LLM, "Anthropic (Vertex, JS/TS SDK)"),
    "@google/generative-ai": (CATEGORY_LLM, "Google Gemini (JS/TS SDK)"),
    "@google/genai": (CATEGORY_LLM, "Google Gemini / Vertex AI (JS/TS SDK)"),
    "@azure/openai": (CATEGORY_LLM, "Azure OpenAI (JS/TS SDK)"),
    "cohere-ai": (CATEGORY_LLM, "Cohere (JS/TS SDK)"),
    "@mistralai/mistralai": (CATEGORY_LLM, "Mistral AI (JS/TS SDK)"),
    "groq-sdk": (CATEGORY_LLM, "Groq (JS/TS SDK)"),
    "replicate": (CATEGORY_LLM, "Replicate (JS/TS SDK)"),
    "together-ai": (CATEGORY_LLM, "Together AI (JS/TS SDK)"),
    "ollama": (CATEGORY_LLM, "Ollama (local, JS/TS client)"),

    # MCP
    "@modelcontextprotocol/sdk": (CATEGORY_MCP, "Model Context Protocol SDK (JS/TS)"),
    "@playwright/mcp": (CATEGORY_MCP, "Playwright MCP server"),

    # Agent / orchestration frameworks
    "langchain": (CATEGORY_AGENT_FRAMEWORK, "LangChain (JS/TS)"),
    "@langchain/core": (CATEGORY_AGENT_FRAMEWORK, "LangChain (JS/TS)"),
    "@langchain/community": (CATEGORY_AGENT_FRAMEWORK, "LangChain (JS/TS)"),
    "@langchain/openai": (CATEGORY_AGENT_FRAMEWORK, "LangChain (JS/TS)"),
    "@langchain/anthropic": (CATEGORY_AGENT_FRAMEWORK, "LangChain (JS/TS)"),
    "@langchain/google-genai": (CATEGORY_AGENT_FRAMEWORK, "LangChain (JS/TS)"),
    "@langchain/langgraph": (CATEGORY_AGENT_FRAMEWORK, "LangGraph (JS/TS)"),
    "llamaindex": (CATEGORY_AGENT_FRAMEWORK, "LlamaIndex (JS/TS)"),
    "ai": (CATEGORY_AGENT_FRAMEWORK, "Vercel AI SDK"),
    "autogen": (CATEGORY_AGENT_FRAMEWORK, "AutoGen (JS/TS)"),

    # Vector stores / memory
    "chromadb": (CATEGORY_VECTOR_STORE, "Chroma (JS/TS)"),
    "@pinecone-database/pinecone": (CATEGORY_VECTOR_STORE, "Pinecone (JS/TS)"),
    "weaviate-client": (CATEGORY_VECTOR_STORE, "Weaviate (JS/TS)"),
    "weaviate-ts-client": (CATEGORY_VECTOR_STORE, "Weaviate (JS/TS)"),
    "@qdrant/js-client-rest": (CATEGORY_VECTOR_STORE, "Qdrant (JS/TS)"),
}

# Scoped-org prefixes for packages not individually listed above (mirrors
# the Python side's `known + "_"` prefix fallback in scan_dependency_file).
# e.g. an unlisted `@langchain/xyz` package still gets attributed to LangChain.
JS_PACKAGE_PREFIX_FALLBACKS = [
    ("@langchain/", CATEGORY_AGENT_FRAMEWORK, "LangChain (JS/TS)"),
    ("@google/", CATEGORY_LLM, "Google Gemini / Vertex AI (JS/TS SDK)"),
    ("@anthropic-ai/", CATEGORY_LLM, "Anthropic (JS/TS SDK)"),
    ("@modelcontextprotocol/", CATEGORY_MCP, "Model Context Protocol SDK (JS/TS)"),
    ("@pinecone-database/", CATEGORY_VECTOR_STORE, "Pinecone (JS/TS)"),
    ("@qdrant/", CATEGORY_VECTOR_STORE, "Qdrant (JS/TS)"),
]

# JS packages that are inherently self-hosted (mirrors SELF_HOSTED_PACKAGES).
JS_SELF_HOSTED_PACKAGES = {"ollama"}

