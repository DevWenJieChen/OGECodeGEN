from __future__ import annotations

from pathlib import Path
import os
import yaml


def load_config(path: str = "config.yaml") -> dict:
    """
    Read a YAML configuration file and return a dict.
    Most non-sensitive settings can be written in config.yaml:
    - model name, temperature, top_k
    - collection names
    - module switches, etc.
    """
    text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}

