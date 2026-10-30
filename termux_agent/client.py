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
from .retry import (RetryDeadlineError, backoff, rate_limit_error, request_deadline,
                    retry_after, thinking_echo_error)


def upstream_error_fields(value):
    """Read structured diagnostic fields without dumping arbitrary response bodies."""
    if not isinstance(value, dict):
        return {}
    error = value.get('error')
    nested = error if isinstance(error, dict) else {}
    result = {}
    for name in ('code', 'type'):
        for candidate in (nested.get(name), value.get(name)):
            if isinstance(candidate, (str, int)) and not isinstance(candidate, bool) and str(candidate):
                result[name] = candidate
                break
    for candidate in (nested.get('message'), nested.get('msg'), value.get('message'),
                      value.get('msg'), error):
        if isinstance(candidate, str) and candidate.strip():
            result['message'] = candidate
            break
    return result


def upstream_error_text(value, api_key='', limit=300):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return ''
    text = str(value)
    # Mask the full value before shortening; otherwise a token cut in half leaks.
    if isinstance(api_key, str) and api_key:
        text = text.replace(api_key, '[REDACTED]')
    text = ' '.join(text.split())
    text = ''.join(c if c.isprintable() else f'\\x{ord(c):02x}' for c in text)
    return text[:limit]


class ClientError(RuntimeError):
    pass


class ProviderError(ClientError):
    pass


class NetworkError(ClientError):
    pass


class ContextLengthError(ClientError):
    def __init__(self, text, *, pre_accept=True):
        super().__init__(text)
        self.pre_accept = bool(pre_accept)


class EmptyResponseError(ClientError):
    def __init__(self, text, message, *, length=False, context_limited=False):
        super().__init__(text)
        self.assistant_message = message
        self.length = bool(length)
        self.context_limited = bool(context_limited)


class IncompleteOutputError(ClientError):
    def __init__(self, text, message, *, context_limited=False):
        super().__init__(text)
        self.assistant_message = message
        self.context_limited = bool(context_limited)


