"""
Demonstrates every deployment-target classification the scanner can make:
cloud, self-hosted/on-prem, and unknown (unresolvable override). This file
exists purely so a scan of `sample_project` shows all three styles in one
report -- it isn't meant to be run.
"""
import os

from openai import OpenAI
from langchain_community.chat_models import ChatOllama

# 1) Cloud -- default vendor-hosted endpoint, no override.
cloud_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# 2) Self-hosted / on-prem -- base_url resolves to a private IP, so the
#    scanner can tell this literally never leaves the local network.
onprem_client = OpenAI(
    api_key="not-needed",
    base_url="http://10.0.5.12:8000/v1",
)

# 3) Unknown -- base_url is set from an env var, so the destination can't be
#    resolved by static analysis. Flagged for manual review rather than
#    silently assumed to be cloud.
gateway_client = OpenAI(
    api_key=os.environ["INTERNAL_GATEWAY_KEY"],
    base_url=os.environ["INTERNAL_LLM_GATEWAY_URL"],
)

# 4) Self-hosted / on-prem -- Ollama is inherently a local inference server,
#    no endpoint override needed to classify it.
local_model = ChatOllama(model="llama3")


def run_all():
    cloud_client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    onprem_client.chat.completions.create(model="local-llama-3-70b", messages=[{"role": "user", "content": "hi"}])
    gateway_client.chat.completions.create(model="internal-model", messages=[{"role": "user", "content": "hi"}])
    local_model.invoke("hi")
