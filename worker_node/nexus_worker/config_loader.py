"""Local config loader for the standalone worker project."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when a worker config file is missing or invalid."""


class ConfigLoader:
    @staticmethod
    def load_yaml(path: str) -> dict[str, Any]:
        try:
            return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except FileNotFoundError as exc:
            raise ConfigError(f"Config file not found: {path}") from exc
        except yaml.YAMLError as exc:
            raise ConfigError(f"Failed to parse YAML file {path}: {exc}") from exc