class Client:
    RETRYABLE_STATUS = {500, 502, 503, 504}
    # Only used while sending/receiving headers, never during successful body reads.
    RETRYABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                        httpx.ReadError, httpx.RemoteProtocolError)

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
        self.context_limited = False
        self.control_markers = ()
        self._marker_filter = None

    def set_control_markers(self, markers):
        self.control_markers = tuple(markers)

    def _prepare_responses_request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        url = self.config.base_url.rstrip("/") + "/responses"
        def parts(content, role):
            kind = 'output_text' if role == 'assistant' else 'input_text'
            if content is None:
                return []
            if isinstance(content, str):
                return [{'type': kind, 'text': content}]
            if isinstance(content, list):
                result = []
                for part in content:
                    text = part if isinstance(part, str) else part.get('text') if isinstance(part, dict) else None
                    if not isinstance(text, str):
                        raise ClientError('Responses adapter supports text message parts only')
                    result.append({'type': kind, 'text': text})
                return result
            raise ClientError('Responses message content must be text')
        resp_input = []
        for m in messages:
            role = m.get("role", "user")
            if role == "tool":
                resp_input.append({
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id", ""),
                    "output": (m.get('content', '') if isinstance(m.get('content', ''), str)
                               else json.dumps(m.get('content'), ensure_ascii=False))
                })
            elif role == "assistant":
                saved = m.get('_responses_output')
                if saved:
                    if not isinstance(saved, list) or any(not isinstance(item, dict) for item in saved):
                        raise ClientError('Invalid saved Responses output items')
                    for item in deepcopy(saved):
                        item.pop('id', None)
                        resp_input.append(item)
                    continue
                content = m.get("content")
                if content:
                    resp_input.append({
                        "type": "message",
                        "role": "assistant",
                        "content": parts(content, role)
                    })
                for tc in m.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    resp_input.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": (json.dumps(fn['arguments'], ensure_ascii=False) if isinstance(fn.get('arguments'), dict)
                                      else fn.get("arguments", "{}"))
                    })
            else:
                content = m.get("content", "")
                resp_input.append({
                    "type": "message",
                    "role": role,
                    "content": parts(content, role)
                })
        body: dict[str, Any] = {
            "model": self.config.model,
            "input": resp_input,
            "stream": bool(getattr(self.config, "stream", True)),
            "store": False,
            "include": ["reasoning.encrypted_content"]
        }
        if getattr(self.config, "max_output_tokens", 0):
            body["max_output_tokens"] = self.config.max_output_tokens
        if getattr(self.config, "reasoning_effort", ""):
            body["reasoning"] = {"effort": self.config.reasoning_effort}
        if tools:
            resp_tools = []
            for t in tools:
                if t.get("type") == "function" and "function" in t:
                    fn = t["function"]
                    resp_tools.append({
                        "type": "function",
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {}),
                        "strict": fn.get("strict", False)
                    })
                else:
                    resp_tools.append(t)
            body["tools"] = resp_tools
        return url, body

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        if getattr(self.config, "wire_api", "chat_completions") == "responses":
            url, body = self._prepare_responses_request(messages, tools)
        else:
            private = {'_responses_output', '_incomplete_output', '_incomplete_tool_calls'}
            chat_messages = [{k: v for k, v in m.items() if k not in private} for m in messages]
            body = {"model": self.config.model, "messages": chat_messages, "tools": tools,
                    "tool_choice": "auto", "stream": bool(getattr(self.config, "stream", True))}
            if getattr(self.config, "max_output_tokens", 0):
                body[getattr(self.config, "chat_max_tokens_field", "max_completion_tokens")] = self.config.max_output_tokens
            if getattr(self.config, "reasoning_effort", ""):
                body["reasoning_effort"] = self.config.reasoning_effort
            if body["stream"] and getattr(self.config, "chat_include_usage", False):
                body["stream_options"] = {"include_usage": True}
            url = self.config.base_url.rstrip("/") + "/chat/completions"
        self.last_request = deepcopy(body)
        self.last_attempts = 0
        self.last_usage = None
        self.last_finish_reason = None
        self.context_limited = False
        self.streamed = self.streamed_text = False
        self._marker_filter = MarkerFilter(self.control_markers) if self.control_markers else None
        headers = {"Content-Type": "application/json", "User-Agent": self.config.user_agent}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        thinking_retry = bool(getattr(self.config, "retry_thinking_400", False))
        deadline = self._clock() + self.config.timeout
        try:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise NetworkError("LLM retry deadline exceeded")
            with request_deadline(remaining) as accepted:
                return self._chat_attempts(body, url, headers, thinking_retry, deadline, accepted)
        except RetryDeadlineError as exc:
            raise NetworkError(str(exc)) from None

    def _chat_attempts(self, body, url, headers, thinking_retry, deadline, accepted):
        waited = 0.0
        # Serialize once so every HTTP attempt carries the same bytes, even if
        # a caller modifies its original message objects while we are waiting.
        wire = self._http.build_request("POST", url, json=body, headers=headers).content
        max_attempts = getattr(self.config, "retry_max_attempts", 3)
        for attempt in range(max_attempts):
            try:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise NetworkError("LLM retry deadline exceeded")
                request = self._http.build_request("POST", url, content=wire,
                                                   headers=headers, timeout=remaining)
                self.last_attempts = attempt + 1
                # Inspect status/headers before reading any body, even for JSON.
                response = self._http.send(request, stream=True)
            except self.RETRYABLE_ERRORS as exc:
                error = f"LLM connection failed ({type(exc).__name__})"
                if attempt + 1 < max_attempts:
                    waited = self._wait_retry(attempt, error, type(exc).__name__,
                                              None, None, deadline, waited)
                    continue
                raise NetworkError(error) from None
            except httpx.TimeoutException:
                raise NetworkError("LLM request timed out") from None
            except httpx.RequestError as exc:
                host = getattr(getattr(exc, "request", None), "url", None)
                where = f" ({host.host})" if host and host.host else ""
                raise NetworkError(f"LLM connection failed{where}") from None
            if response.status_code in self.RETRYABLE_STATUS and attempt + 1 < max_attempts:
                status = response.status_code
                after = response.headers.get("Retry-After")
                response.close()
                error = f"LLM endpoint returned HTTP {status}"
                waited = self._wait_retry(attempt, error, "upstream-status", status,
                                          after, deadline, waited)
                continue
            try:
                if self._clock() >= deadline:
                    raise NetworkError("LLM retry deadline exceeded")
                if (thinking_retry and response.status_code == 400) or response.status_code == 429:
                    sample = self._error_sample(response, deadline)
                    try:
                        value = json.loads(sample)
                    except ValueError:
                        value = None
                    error = self._http_error(response, sample)
                    classified = self._classified_error(response, sample)
                    if isinstance(classified, ContextLengthError):
                        raise classified
                    matched = (thinking_echo_error(value) if response.status_code == 400
                               else rate_limit_error(value))
                    if ("text/html" not in response.headers.get("content-type", "").lower()
                            and matched and attempt + 1 < max_attempts):
                        after = response.headers.get("Retry-After")
                        status = response.status_code
                        response.close()
                        cause = "thinking-echo" if status == 400 else "rate-limit"
                        waited = self._wait_retry(attempt, error, cause, status,
                                                  after, deadline, waited)
                        continue
                    raise classified
                response.raise_for_status()
                if "text/html" in response.headers.get("content-type", "").lower():
                    raise ProviderError("LLM endpoint returned HTML instead of API data (possible gateway/WAF response)")
                # Once a valid successful response is accepted, use ordinary
                # body-read timeouts; no path below can restart the request.
                accepted()
                if body["stream"]:
                    self.streamed = True
                    return self._checked_message(self._read_sse(response))
                response.read()
                result = response.json()
                return self._checked_message(self._message(result))
            except httpx.HTTPStatusError as exc:
                raise self._classified_error(exc.response) from None
            except httpx.TimeoutException:
                raise NetworkError("LLM response timed out") from None
            except httpx.RequestError as exc:
                raise NetworkError(f"LLM response interrupted ({type(exc).__name__})") from None
            except ValueError:
                raise ProviderError("LLM endpoint returned invalid JSON") from None
            finally:
                response.close()
        raise NetworkError("LLM request failed after retries")

    def _wait_retry(self, attempt, error, cause, status, after, deadline, waited):
        delay = backoff(attempt, self._randomizer())
        server_delay = retry_after(after, self._wall_clock())
        if server_delay is not None:
            delay = max(delay, server_delay)
        remaining = deadline - self._clock()
        budget = getattr(self.config, "retry_wait_budget", 20.0) - waited
        if delay > budget or delay >= remaining:
            raise NetworkError(f"{error}; retry budget exhausted (required wait {delay:g}s)")
        if self.on_retry:
            self.on_retry({"attempt": attempt + 2, "max_attempts": getattr(self.config, "retry_max_attempts", 3),
                           "status": status, "cause": cause, "wait_seconds": round(delay, 3)})
        if delay >= deadline - self._clock():
            raise NetworkError(f"{error}; retry deadline exceeded")
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

    def _classified_error(self, response, sample=None):
        sample = self._error_sample(response) if sample is None else sample
        diagnostic = self._http_error(response, sample)
        if response.status_code in (400, 413, 422) and 'text/html' not in response.headers.get('content-type', '').lower():
            try:
                data = json.loads(sample)
                error = upstream_error_fields(data)
                if isinstance(error, dict):
                    code = str(error.get('code', '')).lower()
                    text = str(error.get('message', '')).lower()
                    if code in ('context_length_exceeded', 'context_window_exceeded', 'prompt_too_long',
                                'context_shift', 'context_shift_error') or any(
                            phrase in text for phrase in ('maximum context length', 'context length exceeded',
                                                         'context window exceeded', 'prompt is too long',
                                                         'context shift', 'input truncated to fit context')):
                        return ContextLengthError('上下文超过模型窗口；请使用 /compact task 归档并缩短当前任务，'
                                                  '/compact N 保留最近轮次，或 /clear 新建会话。')
                    if code == 'content-blocked' or 'content-blocked' in text:
                        return ProviderError(
                            f"{diagnostic}（提示：上游网关安全规则拦截；类似'阅读 guide.md'的'动词+带后缀文件名'结构"
                            f"易被网关误判为外链引流。可改写为'阅读文件 guide.md'、'请阅读 guide.md'或'read guide.md'重试）"
                        )
            except ValueError:
                pass
        return ProviderError(diagnostic)

    def _checked_message(self, message):
        content = message.get('content')
        if self.last_finish_reason == 'length':
            incomplete = dict(message)
            calls = incomplete.pop('tool_calls', None)
            if calls:
                incomplete['_incomplete_tool_calls'] = calls
            if content is None or isinstance(content, str) and not content.strip():
                raise EmptyResponseError('模型输出预算已耗尽，未产生最终回答；请提高服务端输出上限或降低思考强度。',
                                         incomplete, length=True, context_limited=self.context_limited)
            raise IncompleteOutputError('模型输出在达到长度上限时截断；正文或工具参数只是未完成草稿，其中工具调用不会执行。',
                                        incomplete, context_limited=self.context_limited)
        if (not message.get('tool_calls') and (content is None or isinstance(content, str) and not content.strip())
                and not message.get('refusal')
                and self.last_finish_reason not in ('content_filter', 'safety', 'refusal')):
            if message.get('reasoning_content'):
                text = '模型仅返回思考内容，未产生最终回答；请检查输出预算和模型设置。'
            else:
                text = '模型返回空回答；请检查模型设置或稍后重试。'
            raise EmptyResponseError(text, message)
        return message

    def _http_error(self, response: httpx.Response, sample=None) -> str:
        url = response.request.url
        details = [url.host + url.path]
        sample = self._error_sample(response) if sample is None else sample
        if ("text/html" in response.headers.get("content-type", "").lower()
                or sample.lstrip().lower().startswith((b"<!doctype html", b"<html"))):
            details.append("HTML response; possible gateway/WAF")
        else:
            try:
                result = json.loads(sample)
                error = upstream_error_fields(result)
                msg = upstream_error_text(error.get('message', ''), self.config.api_key, 300)
                if msg:
                    details.append(f'message={msg}')
                for name in ('type', 'code'):
                    value = upstream_error_text(error.get(name, ''), self.config.api_key, 81)
                    if re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', value):
                        details.append(f'{name}={value}')
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
        if self.config.wire_api == "responses":
            from .responses import message
            return message(self, result)
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ClientError("LLM response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ClientError("LLM response has no assistant message")
        self.last_usage = result.get("usage") if isinstance(result.get("usage"), dict) else None
        self.last_finish_reason = choices[0].get("finish_reason")
        self._apply_local_response_flags(result, message)
        return message

    def _apply_local_response_flags(self, result, message=None):
        values = [result]
        if isinstance(message, dict):
            values.append(message)
        if any(value.get('truncated') is True or value.get('context_shift') is True
               or value.get('context_shifted') is True or value.get('input_truncated') is True
               or value.get('stop_type') in ('context_shift', 'truncated')
               for value in values if isinstance(value, dict)):
            self.last_finish_reason = 'length'
            self.context_limited = True

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
        if self.config.wire_api == "responses":
            from .responses import read_sse
            return read_sse(self, response)
        content, reasoning = [], []
        calls: dict[int, dict[str, Any]] = {}
        data_lines = []
        usage = None
        finish = None
        local_flags = False
        saw_choice = False

        def consume(data: str) -> None:
            nonlocal usage, finish, local_flags, saw_choice
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
            local_flags = local_flags or any(
                event.get(field) is True for field in ('truncated', 'context_shift', 'context_shifted',
                                                        'input_truncated'))
            local_flags = local_flags or event.get('stop_type') in ('context_shift', 'truncated')
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
        if local_flags:
            self.last_finish_reason = 'length'
            self.context_limited = True
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return message

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
