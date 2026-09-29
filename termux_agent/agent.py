"""Tool loop and compact conversation state for Termux."""

from __future__ import annotations

import json
from typing import Any, Callable

from .client import Client
from .tools import TOOLS, execute

from .approval import needs_approval


SYSTEM = "You are a concise Termux terminal agent.\nUse tools when needed.\nInspect before modifying.\nPrefer minimal changes.\nVerify important changes before claiming success."


class Agent:
    def __init__(self, config: Any, approve: Callable[[str, dict[str, Any]], bool] | None = None,
                 event: Callable[[str, dict[str, Any]], None] | None = None, client: Any | None = None, instructions: str = ""):
        self.config = config
        self.config.validate()
        self.approve = approve
        self.event = event
        self.client = client or Client(config)
        self.base_system = SYSTEM + ("\n\n" + instructions if instructions else "")
        self.active_skills: dict[str, str] = {}
        self.system = self.base_system
        self._direct_shell_count = 0
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self.system}]
        self._total_calls = 0
        self._tool_calls_total = 0
        self._usage_history: list[dict[str, Any] | None] = []
        self._request_history_chars: list[int] = []
        self._conversation_count = 0

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

    def run(self, text: str) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("input must be a non-empty string")
        self.messages.append({"role": "user", "content": text})
        self._conversation_count += 1
        for step in range(self.config.max_steps):
            self._total_calls += 1
            try:
                answer = self.client.chat(self.messages, TOOLS)
            except KeyboardInterrupt:
                self._usage_history.append(None)
                self._request_history_chars.append(self._history_chars())
                return "LLM 请求已中断。"
            except Exception as exc:
                self._usage_history.append(None)
                self._request_history_chars.append(self._history_chars())
                return f"LLM 请求失败：{exc}"
            if not isinstance(answer, dict):
                return "LLM 响应格式错误：assistant message must be an object"
            usage = getattr(self.client, "last_usage", None)
            usage = self._clean_usage(usage)
            self._usage_history.append(usage)
            self._request_history_chars.append(self._history_chars())
            content = answer.get("content")
            reasoning = {"reasoning_content": answer["reasoning_content"]} if "reasoning_content" in answer else {}
            calls = answer.get("tool_calls")
            if not isinstance(calls, list) or not calls:
                self.messages.append({"role": "assistant", "content": content if isinstance(content, str) else "", **reasoning})
                return content if isinstance(content, str) else ""
            invalid = self._validate_calls(calls)
            if invalid:
                # Do not persist a malformed assistant tool-call turn: subsequent
                # requests must never contain dangling or invented tool_call_ids.
                self.messages.append({"role": "assistant", "content": content if isinstance(content, str) else "", **reasoning})
                return f"模型返回了格式错误的工具调用：{invalid}"
            self.messages.append({"role": "assistant", "content": content, "tool_calls": calls, **reasoning})
            if step == self.config.max_steps - 1:
                self._fill_tool_results(calls, denied=True, reason="maximum API request count reached")
                return "已达到最大模型请求次数；未执行最后一轮工具调用。"
            interrupted = self._fill_tool_results(calls)
            if interrupted:
                return "工具执行已中断；历史记录已保留为完整工具调用。"
        return "已达到最大模型请求次数。"

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
        return usage or None

    def _history_chars(self) -> int:
        return sum(len(json.dumps(m, ensure_ascii=False, separators=(",", ":"))) for m in self.messages)

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
            elif not self._approved(name, args or {}):
                result = {"error": "approval denied or unavailable"}
            else:
                self._emit("tool_start", {"name": name, "arguments": args})
                try:
                    result = execute(name, args or {}, self.config.max_output_chars)
                except KeyboardInterrupt:
                    interrupted = True
                    result = {"error": "tool execution interrupted"}
                except Exception as exc:
                    result = {"error": f"{type(exc).__name__}: {exc}"}
                self._emit("tool_result", {"name": name, "result": result})
            self.messages.append({"role": "tool", "tool_call_id": call_id,
                                  "content": json.dumps(result, ensure_ascii=False, separators=(",", ":"))})
        return interrupted

    def clear(self) -> None:
        self.messages[:] = [{"role": "system", "content": self.system}]
        self.client.last_request = None

    def stats(self) -> dict[str, Any]:
        totals = {}
        missing = {}
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            observed = [item[field] for item in self._usage_history if item and field in item]
            totals[field] = sum(observed) if observed else None
            missing[field] = len(self._usage_history) - len(observed)
        last_usage = self._usage_history[-1] if self._usage_history else None
        return {"api_calls_total": self._total_calls, "tool_calls_total": self._tool_calls_total,
                "direct_shell_commands": self._direct_shell_count, "active_skills": list(self.active_skills),
                "usage_totals": totals, "last_request_usage": last_usage,
                "usage_history": list(self._usage_history), "usage_missing_requests": missing,
                "conversation_count": self._conversation_count,
                "conversation_message_count": len(self.messages),
                "system_chars": len(self.system),
                "tool_schema_chars": len(json.dumps(TOOLS, ensure_ascii=False, separators=(",", ":"))),
                "request_history_chars": list(self._request_history_chars),
                "current_history_chars": self._history_chars()}

    def debug_context(self) -> dict[str, Any]:
        request = getattr(self.client, "last_request", None)
        if not isinstance(request, dict):
            request = {"messages": self.messages, "tools": TOOLS}
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
        return redact(request)
