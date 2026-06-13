"""
Prompt Versioning System

System prompts are stored as plain .txt files in app/prompts/.
Naming convention:  {agent_name}_v{N}.txt

Examples:
    app/prompts/code_writer_v1.txt
    app/prompts/code_writer_v2.txt   ← newer version
    app/prompts/task_parser_v1.txt

How to use:
    from app.prompts import load_prompt

    prompt = load_prompt("code_writer")   # loads highest version
    prompt = load_prompt("code_writer", version=1)  # loads specific version

How to create a new version:
    1. Copy code_writer_v1.txt → code_writer_v2.txt
    2. Edit the new file
    3. Set PROMPT_VERSION_code_writer=2 in .env to activate
       (or it auto-selects the highest version)

Environment overrides:
    PROMPT_VERSION_code_writer=2
    PROMPT_VERSION_task_parser=1
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_PROMPT_DIR = Path(__file__).parent


def list_versions(agent_name: str) -> list[int]:
    """Return all available version numbers for an agent prompt, sorted ascending."""
    pattern = re.compile(rf"^{re.escape(agent_name)}_v(\d+)\.txt$")
    versions = []
    for f in _PROMPT_DIR.glob(f"{agent_name}_v*.txt"):
        m = pattern.match(f.name)
        if m:
            versions.append(int(m.group(1)))
    return sorted(versions)


def load_prompt(agent_name: str, version: int | None = None) -> str:
    """
    Load a system prompt for the given agent.

    Args:
        agent_name: e.g. "code_writer", "task_parser"
        version:    specific version number, or None to auto-select

    Auto-selection order:
        1. PROMPT_VERSION_{agent_name} env var
        2. Highest available version number

    Returns:
        Prompt string (stripped).

    Raises:
        FileNotFoundError if no prompt file found.
    """
    if version is None:
        env_key = f"PROMPT_VERSION_{agent_name}"
        env_val = os.getenv(env_key)
        if env_val and env_val.isdigit():
            version = int(env_val)
        else:
            versions = list_versions(agent_name)
            if not versions:
                raise FileNotFoundError(
                    f"No prompt file found for agent '{agent_name}' in {_PROMPT_DIR}. "
                    f"Expected: {agent_name}_v1.txt"
                )
            version = versions[-1]  # highest version

    path = _PROMPT_DIR / f"{agent_name}_v{version}.txt"
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt file not found: {path}. "
            f"Available versions: {list_versions(agent_name)}"
        )
    return path.read_text(encoding="utf-8").strip()


def get_active_version(agent_name: str) -> int:
    """Return the version number that would be loaded by load_prompt()."""
    env_key = f"PROMPT_VERSION_{agent_name}"
    env_val = os.getenv(env_key)
    if env_val and env_val.isdigit():
        return int(env_val)
    versions = list_versions(agent_name)
    return versions[-1] if versions else 0