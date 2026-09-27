"""Small OpenAI-compatible chat-completions client using httpx."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import httpx


class ClientError(RuntimeError):
    pass


class Client:
    def __init__(self, config: Any, http_client: httpx.Client | None = None):
        self.config = config
        self._http = http_client or httpx.Client(timeout=config.timeout)
        self._owns_http = http_client is None
        self.last_request: dict[str, Any] | None = None
        self.last_usage: dict[str, Any] | None = None

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        body = {"model": self.config.model, "messages": messages, "tools": tools, "tool_choice": "auto"}
        # This is diagnostic context only; it never contains the authorization header.
        self.last_request = deepcopy(body)
        self.last_usage = None
        headers = {"Content-Type": "application/json", "User-Agent": self.config.user_agent}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        try:
            response = self._http.post(url, json=body, headers=headers)
            response.raise_for_status()
            result = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            # Do not include response text or request headers; either may echo credentials.
            raise ClientError(f"LLM endpoint returned HTTP {status}") from None
        except httpx.TimeoutException:
            raise ClientError("LLM request timed out") from None
        except httpx.RequestError as exc:
            # Exception strings can contain URLs, but never headers/keys. Strip query strings.
            host = getattr(getattr(exc, "request", None), "url", None)
            where = f" ({host.host})" if host and host.host else ""
            raise ClientError(f"LLM connection failed{where}") from None
        except ValueError:
            raise ClientError("LLM endpoint returned invalid JSON") from None
        if not isinstance(result, dict):
            raise ClientError("LLM endpoint returned an invalid response")
        self.last_usage = result.get("usage") if isinstance(result.get("usage"), dict) else None
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ClientError("LLM response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ClientError("LLM response has no assistant message")
        return message

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
