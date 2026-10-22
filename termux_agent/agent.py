"""Tool loop and compact conversation state for Termux."""

from __future__ import annotations

import json
from dataclasses import replace
import uuid
from typing import Any, Callable

from .client import Client, ClientError, ContextLengthError, EmptyResponseError
from .context import context_chars
from .tools import TOOLS, DELEGATE_TOOL, WEB_FETCH_TOOL, execute, web_fetch_available

from .approval import needs_approval
from .instructions import load_system_prompt, load_prompt_file
from .display_privacy import hide_write_payloads
from .continuation import TASK_MARKERS, provider_blocked, task_status


class Agent:
    def __init__(self, config: Any, approve: Callable[[str, dict[str, Any]], bool] | None = None,
                 event: Callable[[str, dict[str, Any]], None] | None = None, client: Any | None = None, instructions: str = "",
                 base_system: str | None = None, *, allow_delegation: bool = True):
        self.config = config
        self.config.validate()
        self.approve = approve
        self.event = event
        self.checkpoint = None
        self.base_system = base_system if base_system is not None else (
            load_system_prompt() + ("\n\n" + instructions if instructions else ""))
        self.client = client or Client(config)
        self._allow_delegation = allow_delegation
        self._web_fetch_enabled = config.web_fetch_enabled and web_fetch_available(config.lightpanda_binary or None)
        self.tools = (TOOLS + ([WEB_FETCH_TOOL] if self._web_fetch_enabled else []) +
                      ([DELEGATE_TOOL] if allow_delegation and config.max_subagents else []))
        self._subagents_this_run = 0
        self._subagent_calls_total = 0
        self._subagent_api_calls_total = 0
        self._subagent_tool_calls_total = 0
        self.active_skills: dict[str, str] = {}
        self.system = self.base_system
        self._direct_shell_count = 0
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self.system}]
        self._total_calls = 0
        self._tool_calls_total = 0
        self._usage_history: list[dict[str, Any] | None] = []
        self._request_history_chars: list[int] = []
        self._conversation_count = 0
        self._approval_denied = False
        self._last_auto_prompt_chars = 0
        self.last_run = {"status": "idle", "auto": False, "continuations": 0}

    def _sync_skills(self) -> None:
        self.system = self.base_system
        for name, content in self.active_skills.items():
            self.system += f"\n\n<!-- SKILL_START: {name} -->\n{content}\n<!-- SKILL_END: {name} -->"
        if self.messages:
            self.messages[0]["content"] = self.system

    def load_skill(self, name: str, content: str) -> None:
        from .skills import validate_skill_name, MAX_SKILL_CHARS, MAX_ACTIVE_SKILL_CHARS
        validate_skill_name(name)
        if len(content) > MAX_SKILL_CHARS:
            raise ValueError(f"Skill exceeds {MAX_SKILL_CHARS} characters")
        total = sum(len(v) for k, v in self.active_skills.items() if k != name) + len(content)
        if total > MAX_ACTIVE_SKILL_CHARS:
            raise ValueError(f"Active skills exceed {MAX_ACTIVE_SKILL_CHARS} characters")
        self.active_skills[name] = content
        self._sync_skills()

    def unload_skill(self, name: str) -> None:
        self.active_skills.pop(name, None)
        self._sync_skills()

    def clear_skills(self) -> None:
        self.active_skills.clear()
        self._sync_skills()

    def get_active_skills(self) -> dict[str, str]:
        return dict(self.active_skills)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.event:
            try:
                self.event(kind, payload)
            except Exception:
                pass

    def _checkpoint(self):
        # Persistence failures must stop further tools, rather than vanish in _emit.
        if self.checkpoint:
            self.checkpoint()

    @staticmethod
    def _tool_args(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
        if isinstance(raw, dict):
            return raw, None
        if not isinstance(raw, str):
            return None, "tool arguments must be a JSON object"
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None, "tool arguments are malformed JSON"
        if not isinstance(value, dict):
            return None, "tool arguments must decode to an object"
        return value, None

    def _approved(self, name: str, args: dict[str, Any]) -> bool:
        policy = self.config.approval_policy
        required = needs_approval(name, args, policy)
        if not required:
            return True
        if self.approve is None:
            return False
        try:
            return bool(self.approve(name, args))
        except Exception:
            return False

    def run(self, text: str, auto_continue: bool | None = None) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("input must be a non-empty string")
        automatic = self.config.auto_continue if auto_continue is None else auto_continue
        rules = load_prompt_file("task_prompt.txt") if automatic else ""
        nudge = load_prompt_file("task_continue.txt") if automatic else ""
        self._last_auto_prompt_chars = len(rules) + 2 if rules else 0
        self._approval_denied = False
        self._subagents_this_run = 0
        self.last_run = {"status": "running", "auto": automatic, "continuations": 0}
        pauses = 0
        warned_context = False
        if hasattr(self.client, "set_control_markers"):
            self.client.set_control_markers(TASK_MARKERS if automatic else ())

        def finish(status, value):
            self.last_run["status"] = status
            self._checkpoint()
            return value

        self.messages.append({"role": "user", "content": text})
        self._conversation_count += 1
        self._checkpoint()
        for step in range(self.config.max_steps):
            request_messages = self.messages
            if rules:
                request_messages = [{"role": "system", "content": self.system + "\n\n" + rules},
                                    *self.messages[1:]]
            chars = context_chars(request_messages, self.tools)
            if chars > self.config.max_context_chars:
                return finish('context_limit', f'LLM 请求失败：上下文保护 {chars:,}/{self.config.max_context_chars:,} 字符；'
                              '请使用 /compact 归档并缩短上下文，或 /clear 新建会话。')
            if not warned_context and chars >= self.config.max_context_chars * 0.8:
                self._emit('context_warning', {'chars': chars, 'limit': self.config.max_context_chars})
                warned_context = True
            self._emit('request_start', {})
            self._total_calls += 1
            try:
                answer = self.client.chat(request_messages, self.tools)
            except KeyboardInterrupt:
                self._usage_history.append(None)
                self._request_history_chars.append(self._history_chars(request_messages))
                return finish("interrupted", "LLM 请求已中断。")
            except Exception as exc:
                self._usage_history.append(self._clean_usage(getattr(self.client, 'last_usage', None))
                                           if isinstance(exc, ClientError) else None)
                self._request_history_chars.append(self._history_chars(request_messages))
                if isinstance(exc, EmptyResponseError):
                    self.messages.append({**exc.assistant_message, 'role': 'assistant'})
                status = 'context_limit' if isinstance(exc, ContextLengthError) else 'empty_response' if isinstance(exc, EmptyResponseError) else 'error'
                return finish(status, f"LLM 请求失败：{exc}")
            if not isinstance(answer, dict):
                self._usage_history.append(None)
                self._request_history_chars.append(self._history_chars(request_messages))
                return finish("error", "LLM 响应格式错误：assistant message must be an object")
            usage = getattr(self.client, "last_usage", None)
            usage = self._clean_usage(usage)
            self._usage_history.append(usage)
            self._request_history_chars.append(self._history_chars(request_messages))
            content = answer.get("content")
            content = content if isinstance(content, str) else ""
            status = None
            if automatic:
                content, status = task_status(content)
            reasoning = {"reasoning_content": answer["reasoning_content"]} if "reasoning_content" in answer else {}
            if '_responses_output' in answer:
                reasoning['_responses_output'] = answer['_responses_output']
            if provider_blocked(answer, getattr(self.client, "last_finish_reason", None)):
                self.messages.append({"role": "assistant", "content": content, **reasoning})
                self.last_run["blocked_by"] = "provider"
                return finish("blocked", content or "自动任务已停止：模型服务拒绝了请求。")
            calls = answer.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                self.messages.append({"role": "assistant", "content": content, **reasoning})
                if not automatic:
                    return finish("complete", content)
                if self._approval_denied:
                    return finish("blocked", "任务因审批拒绝停止；未执行被拒绝的操作。")
                if status == "done":
                    return finish("complete", content)
                if status == "blocked":
                    return finish("blocked", content or "自动任务已阻塞，需要用户输入或权限。")
                if step == self.config.max_steps - 1:
                    return finish("limit", "已达到最大模型请求次数；自动任务尚未确认完成。")
                if pauses >= self.config.max_continuations:
                    return finish("stalled", "自动任务已停止：连续多轮未执行工具或确认完成。")
                pauses += 1
                self.last_run["continuations"] += 1
                self._emit("continuation", {"step": step + 1, "attempt": pauses,
                                           "max_steps": self.config.max_steps})
                self.messages.append({"role": "user", "content": nudge})
                self._checkpoint()
                continue
            invalid = self._validate_calls(calls)
            if invalid:
                # Do not persist a malformed assistant tool-call turn: subsequent
                # requests must never contain dangling or invented tool_call_ids.
                self.messages.append({"role": "assistant", "content": content if isinstance(content, str) else "", **reasoning})
                return finish("error", f"模型返回了格式错误的工具调用：{invalid}")
            self.messages.append({"role": "assistant", "content": content, "tool_calls": calls, **reasoning})
            self._checkpoint()
            if automatic and self._approval_denied:
                self._fill_tool_results(calls, denied=True, reason="task stopped after approval denial")
                return finish("blocked", "任务因审批拒绝停止；未执行新的工具调用。")
            if step == self.config.max_steps - 1:
                self._fill_tool_results(calls, denied=True, reason="maximum API request count reached")
                return finish("limit", "已达到最大模型请求次数；未执行最后一轮工具调用。")
            interrupted = self._fill_tool_results(calls)
            pauses = 0
            if interrupted:
                return finish("interrupted", "工具执行已中断；历史记录已保留为完整工具调用。")
        return finish("limit", "已达到最大模型请求次数。")

    @staticmethod
    def _validate_calls(calls: list[Any]) -> str | None:
        seen: set[str] = set()
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"].strip():
                return "缺少有效的调用 ID"
            if call["id"] in seen:
                return "调用 ID 重复"
            seen.add(call["id"])
            function = call.get("function")
            if call.get("type") not in (None, "function") or not isinstance(function, dict):
                return "调用结构无效"
            if not isinstance(function.get("name"), str) or not function["name"].strip():
                return "缺少工具名称"
        return None

    @staticmethod
    def _clean_usage(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        usage = {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            number = value.get(field)
            if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                usage[field] = number
        details = value.get('completion_tokens_details')
        reasoning = value.get('reasoning_tokens', details.get('reasoning_tokens') if isinstance(details, dict) else None)
        if isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0:
            usage['reasoning_tokens'] = reasoning
        return usage or None

    def _history_chars(self, messages=None) -> int:
        messages = self.messages if messages is None else messages
        return sum(len(json.dumps(m, ensure_ascii=False, separators=(",", ":"))) for m in messages)

    def _fill_tool_results(self, calls: list[Any], denied: bool = False, reason: str = "") -> bool:
        """Append one tool result per call, even if one call is malformed/interrupted."""
        interrupted = False
        for index, call in enumerate(calls):
            call = call if isinstance(call, dict) else {}
            call_id = call["id"]
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = function.get("name") if isinstance(function.get("name"), str) else ""
            args, parse_error = self._tool_args(function.get("arguments", "{}"))
            self._tool_calls_total += 1
            if denied:
                result: Any = {"error": reason}
            elif interrupted:
                result = {"error": "not run because the preceding tool was interrupted"}
            elif parse_error:
                result = {"error": parse_error}
            elif not name:
                result = {"error": "tool name is missing"}
            elif name == "web_fetch" and not self._web_fetch_enabled:
                result = {"error": "web_fetch is disabled or lightpanda is unavailable"}
            elif not self._approved(name, args or {}):
                self._approval_denied = True
                result = {"error": "approval denied or unavailable"}
            else:
                self._emit("tool_start", {"name": name, "arguments": args})
                try:
                    if name == "delegate":
                        result = self._delegate(args or {})
                    elif name == "web_fetch":
                        result = execute(name, args or {}, self.config.max_output_chars,
                                         lightpanda_binary=self.config.lightpanda_binary or None)
                    else:
                        result = execute(name, args or {}, self.config.max_output_chars)
                except KeyboardInterrupt:
                    interrupted = True
                    result = {"error": "tool execution interrupted"}
                except Exception as exc:
                    result = {"error": f"{type(exc).__name__}: {exc}"}
                self._emit("tool_result", {"name": name, "result": result})
            self.messages.append({"role": "tool", "tool_call_id": call_id,
                                  "content": json.dumps(result, ensure_ascii=False, separators=(",", ":"))})
            self._checkpoint()
        return interrupted

    def _delegate(self, args):
        if not self._allow_delegation or not self.config.max_subagents:
            return {"error": "sub-agent delegation is disabled"}
        if set(args) - {"task", "max_steps"}:
            return {"error": "delegate accepts only task and max_steps"}
        task = args.get("task")
        if not isinstance(task, str) or not task.strip() or len(task) > 16000:
            return {"error": "task must contain 1–16000 characters"}
        requested = args.get("max_steps", self.config.subagent_max_steps)
        if not isinstance(requested, int) or isinstance(requested, bool) or not 1 <= requested <= 12:
            return {"error": "max_steps must be between 1 and 12"}
        if self._subagents_this_run >= self.config.max_subagents:
            return {"error": "sub-agent limit reached for this task"}
        self._subagents_this_run += 1
        self._subagent_calls_total += 1
        identifier = f"sub-{self._subagent_calls_total}"
        child_config = replace(self.config, auto_continue=False,
                               max_steps=min(requested, self.config.subagent_max_steps, self.config.max_steps))
        base = self.base_system + "\n\n" + load_prompt_file("subagent_prompt.txt")
        child_client = Client(child_config)
        child_client.on_retry = lambda info: self._emit("subagent_retry", {**info, "id": identifier})
        child = None
        status = "error"
        self._emit("subagent_start", {"id": identifier, "model": child_config.model,
                                      "max_steps": child_config.max_steps})
        try:
            child = Agent(child_config, approve=self.approve, client=child_client,
                          event=lambda kind, payload: self._emit(kind, {**payload, "subagent": identifier}),
                          base_system=base, allow_delegation=False)
            summary = child.run(task, auto_continue=False)
            status = child.last_run["status"]
            if child._approval_denied:
                self._approval_denied = True
                status = "blocked"
            if status == "interrupted":
                raise KeyboardInterrupt
            cap = min(4000, self.config.max_output_chars)
            truncated = len(summary) > cap
            return {"id": identifier, "status": status,
                    "summary": summary[:cap] + ("\n[TRUNCATED]" if truncated else ""),
                    "api_calls": child._total_calls, "tool_calls": child._tool_calls_total,
                    "usage": child.stats()["usage_totals"]}
        finally:
            if child is not None:
                self._subagent_api_calls_total += child._total_calls
                self._subagent_tool_calls_total += child._tool_calls_total
            child_client.close()
            self._emit("subagent_end", {"id": identifier, "status": status})

    def delegate(self, task):
        """Explicit /sub command; record a complete tool turn like model delegation."""
        if not isinstance(task, str) or not task.strip() or len(task) > 16000:
            raise ValueError("task must contain 1–16000 characters")
        self._subagents_this_run = 0
        self._approval_denied = False
        self._conversation_count += 1
        self.messages.append({"role": "user", "content": "/sub " + task})
        self._checkpoint()
        calls = [{"id": "delegate_" + uuid.uuid4().hex[:12], "type": "function",
                  "function": {"name": "delegate", "arguments": json.dumps({"task": task}, ensure_ascii=False)}}]
        self.messages.append({"role": "assistant", "content": "", "tool_calls": calls})
        self._checkpoint()
        interrupted = self._fill_tool_results(calls)
        result = json.loads(self.messages[-1]["content"])
        status = "interrupted" if interrupted else result.get("status", "blocked" if self._approval_denied else "error")
        summary = result.get("summary", result.get("error", ""))
        self.messages.append({"role": "assistant", "content": f"[{status}] {summary}"})
        self.last_run = {"status": status, "auto": False, "continuations": 0}
        self._checkpoint()
        return result

    def clear(self) -> None:
        self.messages[:] = [{"role": "system", "content": self.system}]
        self.client.last_request = None

    def stats(self) -> dict[str, Any]:
        totals = {}
        missing = {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
            observed = [item[field] for item in self._usage_history if item and field in item]
            totals[field] = sum(observed) if observed else None
            missing[field] = len(self._usage_history) - len(observed)
        last_usage = self._usage_history[-1] if self._usage_history else None
        return {"api_calls_total": self._total_calls,
                "http_attempts_last": getattr(self.client, "last_attempts", 0),
                "tool_calls_total": self._tool_calls_total,
                "direct_shell_commands": self._direct_shell_count, "active_skills": list(self.active_skills),
                "subagents": {"calls": self._subagent_calls_total,
                              "api_calls": self._subagent_api_calls_total,
                              "tool_calls": self._subagent_tool_calls_total},
                "usage_totals": totals, "last_request_usage": last_usage,
                "usage_history": list(self._usage_history), "usage_missing_requests": missing,
                "conversation_count": self._conversation_count,
                "conversation_message_count": len(self.messages),
                "system_chars": len(self.system) + self._last_auto_prompt_chars,
                "auto_prompt_chars": self._last_auto_prompt_chars,
                "auto_continue": self.config.auto_continue, "last_run": dict(self.last_run),
                "tool_schema_chars": len(json.dumps(self.tools, ensure_ascii=False, separators=(",", ":"))),
                "request_history_chars": list(self._request_history_chars),
                "current_history_chars": self._history_chars(),
                "context_budget": {'chars': context_chars(self.messages, self.tools),
                                   'limit': self.config.max_context_chars,
                                   'unit': 'characters, not tokens'}}

    def debug_context(self) -> dict[str, Any]:
        request = getattr(self.client, "last_request", None)
        if not isinstance(request, dict):
            request = {"messages": self.messages, "tools": self.tools}
        # A user might paste the configured key into a prompt; scrub it from debug.
        key = getattr(self.config, "api_key", "")
        def redact(value: Any) -> Any:
            if isinstance(value, str) and key:
                return value.replace(key, "[REDACTED]")
            if isinstance(value, dict):
                return {k: redact(v) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(v) for v in value]
            return value
        result = redact(request)
        return hide_write_payloads(result) if self.config.silent_writes else result
