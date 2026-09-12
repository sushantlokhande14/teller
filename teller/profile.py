"""
Loading app profiles, with optional tenant overlays.

A profile describes one vendor product. A tenant overlay is a small YAML file
that changes only what differs for that institution (base_url, auth details,
extra conditions), so a capability recorded against the base product can run
for that tenant without being re-recorded.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from teller.schema import AppProfile

PROFILES_DIR = Path(__file__).parent / "profiles"


def _read(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def apply_overlay(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if key == "conditions":
            # Overlay conditions go first so they win over the base entries with the same id.
            ids = {c["id"] for c in value}
            out["conditions"] = list(value) + [c for c in base.get("conditions", []) if c["id"] not in ids]
        elif key == "auth" and isinstance(value, dict) and isinstance(base.get("auth"), dict):
            out["auth"] = {**base["auth"], **value}
        else:
            out[key] = value
    return out


def load_profile(name: str, overlay: str | None = None) -> AppProfile:
    path = Path(name) if name.endswith((".yaml", ".yml")) else PROFILES_DIR / f"{name}.yaml"
    data = _read(path)
    over: dict[str, Any] = {}
    if overlay:
        over = _read(Path(overlay))
        data = apply_overlay(data, over)
    # The environment can point the base profile at a different instance, but it must
    # not silently outrank a tenant overlay that names its own instance.
    env_url = os.environ.get(f"{data['id'].upper()}_URL")
    if env_url and "base_url" not in over:
        data["base_url"] = env_url
    return AppProfile.model_validate(data)
