"""Full prompt text extraction. Run: python3 tests/test_prompt_extraction.py (or pytest)."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "extension", "python"))

from ai_stack_scanner.local_agents import MAX_PROMPT_CHARS, extract_prompt_texts  # noqa: E402
from ai_stack_scanner.scanner import scan_directory  # noqa: E402
from ai_stack_scanner.vault_publish import agent_content_hash, build_vault_payload  # noqa: E402

SOURCE = '''
import textwrap
from langchain_core.prompts import ChatPromptTemplate

TEMPLATE_DIR = "templates"
SYSTEM_PROMPT = """You are a careful model-risk reviewer.
Answer only from the documents provided."""
GRADER_PROMPT = textwrap.dedent("""
    Grade the answer from 1 to 5 against the rubric below.
""")
JUDGE_TEMPLATE = "Compare answer A with answer B and " + "pick the more faithful one."

def ask(client, question, context):
    user_prompt = f"Use this context to answer the question: {context}"
    client.messages.create(
        system="You summarise validation findings for a model owner.",
        messages=[{"role": "user", "content": "Summarise the findings in three short bullets."}],
    )
    return ChatPromptTemplate.from_messages([
        ("system", "You classify documents into the model inventory taxonomy."),
        ("human", "{question}"),
    ])
'''


def test_extracts_every_static_prompt():
    prompts = {p["name"]: p for p in extract_prompt_texts(SOURCE)}
    assert prompts["SYSTEM_PROMPT"]["text"].startswith("You are a careful model-risk reviewer.\nAnswer only")
    assert prompts["GRADER_PROMPT"]["text"].strip() == "Grade the answer from 1 to 5 against the rubric below."
    assert prompts["JUDGE_TEMPLATE"]["text"] == "Compare answer A with answer B and pick the more faithful one."
    assert prompts["user_prompt"]["text"] == "Use this context to answer the question: {context}"
    assert prompts["create(system=)"]["text"] == "You summarise validation findings for a model owner."
    assert prompts["create(messages content)"]["text"] == "Summarise the findings in three short bullets."
    assert prompts["from_messages(system)"]["text"] == "You classify documents into the model inventory taxonomy."
    assert "TEMPLATE_DIR" not in prompts  # prompt-like name, but not prompt text
    assert "from_messages(human)" not in prompts  # "{question}" alone is too short to be a prompt
    assert all(p["line"] > 0 and p["truncated"] is False for p in prompts.values())


def test_truncates_and_tolerates_bad_source():
    long_prompt = extract_prompt_texts(f'BIG_PROMPT = "{"word " * 10_000}"')[0]
    assert len(long_prompt["text"]) == MAX_PROMPT_CHARS and long_prompt["truncated"] is True
    assert extract_prompt_texts("def broken(:\n") == []


def test_scan_and_payload_carry_prompts():
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "services", "agents", "reviewer"))
        with open(os.path.join(root, "services", "agents", "reviewer", "prompts.py"), "w") as f:
            f.write(SOURCE)
        stack = scan_directory(root).to_dict()

    assert {p["file"] for p in stack["prompts"]} == {os.path.join("services", "agents", "reviewer", "prompts.py")}
    assert {p["component"] for p in stack["prompts"]} == {"Reviewer Prompt"}
    # Prompt text never rides on occurrences: Vault copies those into a size-capped attribute.
    for components in stack["categories"].values():
        for component in components:
            assert all("text" not in o for o in component["occurrences"])

    payload = build_vault_payload(root, stack, repo_url="https://github.com/org/repo.git")
    assert payload["stack"]["prompts"] == stack["prompts"]


def test_hash_unchanged_without_prompts_and_tracks_prompt_edits():
    agent = {"files": ["a.py"], "components": {"prompts": ["Reviewer Prompt"]}, "evidence": []}
    assert agent_content_hash(agent) == agent_content_hash(agent, [])
    v1 = agent_content_hash(agent, [{"text": "You are a reviewer."}])
    v2 = agent_content_hash(agent, [{"text": "You are a strict reviewer."}])
    assert v1 != v2 != agent_content_hash(agent)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
