from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_platforms_config(path: Path) -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("platforms.yaml must contain a mapping")
    return raw
