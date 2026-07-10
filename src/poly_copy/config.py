"""Config loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PACKAGE_ROOT / "configs" / "default.yaml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    with cfg_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config must be a mapping: {cfg_path}")
    return data


def resolve_cache_dir(cfg: dict[str, Any], override: str | Path | None = None) -> Path:
    if override:
        return Path(override)
    rel = cfg.get("data", {}).get("cache_dir", "cache")
    path = Path(rel)
    if not path.is_absolute():
        path = PACKAGE_ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path
