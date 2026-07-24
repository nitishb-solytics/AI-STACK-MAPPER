"""
Extra sample AI-stack usage to exercise signals not covered by the other
sample files: real (not just declared) CrewAI + Chroma usage, a custom
Tool subclass, LangGraph orchestration, and a self-hosted vLLM import.
"""
from crewai import Agent, Crew
from langchain.tools import BaseTool
import chromadb
from langgraph.graph import StateGraph
from vllm import LLM


class WeatherTool(BaseTool):
    name: str = "weather_lookup"
    description: str = "Look up current weather for a city."

    def _run(self, city: str) -> str:
        return f"sunny in {city}"


researcher = Agent(
    role="Researcher",
    goal="Find relevant information",
    backstory="An experienced research analyst.",
)

crew = Crew(agents=[researcher], tasks=[])

memory_client = chromadb.Client()
collection = memory_client.get_or_create_collection("agent-memory")

graph = StateGraph(dict)

# Self-hosted inference server -- no endpoint override needed, vLLM is
# inherently local/on-prem.
local_llm = LLM(model="meta-llama/Llama-3-70b")


def run():
    crew.kickoff()
