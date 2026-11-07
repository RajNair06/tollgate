"""Versioned prompt files.

Prompts live as plain markdown under agent/prompts/<version>/<name>.md —
never as string literals buried in code. Versioning rule: a prompt change
that could alter behavior gets a NEW version directory (v2, v3, ...); old
runs stay reproducible by pinning AGENT_PROMPT_VERSION.

The loader fails loudly on a missing prompt: a silently empty system prompt
would produce confidently wrong agent behavior, which is the failure mode
this whole project exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str, version: str = "v1") -> str:
    """Read agent/prompts/<version>/<name>.md, verbatim."""
    path = PROMPTS_DIR / version / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"prompt {name!r} not found for version {version!r} "
            f"(looked at {path})"
        )
    return path.read_text(encoding="utf-8")


def available_versions() -> list[str]:
    return sorted(p.name for p in PROMPTS_DIR.iterdir() if p.is_dir())
