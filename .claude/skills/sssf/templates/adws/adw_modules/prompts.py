"""Prompt rendering: load system/user refs from config, replace {{placeholders}}."""

from __future__ import annotations

import hashlib
from pathlib import Path


def render(template_path: str | Path, variables: dict[str, str]) -> str:
    text = Path(template_path).read_text(encoding="utf-8-sig")
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def fingerprint(*template_paths: str | Path) -> str:
    """A short hash of an agent's prompt templates: its prompt VERSION.

    Hashed before rendering, so it changes when someone edits system.md or
    user.md and never because the request did. Recorded on every agent_start,
    it is what lets a change in an agent's scores be traced to a prompt edit.
    Line endings are normalized: a CRLF checkout is the same prompt.
    """
    digest = hashlib.sha256()
    for path in template_paths:
        text = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        digest.update(text.encode("utf-8") + b"\0")
    return digest.hexdigest()[:12]


def save(directory: str | Path, name: str, content: str) -> Path:
    """Save the exact prompt sent, before execution — the audit copy."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path
