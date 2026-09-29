"""Small OpenAI-compatible chat-completions client with SSE support."""
from __future__ import annotations

from copy import deepcopy
import json
import time
from typing import Any, Callable

import httpx


class ClientError(RuntimeError):
    pass


class Client:
    RETRYABLE_STATUS = {502, 503, 504}
    RETRYABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

    def __init__(self, config: Any, http_client: httpx.Client | None = None,
                 sleeper: Callable[[float], None] = time.sleep):
        self.config = config
        self._http = http_client or httpx.Client(timeout=config.timeout)
        self._owns_http = http_client is None
        self._sleep = sleeper
        self.on_stream: Callable[[str, str], None] | None = None
        self.last_request: dict[str, Any] | None = None
        self.last_usage: dict[str, Any] | None = None
        self.streamed = False
        self.streamed_text = False

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        body = {"model": self.config.model, "messages": messages, "tools": tools,
                "tool_choice": "auto", "stream": bool(getattr(self.config, "stream", True))}
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        self.last_request = deepcopy(body)
        self.last_usage = None
        self.streamed = self.streamed_text = False
        headers = {"Content-Type": "application/json", "User-Agent": self.config.user_agent}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        for attempt in range(3):
            try:
                request = self._http.build_request("POST", url, json=body, headers=headers)
                response = self._http.send(request, stream=body["stream"])
            except self.RETRYABLE_ERRORS as exc:
                if attempt < 2:
                    self._sleep(0.25 * (2 ** attempt))
                    continue
                raise ClientError(f"LLM connection failed ({type(exc).__name__})") from None
            except httpx.TimeoutException:
                raise ClientError("LLM request timed out") from None
            except httpx.RequestError as exc:
                host = getattr(getattr(exc, "request", None), "url", None)
                where = f" ({host.host})" if host and host.host else ""
                raise ClientError(f"LLM connection failed{where}") from None
            if response.status_code in self.RETRYABLE_STATUS and attempt < 2:
                response.close()
                self._sleep(0.25 * (2 ** attempt))
                continue
            try:
                response.raise_for_status()
                if body["stream"]:
                    self.streamed = True
                    return self._read_sse(response)
                result = response.json()
                return self._message(result)
            except httpx.HTTPStatusError as exc:
                raise ClientError(f"LLM endpoint returned HTTP {exc.response.status_code}") from None
            except httpx.TimeoutException:
                raise ClientError("LLM response timed out") from None
            except httpx.RequestError as exc:
                raise ClientError(f"LLM response interrupted ({type(exc).__name__})") from None
            except ValueError:
                raise ClientError("LLM endpoint returned invalid JSON") from None
            finally:
                response.close()
        raise ClientError("LLM request failed after retries")

    def _message(self, result: Any) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise ClientError("LLM endpoint returned an invalid response")
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ClientError("LLM response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ClientError("LLM response has no assistant message")
        self.last_usage = result.get("usage") if isinstance(result.get("usage"), dict) else None
        return message

    def _emit(self, kind: str, text: str) -> None:
        if not text:
            return
        if self.on_stream:
            self.on_stream(kind, text)
        if kind == "content":
            self.streamed_text = True

    def _read_sse(self, response: httpx.Response) -> dict[str, Any]:
        content, reasoning = [], []
        calls: dict[int, dict[str, Any]] = {}
        data_lines = []
        usage = None
        finish = None

        def consume(data: str) -> None:
            nonlocal usage, finish
            if not data or data.strip() in ("[DONE]", "null"):
                return
            try:
                event = json.loads(data)
            except ValueError:
                raise ClientError("LLM endpoint returned malformed SSE JSON") from None
            if not isinstance(event, dict):
                return
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
            choices = event.get("choices") or []
            if not choices or not isinstance(choices[0], dict):
                return
            choice = choices[0]
            finish = choice.get("finish_reason") or finish
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                return
            piece = delta.get("content")
            if isinstance(piece, str):
                content.append(piece)
                self._emit("content", piece)
            piece = delta.get("reasoning_content")
            if isinstance(piece, str):
                reasoning.append(piece)
                self._emit("reasoning", piece)
            for item in delta.get("tool_calls") or []:
                if not isinstance(item, dict):
                    continue
                index = item.get("index", 0)
                if not isinstance(index, int):
                    continue
                call = calls.setdefault(index, {"id": "", "type": "function",
                                                "function": {"name": "", "arguments": ""}})
                for field in ("id", "type"):
                    value = item.get(field)
                    if isinstance(value, str):
                        call[field] = call[field] + value if field == "id" else value
                function = item.get("function") or {}
                for field in ("name", "arguments"):
                    value = function.get(field) if isinstance(function, dict) else None
                    if isinstance(value, str):
                        call["function"][field] += value

        for line in response.iter_lines():
            if not line:
                if data_lines:
                    consume("\n".join(data_lines))
                    data_lines.clear()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            consume("\n".join(data_lines))
        self.last_usage = usage
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return message

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
