"""Configuration loading for the lightweight Termux agent."""

from __future__ import annotations

import os
import math
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class Config:
    base_url: str = "http://192.168.86.244:8085/v1"
    model: str = "Qwen3.8-27B-MTPLX-Speed"
    max_steps: int = 16
    max_output_chars: int = 20_000
    approval_policy: str = "on-risk"
    timeout: float = 120.0
    api_key: str = ""
    api_key_env: str = "TERMUX_AGENT_API_KEY"
    api_profile: str = "default"
    user_agent: str = "termux-agent/0.1"
    stream: bool = True

    @classmethod
    def load(cls, path: str | Path | None = None, profile: str | None = None) -> "Config":
        """Load supported values from TOML, then apply the sole key source: env."""
        config = cls()
        config_path = Path(path) if path is not None else Path.home() / ".config/termux-agent/config.toml"
        try:
            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"cannot read config {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("config must be a TOML table")
        valid = {"base_url", "model", "max_steps", "max_output_chars", "approval_policy", "timeout", "user_agent", "stream"}
        for key in valid & raw.keys():
            setattr(config, key, raw[key])
        profiles = raw.get("apis", {})
        if not isinstance(profiles, dict):
            raise ValueError("apis must be a TOML table")
        selected = profile or raw.get("default_api", "default")
        if not isinstance(selected, str):
            raise ValueError("default_api must be a profile name")
        if selected != "default":
            entry = profiles.get(selected)
            if not isinstance(entry, dict):
                raise ValueError(f"unknown API profile: {selected}")
            for required in ("base_url", "model"):
                if required not in entry:
                    raise ValueError(f"API profile requires {required}")
            for key in ("base_url", "model", "timeout", "user_agent", "stream"):
                if key in entry:
                    setattr(config, key, entry[key])
            config.api_key_env = entry.get("api_key_env", "")
        else:
            config.api_key_env = raw.get("api_key_env", "TERMUX_AGENT_API_KEY")
        config.api_profile = selected
        if not isinstance(config.api_key_env, str):
            raise ValueError("api_key_env must be an environment variable name")
        config.api_key = os.environ.get(config.api_key_env, "") if config.api_key_env else ""
        config.validate()
        return config

    @staticmethod
    def profiles(path=None):
        source = Path(path) if path else Path.home() / ".config/termux-agent/config.toml"
        raw = tomllib.loads(source.read_text()) if source.exists() else {}
        profiles = raw.get("apis", {})
        if not isinstance(profiles, dict):
            raise ValueError("apis must be a TOML table")
        return ["default", *profiles.keys()]

    def validate(self) -> None:
        if not isinstance(self.stream, bool):
            raise ValueError("stream must be a boolean")
        if not isinstance(self.user_agent, str) or not self.user_agent.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in self.user_agent):
            raise ValueError("user_agent must be a single-line ASCII string")
        if not isinstance(self.base_url, str) or not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(self.max_steps, int) or isinstance(self.max_steps, bool) or self.max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        if not isinstance(self.max_output_chars, int) or isinstance(self.max_output_chars, bool) or not 128 <= self.max_output_chars <= 20_000:
            raise ValueError("max_output_chars must be between 128 and 20000")
        if self.approval_policy not in {"always", "on-risk", "never"}:
            raise ValueError("approval_policy must be always, on-risk, or never")
        if not isinstance(self.timeout, (int, float)) or isinstance(self.timeout, bool) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 3600:
            raise ValueError("timeout must be a positive number no greater than 3600 seconds")

    def public_dict(self) -> dict[str, Any]:
        """Safe-to-display config; credentials are intentionally omitted."""
        data = asdict(self)
        data.pop("api_key", None)
        return data


def load_config(path: str | Path | None = None) -> Config:
    return Config.load(path)
