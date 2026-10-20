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
    max_input_file_chars: int = 64_000
    max_context_chars: int = 500_000
    approval_policy: str = "on-risk"
    timeout: float = 120.0
    retry_thinking_400: bool = False
    retry_wait_budget: float = 20.0
    retry_max_attempts: int = 3
    max_subagents: int = 3
    subagent_max_steps: int = 6
    web_fetch_enabled: bool = False
    lightpanda_binary: str = ""
    api_key: str = ""
    api_key_env: str = "TERMUX_AGENT_API_KEY"
    api_profile: str = "default"
    api_kind: str = "local"
    user_agent: str = "termux-agent/0.1"
    stream: bool = True
    silent_writes: bool = True
    auto_continue: bool = False
    max_continuations: int = 3

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
        valid = {"base_url", "model", "max_steps", "max_output_chars", "max_input_file_chars", "max_context_chars", "approval_policy", "timeout", "retry_thinking_400", "retry_wait_budget", "retry_max_attempts", "max_subagents", "subagent_max_steps", "web_fetch_enabled", "lightpanda_binary", "user_agent", "stream", "silent_writes", "auto_continue", "max_continuations"}
        for key in valid & raw.keys():
            setattr(config, key, raw[key])
        from .api_profiles import ProfileStore
        store = ProfileStore(config_path)
        profiles = store.entries(raw)
        selected = profile if profile is not None else store.get_default(raw, profiles)
        if not isinstance(selected, str):
            raise ValueError("default_api must be a profile name")
        if selected != "default":
            entry = profiles.get(selected)
            if not isinstance(entry, dict):
                raise ValueError(f"unknown API profile: {selected}")
            for required in ("base_url", "model"):
                if required not in entry:
                    raise ValueError(f"API profile requires {required}")
            for key in ("base_url", "model", "timeout", "retry_wait_budget", "retry_max_attempts", "user_agent", "stream", "max_context_chars"):
                if key in entry:
                    setattr(config, key, entry[key])
            # Retrying the known thinking-format error is opt-in per API profile.
            config.retry_thinking_400 = entry.get("retry_thinking_400", False)
            config.api_key_env = entry.get("api_key_env", "")
            config.api_kind = entry.get("kind", store.kind_for(config.base_url))
        else:
            config.api_key_env = raw.get("api_key_env", "TERMUX_AGENT_API_KEY")
            config.api_kind = store.kind_for(config.base_url)
        config.api_profile = selected
        if not isinstance(config.api_key_env, str):
            raise ValueError("api_key_env must be an environment variable name")
        config.api_key = os.environ.get(config.api_key_env, "") if config.api_key_env else ""
        config.validate()
        return config

    @staticmethod
    def profiles(path=None):
        from .api_profiles import ProfileStore
        return ["default", *ProfileStore(path).entries().keys()]

    def validate(self) -> None:
        if (isinstance(self.max_context_chars, bool) or not isinstance(self.max_context_chars, int)
                or not 1000 <= self.max_context_chars <= 20_000_000):
            raise ValueError('max_context_chars must be between 1000 and 20000000')
        if not isinstance(self.web_fetch_enabled, bool):
            raise ValueError("web_fetch_enabled must be a boolean")
        if (not isinstance(self.lightpanda_binary, str) or
                any(ord(c) < 32 or ord(c) == 127 for c in self.lightpanda_binary)):
            raise ValueError("lightpanda_binary must be an executable name or path without control characters")
        for key, low, high in (("retry_max_attempts", 1, 6), ("max_subagents", 0, 8),
                               ("subagent_max_steps", 1, 12)):
            value = getattr(self, key)
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}")
        if not isinstance(self.auto_continue, bool):
            raise ValueError("auto_continue must be a boolean")
        if (not isinstance(self.max_continuations, int) or isinstance(self.max_continuations, bool)
                or not 1 <= self.max_continuations <= 8):
            raise ValueError("max_continuations must be between 1 and 8")
        if not isinstance(self.silent_writes, bool):
            raise ValueError("silent_writes must be a boolean")
        if (not isinstance(self.max_input_file_chars, int) or
                isinstance(self.max_input_file_chars, bool) or not 1 <= self.max_input_file_chars <= 1_000_000):
            raise ValueError("max_input_file_chars must be between 1 and 1000000")
        if self.api_kind not in ("local", "cloud"):
            raise ValueError("api_kind must be local or cloud")
        if not isinstance(self.stream, bool):
            raise ValueError("stream must be a boolean")
        if not isinstance(self.retry_thinking_400, bool):
            raise ValueError("retry_thinking_400 must be a boolean")
        retry_budget_valid = isinstance(self.retry_wait_budget, (int, float)) and not isinstance(self.retry_wait_budget, bool)
        if retry_budget_valid:
            try:
                retry_budget_valid = math.isfinite(self.retry_wait_budget) and 0 <= self.retry_wait_budget <= 60
            except OverflowError:
                retry_budget_valid = False
        if not retry_budget_valid:
            raise ValueError("retry_wait_budget must be between 0 and 60 seconds")
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
