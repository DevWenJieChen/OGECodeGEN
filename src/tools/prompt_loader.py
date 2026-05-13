from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path.cwd() / "prompts"

def load(rel_path: str) -> str:
    """
    Read the raw text of a prompt file.
    rel_path example："intent/system.md"
    """
    p = PROMPTS_DIR / rel_path
    return p.read_text(encoding="utf-8")


def render(rel_path: str, **kwargs) -> str:
    """
    The lightest dynamic prompt approach: str.format(**kwargs)
    - Write placeholders such as {user_query} and {intent_json} in the .md file
    - Insert retrieval results, constraints, etc. at runtime
'
    Note:
    - If you need literal { } in a prompt, write {{ }} to escape them
    """
    template = load(rel_path)
    return template.format(**kwargs)
