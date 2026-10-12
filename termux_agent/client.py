"""Small OpenAI-compatible chat-completions client with SSE support."""
from __future__ import annotations

from copy import deepcopy
import json
import random
import re
import time
from typing import Any, Callable

import httpx
from .continuation import MarkerFilter
from .retry import (RetryDeadlineError, backoff, request_deadline,
                    retry_after, thinking_echo_error)


class ClientError(RuntimeError):
    pass


class Client:
    RETRYABLE_STATUS = {500, 502, 503, 504}
    RETRYABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

    def __init__(self, config: Any, http_client: httpx.Client | None = None,
                 sleeper: Callable[[float], None] = time.sleep, *,
                 clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time,
                 randomizer: Callable[[], float] = random.random):
        self.config = config
        self._http = http_client or httpx.Client(timeout=config.timeout)
        self._owns_http = http_client is None
        self._sleep = sleeper
        self._clock, self._wall_clock, self._randomizer = clock, wall_clock, randomizer
        self.on_retry: Callable[[dict[str, Any]], None] | None = None
        self.last_attempts = 0
        self.on_stream: Callable[[str, str], None] | None = None
        self.last_request: dict[str, Any] | None = None
        self.last_usage: dict[str, Any] | None = None
        self.streamed = False
        self.streamed_text = False
        self.last_finish_reason = None
        self.control_markers = ()
        self._marker_filter = None

    def set_control_markers(self, markers):
        self.control_markers = tuple(markers)

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        body = {"model": self.config.model, "messages": messages, "tools": tools,
                "tool_choice": "auto", "stream": bool(getattr(self.config, "stream", True))}
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        self.last_request = deepcopy(body)
        self.last_attempts = 0
        self.last_usage = None
        self.last_finish_reason = None
        self.streamed = self.streamed_text = False
        self._marker_filter = MarkerFilter(self.control_markers) if self.control_markers else None
        headers = {"Content-Type": "application/json", "User-Agent": self.config.user_agent}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        hardened = bool(getattr(self.config, "retry_thinking_400", False))
        deadline = self._clock() + self.config.timeout if hardened else None
        try:
            remaining = deadline - self._clock() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                raise ClientError("LLM retry deadline exceeded")
            with request_deadline(remaining) as accepted:
                return self._chat_attempts(body, url, headers, hardened, deadline, accepted)
        except RetryDeadlineError as exc:
            raise ClientError(str(exc)) from None

    def _chat_attempts(self, body, url, headers, hardened, deadline, accepted):
        waited = 0.0
        # Serialize once so every HTTP attempt carries the same bytes, even if
        # a caller modifies its original message objects while we are waiting.
        wire = self._http.build_request("POST", url, json=body, headers=headers).content
        for attempt in range(3):
            try:
                kwargs = {}
                if deadline is not None:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise ClientError("LLM retry deadline exceeded")
                    kwargs["timeout"] = remaining
                request = self._http.build_request("POST", url, content=wire,
                                                   headers=headers, **kwargs)
                self.last_attempts = attempt + 1
                # Opt-in retries inspect error responses before loading their
                # bodies, including when the model's stream option is false.
                response = self._http.send(request, stream=body["stream"] or hardened)
            except self.RETRYABLE_ERRORS as exc:
                error = f"LLM connection failed ({type(exc).__name__})"
                if attempt < 2:
                    waited = self._wait_retry(attempt, error, type(exc).__name__,
                                              None, None, hardened, deadline, waited)
                    continue
                raise ClientError(error) from None
            except httpx.TimeoutException:
                raise ClientError("LLM request timed out") from None
            except httpx.RequestError as exc:
                host = getattr(getattr(exc, "request", None), "url", None)
                where = f" ({host.host})" if host and host.host else ""
                raise ClientError(f"LLM connection failed{where}") from None
            if response.status_code in self.RETRYABLE_STATUS and attempt < 2:
                status = response.status_code
                after = response.headers.get("Retry-After")
                response.close()
                error = f"LLM endpoint returned HTTP {status}"
                waited = self._wait_retry(attempt, error, "upstream-status", status,
                                          after, hardened, deadline, waited)
                continue
            try:
                if deadline is not None and self._clock() >= deadline:
                    raise ClientError("LLM retry deadline exceeded")
                if hardened and response.status_code == 400:
                    sample = self._error_sample(response, deadline)
                    try:
                        value = json.loads(sample)
                    except ValueError:
                        value = None
                    error = self._http_error(response, sample)
                    if ("text/html" not in response.headers.get("content-type", "").lower()
                            and thinking_echo_error(value) and attempt < 2):
                        after = response.headers.get("Retry-After")
                        response.close()
                        waited = self._wait_retry(attempt, error, "thinking-echo", 400,
                                                  after, True, deadline, waited)
                        continue
                    raise ClientError(error)
                response.raise_for_status()
                if "text/html" in response.headers.get("content-type", "").lower():
                    raise ClientError("LLM endpoint returned HTML instead of API data (possible gateway/WAF response)")
                # Once a valid successful response is accepted, use ordinary
                # body-read timeouts; no path below can restart the request.
                accepted()
                if body["stream"]:
                    self.streamed = True
                    return self._read_sse(response)
                if hardened:
                    response.read()
                result = response.json()
                return self._message(result)
            except httpx.HTTPStatusError as exc:
                raise ClientError(self._http_error(exc.response)) from None
            except httpx.TimeoutException:
                raise ClientError("LLM response timed out") from None
            except httpx.RequestError as exc:
                raise ClientError(f"LLM response interrupted ({type(exc).__name__})") from None
            except ValueError:
                raise ClientError("LLM endpoint returned invalid JSON") from None
            finally:
                response.close()
        raise ClientError("LLM request failed after retries")

    def _wait_retry(self, attempt, error, cause, status, after, hardened, deadline, waited):
        if not hardened:
            self._sleep(0.25 * (2 ** attempt))
            return waited
        delay = backoff(attempt, self._randomizer())
        server_delay = retry_after(after, self._wall_clock())
        if server_delay is not None:
            delay = max(delay, server_delay)
        remaining = deadline - self._clock()
        budget = getattr(self.config, "retry_wait_budget", 20.0) - waited
        if delay > budget or delay >= remaining:
            raise ClientError(f"{error}; retry budget exhausted (required wait {delay:g}s)")
        if self.on_retry:
            self.on_retry({"attempt": attempt + 2, "max_attempts": 3,
                           "status": status, "cause": cause, "wait_seconds": round(delay, 3)})
        if delay >= deadline - self._clock():
            raise ClientError(f"{error}; retry deadline exceeded")
        before = self._clock()
        self._sleep(delay)
        return waited + max(delay, self._clock() - before)

    def _error_sample(self, response, deadline=None):
        sample = b""
        try:
            chunks = iter(response.iter_bytes(chunk_size=512))
            while len(sample) < 2048:
                if deadline is not None and self._clock() >= deadline:
                    break
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                sample += chunk[:2048 - len(sample)]
        except httpx.HTTPError:
            return b""
        return sample

    def _http_error(self, response: httpx.Response, sample=None) -> str:
        # Never echo provider messages or response bodies.
        url = response.request.url
        details = [url.host + url.path]
        sample = self._error_sample(response) if sample is None else sample
        if ("text/html" in response.headers.get("content-type", "").lower()
                or sample.lstrip().lower().startswith((b"<!doctype html", b"<html"))):
            details.append("HTML response; possible gateway/WAF")
        else:
            try:
                result = json.loads(sample)
                error = result.get("error", {}) if isinstance(result, dict) else {}
                if isinstance(error, dict):
                    for name in ("type", "code"):
                        value = error.get(name, "")
                        if not isinstance(value, (str, int)) or isinstance(value, bool):
                            continue
                        value = str(value)
                        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value) and not (
                                self.config.api_key and self.config.api_key in value):
                            details.append(f"{name}={value}")
            except ValueError:
                pass
        methods = [m.strip().upper() for m in response.headers.get("allow", "").split(",")]
        methods = [m for m in methods if m in {"GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH", "CONNECT", "TRACE"}]
        if methods:
            details.append("Allow=" + ",".join(methods))
        diagnostic = f"LLM endpoint returned HTTP {response.status_code} ({'; '.join(details)})"
        return diagnostic.replace(self.config.api_key, "[REDACTED]") if self.config.api_key else diagnostic

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
        self.last_finish_reason = choices[0].get("finish_reason")
        return message

    def _emit(self, kind: str, text: str) -> None:
        if kind == "content" and self._marker_filter is not None:
            text = self._marker_filter.feed(text)
        self._emit_visible(kind, text)

    def _emit_visible(self, kind: str, text: str) -> None:
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
        saw_choice = False

        def consume(data: str) -> None:
            nonlocal usage, finish, saw_choice
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
            if not isinstance(choice.get("delta"), dict) and not choice.get("finish_reason"):
                return
            saw_choice = True
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
        if not saw_choice or not (content or reasoning or calls or finish):
            raise ClientError("LLM endpoint returned no assistant choices in SSE; possible gateway response")
        if self._marker_filter is not None:
            self._emit_visible("content", self._marker_filter.feed("", final=True))
        self.last_usage = usage
        self.last_finish_reason = finish
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return message

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
