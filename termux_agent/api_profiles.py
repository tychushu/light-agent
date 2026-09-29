"""Small, credential-free TOML files for additional API profiles."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import time
import tomllib
from urllib.parse import urlsplit

NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProfileStore:
    def __init__(self, config_path=None):
        self.path = Path(config_path) if config_path else Path.home() / ".config/termux-agent/config.toml"
        self.folder = self.path.parent / "profiles"
        self.default_file = self.path.parent / "selected-api"

    @staticmethod
    def check_name(name):
        if not isinstance(name, str) or not NAME.fullmatch(name) or name == "default":
            raise ValueError("Profile name must use letters, numbers, '.', '_' or '-' and start with a letter")

    @staticmethod
    def kind_for(url):
        host = (urlsplit(url).hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
            return "local"
        try:
            if ipaddress.ip_address(host).is_private:
                return "local"
        except ValueError:
            pass
        return "cloud"

    def _raw(self):
        if not self.path.exists():
            return {}
        try:
            return tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"cannot read config {self.path}: {exc}") from exc

    def entries(self, raw=None):
        raw = self._raw() if raw is None else raw
        original = raw.get("apis", {})
        if not isinstance(original, dict):
            raise ValueError("apis must be a TOML table")
        merged = dict(original)
        if self.folder.is_dir():
            for file in sorted(self.folder.glob("*.toml")):
                if file.is_symlink():
                    continue
                name = file.stem
                self.check_name(name)
                if name in merged:
                    raise ValueError(f"duplicate API profile: {name}")
                try:
                    value = tomllib.loads(file.read_text(encoding="utf-8"))
                except (OSError, tomllib.TOMLDecodeError) as exc:
                    raise ValueError(f"cannot read API profile {name}: {exc}") from exc
                if not isinstance(value, dict):
                    raise ValueError(f"invalid API profile: {name}")
                merged[name] = value
        return merged

    def get_default(self, raw=None, entries=None):
        raw = self._raw() if raw is None else raw
        entries = self.entries(raw) if entries is None else entries
        if self.default_file.exists():
            name = self.default_file.read_text(encoding="utf-8").strip()
            if name == "default" or name in entries:
                return name
        return raw.get("default_api", "default")

    def list(self):
        raw = self._raw()
        profiles = self.entries(raw)
        default = self.get_default(raw, profiles)
        rows = []
        def sort_key(name):
            entry = profiles[name]
            kind = entry.get("kind", self.kind_for(entry.get("base_url", "")))
            return kind != "local", name
        for name in sorted(profiles, key=sort_key):
            entry = profiles[name]
            kind = entry.get("kind", self.kind_for(entry.get("base_url", "")))
            env = entry.get("api_key_env", "")
            rows.append({"name": name, "kind": kind, "model": entry.get("model", ""),
                         "base_url": entry.get("base_url", ""), "api_key_env": env,
                         "key_ready": bool(os.environ.get(env)) if env else True,
                         "default": name == default})
        return rows

    def add(self, name, base_url, model, api_key_env="", kind="auto",
            stream=True, timeout=None, user_agent=None):
        self.check_name(name)
        if name in self.entries():
            raise ValueError(f"API profile already exists: {name}")
        parts = urlsplit(base_url)
        if (parts.scheme not in ("http", "https") or not parts.netloc or
                parts.username or parts.password or parts.query or parts.fragment or
                any(ord(c) < 33 for c in base_url)):
            raise ValueError("base_url must be an http(s) URL without credentials, query or fragment")
        if (not isinstance(model, str) or not model.strip() or
                any(ord(c) < 32 or ord(c) == 127 for c in model)):
            raise ValueError("model must be a non-empty single line")
        if api_key_env and not ENV_NAME.fullmatch(api_key_env):
            raise ValueError("api_key_env must be an environment variable name; never enter the Key itself")
        if kind == "auto":
            kind = self.kind_for(base_url)
        if kind not in ("local", "cloud") or not isinstance(stream, bool):
            raise ValueError("kind must be local/cloud and stream must be boolean")
        if timeout is not None and (not isinstance(timeout, (int, float)) or
                                    isinstance(timeout, bool) or not 0 < timeout <= 3600):
            raise ValueError("timeout must be between 0 and 3600 seconds")
        if user_agent is not None and (not isinstance(user_agent, str) or
                                       not user_agent.isascii() or
                                       any(ord(c) < 32 or ord(c) == 127 for c in user_agent)):
            raise ValueError("user_agent must be single-line ASCII text")
        fields = {"base_url": base_url, "model": model, "api_key_env": api_key_env,
                  "kind": kind, "stream": stream}
        if timeout is not None:
            fields["timeout"] = timeout
        if user_agent is not None:
            fields["user_agent"] = user_agent
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.folder.chmod(0o700)
        path = self.folder / f"{name}.toml"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                for key, value in fields.items():
                    encoded = str(value).lower() if isinstance(value, bool) else json.dumps(value, ensure_ascii=False)
                    file.write(f"{key} = {encoded}\n")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def set_default(self, name):
        if name != "default" and name not in self.entries():
            raise ValueError(f"unknown API profile: {name}")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = self.default_file.with_name(self.default_file.name + f".{os.getpid()}.tmp")
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(name + "\n")
            os.replace(temp, self.default_file)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def remove(self, name):
        self.check_name(name)
        path = self.folder / f"{name}.toml"
        if not path.is_file() or path.is_symlink():
            raise ValueError("Only profiles added under profiles/ can be removed")
        backup = self.folder / f".{name}.{time.time_ns()}.removed"
        path.replace(backup)
        if self.default_file.exists() and self.default_file.read_text().strip() == name:
            self.default_file.unlink()
        return backup
