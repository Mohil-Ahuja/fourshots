"""Configuration, loaded from the environment with a .env fallback.

Deliberately dependency-free: a ten-line parser is easier to audit than a
library, and this file handles secrets.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path = ".env") -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Real environment variables always win, so a deployment can override the
    file without editing it. Blank lines and `#` comments are skipped.
    """
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def require(name: str) -> str:
    """Fetch a required setting, failing loudly and early if it is missing.

    Better to refuse to start than to run with an empty webhook secret, which
    would turn signature verification into a formality.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


def optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()
