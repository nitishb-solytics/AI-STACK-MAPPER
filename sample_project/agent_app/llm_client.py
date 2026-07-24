import os
from openai import OpenAI
from anthropic import Anthropic
from langchain.agents import AgentExecutor
from langchain.tools import tool

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
claude = Anthropic()

MODEL_NAME = "claude-opus-4-1"


@tool
def search_web(query: str) -> str:
    """Search the web for a query."""
    return f"results for {query}"


def run():
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
    )
    return resp
