import os
from typing import Optional

import yaml

from shared.exceptions import ConfigError


class ConfigLoader:
    @staticmethod
    def load_yaml(path: str) -> dict:
        try:
            with open(path, "r") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            raise ConfigError(f"Config file not found: {path}")
        except yaml.YAMLError as e:
            raise ConfigError(f"Failed to parse YAML file {path}: {e}")

    @staticmethod
    def merge_configs(base: dict, override: dict) -> dict:
        result = dict(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = ConfigLoader.merge_configs(result[key], value)
            else:
                result[key] = value
        return result

    @staticmethod
    def load_config(config_path: str, override_path: Optional[str] = None) -> dict:
        base = ConfigLoader.load_yaml(config_path)
        if override_path:
            override = ConfigLoader.load_yaml(override_path)
            return ConfigLoader.merge_configs(base, override)
        return base

    @staticmethod
    def load_all_from_dir(directory: str) -> list[dict]:
        configs = []
        if not os.path.isdir(directory):
            return configs
        for filename in sorted(os.listdir(directory)):
            if filename.endswith(".yaml") or filename.endswith(".yml"):
                path = os.path.join(directory, filename)
                configs.append(ConfigLoader.load_yaml(path))
        return configs
