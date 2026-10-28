"""Lazy token estimates and optional llama.cpp metadata/tokenization."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any

import httpx


_WORD = re.compile(r"[A-Za-z0-9_]+|[^\s]", re.UNICODE)


def estimate_text_tokens(text: str) -> int:
    """Conservative, labeled heuristic; it never equates characters with tokens."""
    if not isinstance(text, str) or not text:
        return 0
    total = 0
    for token in _WORD.findall(text):
        if token.isascii() and all(char.isalnum() or char == "_" for char in token):
            total += max(1, math.ceil(len(token) / 4))
        elif any("\u3400" <= char <= "\u9fff" for char in token):
            total += sum(1 if "\u3400" <= char <= "\u9fff" else 2 for char in token)
        else:
            total += 2
    return math.ceil(total * 1.25) + 2


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class TokenCounter:
    """No cold-start network: local endpoints are touched only on a measured request."""

    def __init__(self, config: Any, http_client: httpx.Client | None = None,
                 cache_path: str | Path | None = None, clock=time.time):
        self.config = config
        self._http = http_client
        self._owns_http = http_client is None
        self.clock = clock
        self.cache_path = Path(cache_path) if cache_path else Path.home() / ".cache/termux-agent/token-metadata.json"
        self._memory: dict[str, Any] = {}

    @property
    def _key(self) -> str:
        raw = "\0".join((self.config.base_url.rstrip("/"), self.config.model, self.config.wire_api,
                         getattr(self.config, "token_metadata_base_url", "")))
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def effective_output_reserve_tokens(self) -> int:
        """Reserve input headroom for configured output or an explicit generation cap."""
        return max(int(self.config.output_reserve_tokens or 0),
                   int(getattr(self.config, "max_output_tokens", 0) or 0))

    def close(self):
        if self._owns_http and self._http is not None:
            self._http.close()

    def _cache_read(self):
        key = self._key
        if key in self._memory:
            value = self._memory[key]
            if isinstance(value, dict) and value.get("updated", 0) + self.config.token_cache_ttl > self.clock():
                return value
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            value = data.get("entries", {}).get(key, {})
            if isinstance(value, dict) and value.get("updated", 0) + self.config.token_cache_ttl > self.clock():
                self._memory[key] = value
                return value
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return {}

    def _cache_write(self, value):
        value = dict(value, updated=self.clock())
        self._memory[self._key] = value
        try:
            self.cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            data = {"version": 1, "entries": {}}
            if self.cache_path.exists():
                try:
                    loaded = json.loads(self.cache_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                        data["entries"] = loaded["entries"]
                except (OSError, ValueError):
                    pass
            data["entries"][self._key] = value
            temp = self.cache_path.with_name(self.cache_path.name + f".{os.getpid()}.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temp.chmod(0o600)
            os.replace(temp, self.cache_path)
        except OSError:
            pass

    def invalidate(self):
        self._memory.pop(self._key, None)
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            data.get("entries", {}).pop(self._key, None)
            self.cache_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            self.cache_path.chmod(0o600)
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _client(self):
        if self._http is None:
            self._http = httpx.Client(timeout=min(float(self.config.timeout), 10.0))
        return self._http

    def _native_root(self):
        explicit = getattr(self.config, "token_metadata_base_url", "")
        if explicit:
            return explicit.rstrip("/")
        root = self.config.base_url.rstrip("/")
        return root[:-3] if root.endswith("/v1") else root

    def _request(self, method, path, **kwargs):
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        response = self._client().request(method, self._native_root() + path,
                                          headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def _local_metadata(self):
        cached = self._cache_read()
        if cached.get("metadata_checked"):
            return cached
        window, error = 0, ""
        try:
            props = self._request("GET", "/props")
            if not isinstance(props, dict):
                raise ValueError("local metadata must be an object")
            candidates = [props.get("n_ctx"), props.get("context_size"),
                          (props.get("default_generation_settings") or {}).get("n_ctx")
                          if isinstance(props.get("default_generation_settings"), dict) else None]
            for candidate in candidates:
                if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
                    window = candidate
                    break
            if not window:
                slots = self._request("GET", "/slots")
                values = slots if isinstance(slots, list) else slots.get("slots", [])
                for slot in values if isinstance(values, list) else []:
                    candidate = slot.get("n_ctx") if isinstance(slot, dict) else None
                    if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
                        window = candidate
                        break
        except (httpx.HTTPError, ValueError, TypeError, OSError) as exc:
            error = type(exc).__name__
        value = {"metadata_checked": True, "metadata_source": "llama_cpp" if window else "unavailable",
                 "context_window_tokens": window, "metadata_error": error}
        self._cache_write(value)
        return value

    def _local_count(self, messages, tools):
        cached = self._cache_read()
        template_supported = cached.get("template_supported", True)
        tokenize_supported = cached.get("tokenize_supported", True)
        rendered = None
        if template_supported:
            try:
                applied = self._request("POST", "/apply-template", json={"messages": messages})
                rendered = applied.get("prompt") if isinstance(applied, dict) else None
                if not isinstance(rendered, str):
                    raise ValueError("local template returned no prompt")
            except (httpx.HTTPError, ValueError, TypeError, OSError):
                template_supported = False
        payload = rendered if isinstance(rendered, str) else _canonical(messages).decode("utf-8")
        if not tokenize_supported:
            raise ValueError("local tokenizer capability is unavailable")
        try:
            response = self._request("POST", "/tokenize", json={"content": payload, "add_special": False})
            tokens = response.get("tokens") if isinstance(response, dict) else None
            if not isinstance(tokens, list):
                raise ValueError("local tokenizer returned no token array")
            count = len(tokens)
            tool_tokens = 0
            if tools:
                tool_response = self._request("POST", "/tokenize",
                                              json={"content": _canonical(tools).decode("utf-8"), "add_special": False})
                values = tool_response.get("tokens") if isinstance(tool_response, dict) else None
                if not isinstance(values, list):
                    raise ValueError("local tokenizer returned no tool token array")
                tool_tokens = len(values)
        except (httpx.HTTPError, ValueError, TypeError, OSError) as exc:
            self._cache_write({**self._cache_read(), "metadata_checked": True,
                               "template_supported": template_supported, "tokenize_supported": False,
                               "tokenize_error": type(exc).__name__})
            raise
        if template_supported != cached.get("template_supported", True):
            self._cache_write({**self._cache_read(), "metadata_checked": True,
                               "template_supported": template_supported, "tokenize_supported": True})
        return count, tool_tokens, bool(rendered is not None)

    def measure(self, messages, tools=(), *, allow_network=True):
        mode = self.config.token_count_mode
        window = int(self.config.context_window_tokens or 0)
        manual_window = bool(window)
        metadata_source = "manual" if manual_window else "unknown"
        output_reserve = self.effective_output_reserve_tokens
        if mode == "off":
            return {"prompt_tokens": None, "source": "unknown", "context_window_tokens": window or None,
                    "context_window_source": metadata_source, "template_overhead_tokens": None,
                    "output_reserve_tokens": output_reserve}

        if mode == "local" and self.config.api_kind == "local" and allow_network:
            try:
                metadata = self._local_metadata()
                if not manual_window:
                    window = int(metadata.get("context_window_tokens") or 0)
                    metadata_source = "llama_cpp" if window else "unknown"
                count, tool_tokens, templated = self._local_count(messages, tools)
                tool_overhead = 64 + 12 * len(tuple(tools)) if tools else 0
                return {"prompt_tokens": count + tool_tokens + tool_overhead,
                        "source": "llama_cpp" if templated and not tools else "llama_cpp_estimate",
                        "exact": bool(templated and not tools),
                        "context_window_tokens": window or None,
                        "context_window_source": metadata_source,
                        "template_overhead_tokens": (32 + 6 * len(messages) if not templated
                                                      else 16 + tool_overhead),
                        "output_reserve_tokens": output_reserve}
            except (httpx.HTTPError, ValueError, TypeError, OSError):
                pass

        serialized = _canonical(messages).decode("utf-8")
        schemas = _canonical(tools).decode("utf-8")
        overhead = 32 + 6 * len(messages)
        return {"prompt_tokens": estimate_text_tokens(serialized) + estimate_text_tokens(schemas) + overhead,
                "source": "estimate", "exact": False, "context_window_tokens": window or None,
                "context_window_source": metadata_source, "template_overhead_tokens": overhead,
                "output_reserve_tokens": output_reserve}
