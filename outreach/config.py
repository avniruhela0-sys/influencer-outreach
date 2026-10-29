"""Configuration loading (config.yaml + .env)."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

try:  # optional
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | os.PathLike | None = None) -> dict:
    if load_dotenv:
        load_dotenv(ROOT / ".env")
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg.setdefault("db_path", str(ROOT / "data" / "outreach.db"))
    cfg.setdefault("export_dir", str(ROOT / "data" / "exports"))
    return cfg


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(f"Missing required environment variable {name}. See .env.example")
    return val
