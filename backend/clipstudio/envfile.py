"""Minimal ``.env`` loader.

ClipStudio keeps its API keys (``NVIDIA_API_KEY``, ``NVIDIA_MODEL``,
``GROQ_API_KEY``) in a ``.env`` file in the project root. Values are copied into
``os.environ`` without overriding anything already set, and are never logged.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE_VAR = "CLIPSTUDIO_ENV_FILE"

#: Project root: backend/clipstudio/envfile.py -> parents[2].
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def candidate_files() -> list[Path]:
    explicit = os.environ.get(ENV_FILE_VAR)
    files = [Path(explicit)] if explicit else []
    files += [Path.cwd() / ".env", PROJECT_ROOT / ".env"]
    return files


def parse(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines. Supports comments, ``export`` and quotes."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] in "\"'" and (end := value.find(value[0], 1)) > 0:
            # Quoted value; anything after the closing quote (a comment) is dropped.
            value = value[1:end]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            values[key] = value
    return values


def load_env() -> list[Path]:
    """Load the first readable ``.env`` candidates; returns the files used."""
    used: list[Path] = []
    if os.environ.get("CLIPSTUDIO_NO_ENV") == "1":  # tests
        return used
    for path in candidate_files():
        try:
            if not path.is_file():
                continue
            parsed = parse(path.read_text(encoding="utf-8-sig"))
        except OSError:
            continue
        for key, value in parsed.items():
            os.environ.setdefault(key, value)
        used.append(path)
    return used
