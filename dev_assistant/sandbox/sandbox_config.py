# Quick note: one-line comment added as requested.
"""Sandbox configuration with environment variable overrides."""

from __future__ import annotations

import os
from typing import Any


def _to_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _to_int(value: str, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


SANDBOX_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "mode": "auto",
    "timeout_seconds": 30,
    "memory_limit": "256m",
    "cpu_limit": 0.5,
    "network_disabled": True,
    "allowed_languages": ["python", "javascript", "typescript", "shell"],
}


def _load_env_overrides() -> None:
    mode = os.getenv("SANDBOX_MODE")
    timeout = os.getenv("SANDBOX_TIMEOUT")
    memory = os.getenv("SANDBOX_MEMORY")
    network = os.getenv("SANDBOX_NETWORK")

    if mode:
        normalized_mode = mode.strip().lower()
        if normalized_mode in {"docker", "subprocess", "auto"}:
            SANDBOX_SETTINGS["mode"] = normalized_mode

    SANDBOX_SETTINGS["timeout_seconds"] = _to_int(timeout, SANDBOX_SETTINGS["timeout_seconds"])

    if memory:
        SANDBOX_SETTINGS["memory_limit"] = memory.strip()

    if network is not None:
        allow_network = _to_bool(network, default=False)
        SANDBOX_SETTINGS["network_disabled"] = not allow_network


_load_env_overrides()
