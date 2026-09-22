"""Configuration loading. Everything tunable lives in config.yaml at the repo root."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"


class Config(dict):
    """dict with attribute access and path helpers."""

    def __getattr__(self, item: str) -> Any:
        try:
            v = self[item]
        except KeyError as e:
            raise AttributeError(item) from e
        return Config(v) if isinstance(v, dict) else v

    def path(self, key: str) -> Path:
        p = Path(self["paths"][key])
        return p if p.is_absolute() else ROOT / p


@lru_cache(maxsize=1)
def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = Config(raw)
    # env overrides
    if os.environ.get("MEALPLAN_MOCK", "").strip() in {"1", "true", "yes"}:
        cfg["llm"]["mock"] = True
    for k in ("uploads", "cache", "outputs"):
        cfg.path(k).mkdir(parents=True, exist_ok=True)
    return cfg


def reload_config() -> Config:
    load_config.cache_clear()
    return load_config()
