"""Tool loop and compact conversation state for Termux."""

from __future__ import annotations

import json
import hashlib
from copy import deepcopy
from dataclasses import replace
import time
import uuid
from typing import Any, Callable

from .client import (Client, ClientError, ContextLengthError, EmptyResponseError,
                     IncompleteOutputError, NetworkError)
from .context import compact_task_messages, context_chars
from .token_budget import TokenCounter, estimate_text_tokens
from .tools import TOOLS, DELEGATE_TOOL, WEB_FETCH_TOOL, execute, web_fetch_available
from .output_compression import OutputCompressor, stable_result_payload

from .approval import needs_approval
from .instructions import load_system_prompt, load_prompt_file
from .display_privacy import hide_write_payloads
from .continuation import TASK_MARKERS, provider_blocked, task_status


OUTPUT_COMPRESSION_COUNTERS = (
    "shell_results", "filter_attempts", "compressed_results", "filter_failures",
    "incomplete_capture", "raw_artifacts", "artifact_bytes", "before_bytes",
    "after_bytes", "before_chars", "after_chars",
)


class TaskBudgetExhausted(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(f'task budget exhausted: {reason}')


class Agent:
    def __init__(self, config: Any, approve: Callable[[str, dict[str, Any]], bool] | None = None,
                 event: Callable[[str, dict[str, Any]], None] | None = None, client: Any | None = None, instructions: str = "",
                 base_system: str | None = None, *, allow_delegation: bool = True):
        self.config = config
        self.config.validate()
        self.approve = approve
        self.event = event
        self.checkpoint = None
        self.branch_after_compact = None
        self.archive_id = None
        self.base_system = base_system if base_system is not None else (
            load_system_prompt() + ("\n\n" + instructions if instructions else ""))
        self.client = client or Client(config)
        self._allow_delegation = allow_delegation
        self._web_fetch_enabled = config.web_fetch_enabled and web_fetch_available(config.lightpanda_binary or None)
        self.output_compressor = OutputCompressor(config)
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
        self._tool_calls_executed = 0
        self._tool_calls_denied = 0
        self._tool_calls_not_run = 0
        self._tool_calls_approval_denied = 0
        self._tool_calls_hard_limit_not_run = 0
        self._tool_write_unknown = 0
        self._http_attempts_total = 0
        self._http_retries_total = 0
        self._context_recoveries = 0
        self._task_compactions = 0
        self._rtk_shell_results = 0
        self._rtk_filter_attempts = 0
        self._rtk_compressed_results = 0
        self._rtk_filter_failures = 0
        self._rtk_incomplete_capture = 0
        self._rtk_raw_artifacts = 0
        self._rtk_artifact_bytes = 0
        self._rtk_before_bytes = 0
        self._rtk_after_bytes = 0
        self._rtk_before_chars = 0
        self._rtk_after_chars = 0
        self._usage_history: list[dict[str, Any] | None] = []
        self._request_history_chars: list[int] = []
        self._conversation_count = 0
        self._approval_denied = False
        self._last_auto_prompt_chars = 0
        self.task_state = None
        self.in_memory_archives = []
        self.incomplete_draft = None
        self.token_counter = TokenCounter(config)
        self._segment_started = 0.0
        self._task_active_before = 0.0
        self._budget_stop_reason = None
        self._no_progress_stop = False
        self._last_tool_fingerprint = None
        self._same_tool_fingerprint_count = 0
        self._segment_progress = False
        self._task_progress = False
        self._last_progress_evidence = None
        self._unsafe_write_fingerprints = self._scan_unsafe_writes(self.messages)
        self.last_run = self._idle_run()

    @staticmethod
    def _idle_run():
        return {"status": "idle", "stop_reason": "idle", "auto": False, "continuations": 0,
                "steps": 0, "message": "尚无任务。", "continue_hint": "输入任务，或用 /session load ID 恢复。"}

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

    @property
    def history_only(self):
        return getattr(self.client, 'history_only', False) is True

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

    def close(self):
        """Close owned HTTP clients and local token capability clients."""
        try:
            self.client.close()
        finally:
            self.token_counter.close()

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
        if self.history_only:
            return False
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

    @staticmethod
    def _call_fingerprint(call):
        function = call.get('function') if isinstance(call.get('function'), dict) else {}
        try:
            args = json.loads(function.get('arguments', '{}')) if isinstance(function.get('arguments'), str) else function.get('arguments', {})
        except (TypeError, ValueError):
            args = function.get('arguments')
        raw = json.dumps({'name': function.get('name'), 'arguments': args}, ensure_ascii=False,
                         sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode()).hexdigest()

    def _scan_unsafe_writes(self, messages):
        unsafe, pending = set(), {}
        for message in messages:
            if message.get('role') == 'assistant' and message.get('tool_calls'):
                pending = {call.get('id'): call for call in message['tool_calls']}
            elif message.get('role') == 'tool':
                call = pending.pop(message.get('tool_call_id'), None)
                text = message.get('content', '')
                if not call or call.get('function', {}).get('name') not in ('write_file', 'edit_file'):
                    continue
                try:
                    result = json.loads(text) if isinstance(text, str) else text
                except (TypeError, ValueError):
                    result = {}
                execution = result.get('execution_status') if isinstance(result, dict) else None
                write_status = result.get('write_status') if isinstance(result, dict) else None
                legacy_not_run = isinstance(text, str) and (
                    'maximum API request count reached' in text or 'not run:' in text
                    or 'not run because' in text or 'approval denied' in text or 'file unchanged' in text)
                if execution == 'unknown' or write_status in ('unknown', 'failed') or (
                        isinstance(text, str) and 'outcome is unknown' in text):
                    unsafe.add(self._call_fingerprint(call))
                elif isinstance(result, dict) and result.get('error') and not legacy_not_run and execution != 'not_run':
                    unsafe.add(self._call_fingerprint(call))
        return unsafe

    def _ensure_task_state(self, goal=''):
        self.task_state = {'id': uuid.uuid4().hex[:12], 'goal': goal, 'status': 'running',
                           'stop_reason': None, 'started': time.time(), 'active_seconds': 0.0,
                           'segments': 0, 'logical_calls': 0, 'max_logical_calls': self.config.max_task_calls,
                           'max_active_seconds': self.config.max_task_seconds, 'max_tokens': self.config.max_task_tokens,
                           'tokens': 0, 'token_source': 'unknown', 'token_budget_exact': True,
                           'recovery_requests': 0, 'compactions': 0, 'progress_evidence': []}
        self._approval_denied = False
        self._no_progress_stop = False
        self._last_tool_fingerprint = None
        self._same_tool_fingerprint_count = 0
        self._segment_progress = False
        self._task_progress = False
        self._last_progress_evidence = None
        self.incomplete_draft = None

    def _restore_legacy_task(self):
        starts = [i for i, m in enumerate(self.messages) if m.get('role') == 'user' and
                  not str(m.get('content', '')).startswith(('[Task memo:', '[TASK_RESUME]', '[Context archive:'))]
        start = starts[-1] if starts else 1
        self._ensure_task_state(self.messages[start].get('content', '') if start < len(self.messages) else '')
        self.task_state['logical_calls'] = sum(m.get('role') == 'assistant' for m in self.messages[start + 1:])
        self.task_state['status'] = self.last_run.get('status', 'limit')
        self.task_state['stop_reason'] = self.last_run.get('stop_reason', 'step_limit')

    def _task_budget(self):
        state = self.task_state or {}
        active = float(state.get('active_seconds', 0))
        if self._segment_started:
            active += time.monotonic() - self._segment_started
        return {'logical_calls': state.get('logical_calls', 0), 'max_logical_calls': state.get('max_logical_calls', self.config.max_task_calls),
                'active_seconds': round(active, 3), 'max_active_seconds': state.get('max_active_seconds', self.config.max_task_seconds),
                'tokens': state.get('tokens', 0), 'max_tokens': state.get('max_tokens', self.config.max_task_tokens),
                'token_source': state.get('token_source', 'unknown'),
                'token_budget_exact': state.get('token_budget_exact', True),
                'recovery_requests': state.get('recovery_requests', 0),
                'compactions': state.get('compactions', 0)}

    def _budget_stop(self):
        budget = self._task_budget()
        if budget['logical_calls'] >= budget['max_logical_calls']:
            return 'task_call_budget'
        if budget['active_seconds'] >= budget['max_active_seconds']:
            return 'time_budget'
        if budget['max_tokens'] and budget['tokens'] >= budget['max_tokens']:
            return 'token_budget'
        return None

    def _finish(self, status, stop_reason, value, can_continue=True):
        if not self.task_state:
            self._ensure_task_state()
        state = self.task_state
        if self._segment_started:
            state['active_seconds'] += time.monotonic() - self._segment_started
            self._segment_started = 0.0
        state.update(status=status, stop_reason=stop_reason)
        self.last_run.update(status=status, stop_reason=stop_reason, message=value,
                             can_continue=can_continue, task_budget=self._task_budget(),
                             continue_hint='/continue 开始同一任务的新受控段。' if can_continue else
                                           '此结果不能自动续跑；请修正条件后开始新任务。')
        self._checkpoint()
        return value

    def _request_messages(self, rules='', messages=None):
        source = self.messages if messages is None else messages
        return ([{'role': 'system', 'content': self.system + '\n\n' + rules}, *source[1:]]
                if rules else source)

    def _context_budget(self, request_messages):
        chars = context_chars(request_messages, self.tools)
        token = self.token_counter.measure(request_messages, self.tools,
                                           allow_network=not self.history_only)
        reserve = max(self.config.output_reserve_tokens,
                      getattr(self.config, 'max_output_tokens', 0) or 0)
        window = token.get('context_window_tokens')
        # Keep measured metadata and the headroom check on the same effective reserve.
        if isinstance(token, dict):
            token['output_reserve_tokens'] = reserve
        exceeded = bool(window and token.get('prompt_tokens') is not None and token['prompt_tokens'] + reserve > window)
        return chars, token, exceeded

    def _compact_for_context(self, force=False, messages=None):
        source = self.messages if messages is None else messages
        chars, token, exceeded = self._context_budget(self._request_messages(messages=source))
        ratio = chars / self.config.max_context_chars
        window = token.get('context_window_tokens')
        if window and token.get('prompt_tokens') is not None:
            ratio = max(ratio, token['prompt_tokens'] / max(1, window - (token.get('output_reserve_tokens') or 0)))
        if not force and (not self.config.auto_compact_task or ratio < self.config.auto_compact_threshold):
            return False
        archive_id = self.archive_id or 'memory-' + uuid.uuid4().hex[:12]
        result, details = compact_task_messages(source, self.config.auto_compact_keep_groups, archive_id)
        if details.get('skipped_no_reduction') or (not details['omitted_groups'] and not details['reduced_calls']):
            return False
        persistent = self.branch_after_compact is not None and (
            self.branch_after_compact(archive_id, result, details) is not False)
        if not persistent:
            if not any(item.get('id') == archive_id for item in self.in_memory_archives):
                self.in_memory_archives.append({'id': archive_id,
                                                'messages': __import__('copy').deepcopy(source)})
        self.messages[:] = result
        self.task_state['compactions'] += 1
        self._task_compactions += 1
        self._emit('task_compacted', {'archive': archive_id, 'persistent': persistent, **details})
        self._checkpoint()
        return True

    def continue_task(self, steps=None, auto_continue=None):
        if self.history_only:
            raise ValueError('历史查看模式不能继续任务；请用 /api NAME 新建可用会话。')
        if not self.task_state:
            self._restore_legacy_task()
        if self.last_run.get('can_continue') is False:
            raise ValueError('上次任务明确不可自动续跑；请修正条件后开始新任务。')
        if steps is not None and (isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 256):
            raise ValueError('continue steps must be between 1 and 256')
        return self._run_segment('[TASK_RESUME]\n' + load_prompt_file('task_resume.txt'),
                                 automatic=self.config.auto_continue if auto_continue is None else auto_continue,
                                 resume=True, segment_steps=steps or self.config.max_steps)

    def run(self, text: str, auto_continue: bool | None = None) -> str:
        if self.history_only:
            raise ValueError('历史查看模式不能调用模型；请用 /api NAME 新建可用会话。')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('input must be a non-empty string')
        self._ensure_task_state(text)
        return self._run_segment(text, automatic=self.config.auto_continue if auto_continue is None else auto_continue,
                                 resume=False, segment_steps=self.config.max_steps)

    def _record_model_request(self, request, answer=None):
        usage = self._clean_usage(getattr(self.client, 'last_usage', None))
        self._usage_history.append(usage)
        self._request_history_chars.append(self._history_chars(request))
        attempts = max(0, int(getattr(self.client, 'last_attempts', 0) or 0))
        self._http_attempts_total += attempts
        self._http_retries_total += max(0, attempts - 1)
        total = usage.get('total_tokens') if usage else None
        if total is None and usage and usage.get('prompt_tokens') is not None and usage.get('completion_tokens') is not None:
            total = usage['prompt_tokens'] + usage['completion_tokens']
        if total is not None:
            self.task_state['tokens'] += total
            self.task_state['token_source'] = 'server'
        else:
            request_tokens = estimate_text_tokens(json.dumps(request, ensure_ascii=False, separators=(',', ':')))
            answer_tokens = estimate_text_tokens(json.dumps(answer or {}, ensure_ascii=False, separators=(',', ':')))
            output_reserve = max(self.config.output_reserve_tokens, self.config.max_output_tokens)
            self.task_state['tokens'] += request_tokens + answer_tokens + output_reserve
            self.task_state['token_source'] = 'conservative_estimate'
            self.task_state['token_budget_exact'] = False

    def _send_model_request(self, request, *, recovery=False):
        stop = self._budget_stop()
        if stop:
            raise TaskBudgetExhausted(stop)
        self._total_calls += 1
        self.task_state['logical_calls'] += 1
        self._emit('request_start', {'logical_call': self.task_state['logical_calls'],
                                    'max_logical_calls': self.task_state['max_logical_calls'],
                                    'recovery': recovery})
        try:
            answer = self.client.chat(request, self.tools)
        except BaseException as exc:
            self._record_model_request(request, getattr(exc, 'assistant_message', None))
            raise
        self._record_model_request(request, answer if isinstance(answer, dict) else None)
        return answer

    def _run_segment(self, text, *, automatic, resume, segment_steps):
        rules = load_prompt_file('task_prompt.txt') if automatic else ''
        nudge = load_prompt_file('task_continue.txt') if automatic else ''
        self._last_auto_prompt_chars = len(rules) + 2 if rules else 0
        self._segment_progress = False
        self.task_state['segments'] += 1
        self._segment_started = time.monotonic()
        self.last_run = {'status': 'running', 'stop_reason': 'running', 'auto': automatic,
                         'continuations': 0, 'steps': 0, 'segment': self.task_state['segments'],
                         'task_id': self.task_state['id'], 'message': '任务运行中。'}
        if hasattr(self.client, 'set_control_markers'):
            self.client.set_control_markers(TASK_MARKERS if automatic else ())
        if resume:
            prospective = [*__import__('copy').deepcopy(self.messages), {'role': 'user', 'content': text}]
            compacted = self._compact_for_context(messages=prospective)
            if not compacted:
                self.messages.append({'role': 'user', 'content': text})
        else:
            self.messages.append({'role': 'user', 'content': text})
        if not resume:
            self._conversation_count += 1
        self._checkpoint()
        pauses = 0
        warned = False
        for step in range(segment_steps):
            self.last_run['steps'] = step + 1
            stop = self._budget_stop()
            if stop:
                reason = {'time_budget': 'time_budget', 'token_budget': 'token_budget'}.get(stop, 'task_call_budget')
                return self._finish('limit', reason, '同一任务的硬预算已用完；未执行新的调用。')
            self._compact_for_context()
            request = self._request_messages(rules)
            chars, token, exceeded = self._context_budget(request)
            self.last_run['context'] = {'chars': chars, 'tokens': token}
            if chars > self.config.max_context_chars or exceeded:
                return self._finish('context_limit', 'context_limit', '上下文预算不足；请用 /compact task、/compact N 或 /clear。')
            if not warned and chars >= self.config.max_context_chars * 0.8:
                self._emit('context_warning', {'chars': chars, 'limit': self.config.max_context_chars})
                warned = True
            self._emit('step_start', {'step': step + 1, 'segment': self.task_state['segments'],
                                     'task_logical_calls': self.task_state['logical_calls'] + 1,
                                     'max_task_logical_calls': self.config.max_task_calls})
            try:
                answer = self._chat_with_recovery(request, rules)
            except KeyboardInterrupt:
                return self._finish('interrupted', 'interrupted', 'LLM 请求已中断。')
            except TaskBudgetExhausted as exc:
                reason = {'time_budget': 'time_budget', 'token_budget': 'token_budget'}.get(
                    exc.reason, 'task_call_budget')
                return self._finish('limit', reason, '同一任务的硬预算已用完；未执行新的调用。')
            except Exception as exc:
                if isinstance(exc, IncompleteOutputError) or (
                        isinstance(exc, EmptyResponseError) and getattr(exc, 'length', False)):
                    self.incomplete_draft = deepcopy(getattr(exc, 'assistant_message', None))
                elif isinstance(exc, EmptyResponseError):
                    draft = getattr(exc, 'assistant_message', None)
                    if isinstance(draft, dict):
                        self.messages.append({k: v for k, v in draft.items()
                                              if k not in ('tool_calls', '_incomplete_tool_calls', '_responses_output',
                                                           '_incomplete_output')}
                                             | {'role': 'assistant'})
                if isinstance(exc, ContextLengthError):
                    return self._finish('context_limit', 'context_limit', f'LLM 请求失败：{exc}', False)
                if isinstance(exc, IncompleteOutputError):
                    reason = 'context_limit' if getattr(exc, 'context_limited', False) else 'output_limit'
                    return self._finish('incomplete', reason, f'LLM 请求失败：输出不完整：{exc}', False)
                if isinstance(exc, EmptyResponseError):
                    reason = 'output_limit' if getattr(exc, 'length', False) else 'provider_error'
                    return self._finish('empty_response', reason, f'LLM 请求失败：{exc}', False)
                return self._finish('error', 'network_error' if isinstance(exc, NetworkError) else 'provider_error', f'LLM 请求失败：{exc}')
            if not isinstance(answer, dict):
                return self._finish('error', 'provider_error', 'LLM 响应格式错误：assistant message must be an object')
            if answer.get('_incomplete_tool_calls') or answer.get('_incomplete_output'):
                self.incomplete_draft = deepcopy(answer)
                reason = 'context_limit' if getattr(self.client, 'context_limited', False) else 'output_limit'
                return self._finish('incomplete', reason, '模型输出未完成；草稿已保存，未执行其中工具。', False)
            content = answer.get('content') if isinstance(answer.get('content'), str) else ''
            marker = None
            if automatic:
                content, marker = task_status(content)
            keep = {k: answer[k] for k in ('reasoning_content', '_responses_output', '_incomplete_output') if k in answer}
            if provider_blocked(answer, getattr(self.client, 'last_finish_reason', None)):
                self.messages.append({'role': 'assistant', 'content': content, **keep}); self.last_run['blocked_by'] = 'provider'
                return self._finish('blocked', 'provider_denied', content or '模型服务拒绝了请求。', False)
            calls = answer.get('tool_calls') if isinstance(answer.get('tool_calls'), list) else []
            if not calls:
                self.messages.append({'role': 'assistant', 'content': content, **keep})
                if not automatic:
                    return self._finish('complete', 'complete', content)
                if self._approval_denied:
                    return self._finish('blocked', 'approval_denied', '任务因审批拒绝停止。', False)
                if marker == 'done': return self._finish('complete', 'complete', content)
                if marker == 'blocked': return self._finish('blocked', 'user_blocked', content or '任务需要用户输入。')
                if pauses >= self.config.max_continuations:
                    return self._finish('stalled', 'stalled', '自动任务已停止：连续多轮未执行工具或确认完成。')
                pauses += 1; self.last_run['continuations'] += 1
                self.messages.append({'role': 'user', 'content': nudge}); self._checkpoint(); continue
            invalid = self._validate_calls(calls)
            if invalid:
                self.messages.append({'role': 'assistant', 'content': content, **keep})
                return self._finish('error', 'provider_error', f'模型返回了格式错误的工具调用：{invalid}')
            self.messages.append({'role': 'assistant', 'content': content, 'tool_calls': calls, **keep}); self._checkpoint()
            if self._approval_denied:
                self._fill_tool_results(calls, disposition='denied', reason='task stopped after approval denial')
                return self._finish('blocked', 'approval_denied', '任务因审批拒绝停止；新工具未执行。', False)
            stop = self._budget_stop()
            if stop:
                self._fill_tool_results(calls, disposition='not_run', reason='task hard budget reached')
                reason = {'time_budget': 'time_budget', 'token_budget': 'token_budget'}.get(stop, 'task_call_budget')
                return self._finish('limit', reason, '任务硬预算已用完；最后一轮工具未执行。')
            may_run_last = (self.config.auto_extend_segments
                            and self._can_extend_segment(require_progress=False))
            if step == segment_steps - 1 and not may_run_last:
                self._fill_tool_results(calls, disposition='not_run', reason='maximum API request count reached')
                return self._finish('limit', 'step_limit', '已达到本段最大模型请求次数；最后一轮工具未执行。')
            interrupted = self._fill_tool_results(calls)
            pauses = 0
            if interrupted: return self._finish('interrupted', 'interrupted', '工具执行已中断；结果配对已保留。')
            if self._approval_denied and len(calls) > 1:
                return self._finish('blocked', 'approval_denied',
                                    '任务因审批拒绝停止；其余调用未执行。', False)
            if self._no_progress_stop:
                return self._finish('stalled', 'no_progress', '连续相同调用、参数和结果达到阈值；结果已保留。')
        if self.config.auto_extend_segments and self._can_extend_segment():
            self.task_state['active_seconds'] += time.monotonic() - self._segment_started
            self._segment_started = 0.0
            self._emit('segment_extension', self._task_budget())
            remaining = self.config.max_task_calls - self.task_state['logical_calls']
            return self._run_segment('[TASK_RESUME]\n' + load_prompt_file('task_resume.txt'), automatic=automatic,
                                     resume=True, segment_steps=max(1, min(segment_steps, remaining)))
        return self._finish('limit', 'step_limit', '已达到最大模型请求次数；同一任务可用 /continue 开始新的受控段。')

    def _can_extend_segment(self, require_progress=True):
        if self._budget_stop() or self._approval_denied or self._no_progress_stop:
            return False
        return self._segment_progress if require_progress else True

    def _chat_with_recovery(self, request, rules):
        try:
            return self._send_model_request(request)
        except ContextLengthError as exc:
            if (not getattr(exc, 'pre_accept', False) or not self.config.context_recovery
                    or self.task_state['recovery_requests']):
                raise
            if self._budget_stop():
                raise TaskBudgetExhausted(self._budget_stop()) from exc
            if not self._compact_for_context(force=True):
                raise
            self.task_state['recovery_requests'] += 1
            self._context_recoveries += 1
            return self._send_model_request(self._request_messages(rules), recovery=True)

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

    def _fill_tool_results(self, calls: list[Any], disposition: str | None = None,
                           reason: str = "", *, denied: bool = False) -> bool:
        """Append one tool result per call, even if one call is malformed/interrupted."""
        interrupted = False
        disposition = disposition or ('denied' if denied else None)
        if disposition not in (None, 'denied', 'not_run'):
            raise ValueError('invalid tool disposition')
        # A denied call stops the remainder of this model batch.  Keep the first
        # denial as evidence, then record every later call as explicitly not_run
        # without asking for approval or executing anything.
        prior_denial = self._approval_denied
        batch_denied = prior_denial
        for index, call in enumerate(calls):
            call = call if isinstance(call, dict) else {}
            call_id = call.get("id", f"invalid-{index}")
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = function.get("name") if isinstance(function.get("name"), str) else ""
            args, parse_error = self._tool_args(function.get("arguments", "{}"))
            self._tool_calls_total += 1
            write_call = name in ("write_file", "edit_file")
            effective_disposition = disposition
            no_progress_not_run = False
            if effective_disposition == "denied" and (prior_denial or batch_denied):
                effective_disposition = "not_run"
            elif effective_disposition is None and batch_denied:
                effective_disposition = "not_run"
            elif effective_disposition is None and self._no_progress_stop:
                effective_disposition = "not_run"
                no_progress_not_run = True
            if effective_disposition == "denied":
                result: Any = {"error": reason or "approval denied or unavailable",
                               "execution_status": "denied", "outcome_status": "denied"}
                self._tool_calls_denied += 1
                self._tool_calls_approval_denied += 1
                self._approval_denied = True
                batch_denied = True
            elif effective_disposition == "not_run":
                result = {"error": reason or ("not run: no-progress threshold reached"
                                              if no_progress_not_run else "not run"),
                          "execution_status": "not_run",
                          "outcome_status": "not_run"}
                self._tool_calls_not_run += 1
                if disposition == "not_run" and not prior_denial and not batch_denied:
                    self._tool_calls_hard_limit_not_run += 1
            elif interrupted:
                result = {"error": "not run because the preceding tool was interrupted",
                          "execution_status": "not_run", "outcome_status": "not_run"}
                self._tool_calls_not_run += 1
            elif parse_error:
                result = {"error": parse_error, "execution_status": "not_run", "outcome_status": "not_run"}
                self._tool_calls_not_run += 1
            elif not name:
                result = {"error": "tool name is missing", "execution_status": "not_run", "outcome_status": "not_run"}
                self._tool_calls_not_run += 1
            elif write_call and self._call_fingerprint(call) in self._unsafe_write_fingerprints:
                result = {"error": "not run: an identical write previously failed or its outcome is unknown",
                          "execution_status": "not_run", "outcome_status": "not_run"}
                self._tool_calls_not_run += 1
            elif name == "web_fetch" and not self._web_fetch_enabled:
                result = {"error": "web_fetch is disabled or lightpanda is unavailable",
                          "execution_status": "not_run", "outcome_status": "not_run"}
                self._tool_calls_not_run += 1
            elif not self._approved(name, args or {}):
                self._approval_denied = True
                batch_denied = True
                result = {"error": "approval denied or unavailable", "execution_status": "denied",
                          "outcome_status": "denied"}
                self._tool_calls_denied += 1
                self._tool_calls_approval_denied += 1
            else:
                self._tool_calls_executed += 1
                self._emit("tool_start", {"name": name, "arguments": args})
                try:
                    if name == "delegate":
                        result = self._delegate(args or {})
                    elif name == "web_fetch":
                        result = execute(name, args or {}, self.config.max_output_chars,
                                         lightpanda_binary=self.config.lightpanda_binary or None)
                    elif name == "shell":
                        self.output_compressor.last_stats = None
                        try:
                            if self.output_compressor.enabled:
                                result = execute(name, args or {}, self.config.max_output_chars,
                                                 output_compressor=self.output_compressor)
                            else:
                                result = execute(name, args or {}, self.config.max_output_chars)
                        finally:
                            self._record_output_compression(
                                getattr(self.output_compressor, "last_stats", None))
                            self.output_compressor.last_stats = None
                    else:
                        result = execute(name, args or {}, self.config.max_output_chars)
                except KeyboardInterrupt:
                    interrupted = True
                    result = {"error": "tool execution interrupted", "execution_status": "unknown",
                              "outcome_status": "unknown"}
                except Exception as exc:
                    result = {"error": f"{type(exc).__name__}: {exc}", "execution_status": "unknown",
                              "outcome_status": "unknown"}
                if not isinstance(result, dict):
                    result = {"error": "tool returned a non-object result", "execution_status": "unknown",
                              "outcome_status": "unknown"}
                if write_call:
                    error = result.get("error")
                    if not error and result.get("verified") is True:
                        result.update(execution_status="executed", write_status="completed",
                                      outcome_status="completed")
                    elif isinstance(error, str) and "file unchanged" in error:
                        result.update(execution_status="not_run", write_status="not_run",
                                      outcome_status="not_run")
                    else:
                        result.update(execution_status="unknown", write_status="unknown",
                                      outcome_status="unknown")
                        self._unsafe_write_fingerprints.add(self._call_fingerprint(call))
                        self._tool_write_unknown += 1
                elif name == "shell":
                    failed = bool(result.get("timed_out") or result.get("exit_code") not in (None, 0))
                    result.setdefault("execution_status", "executed")
                    result.setdefault("outcome_status", "failed" if failed else "completed")
                else:
                    failed = bool(result.get("error"))
                    result.setdefault("execution_status", "executed")
                    result.setdefault("outcome_status", "failed" if failed else "completed")
                self._emit("tool_result", {"name": name, "result": result})
            self.messages.append({"role": "tool", "tool_call_id": call_id,
                                  "content": json.dumps(result, ensure_ascii=False, separators=(",", ":"))})
            fingerprint = hashlib.sha256(json.dumps([self._call_fingerprint(call), stable_result_payload(result)], sort_keys=True,
                                                     ensure_ascii=False).encode()).hexdigest()
            if self.config.no_progress_detection and fingerprint == self._last_tool_fingerprint:
                self._same_tool_fingerprint_count += 1
                if self._same_tool_fingerprint_count >= self.config.no_progress_threshold:
                    self._no_progress_stop = True
            else:
                self._same_tool_fingerprint_count = 1
            self._last_tool_fingerprint = fingerprint
            if result.get("execution_status") == "executed" and result.get("outcome_status") == "completed":
                evidence = {"tool": name, "call_fingerprint": self._call_fingerprint(call),
                            "result_fingerprint": fingerprint}
                self._segment_progress = True
                self._task_progress = True
                self._last_progress_evidence = evidence
                state = self.task_state if isinstance(self.task_state, dict) else None
                progress = state.setdefault("progress_evidence", []) if state is not None else []
                if isinstance(progress, list):
                    progress.append(evidence)
                    del progress[:-32]
            self._checkpoint()
        return interrupted

    @staticmethod
    def _compression_count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def _record_output_compression(self, metadata):
        if not self.output_compressor.enabled or not isinstance(metadata, dict):
            return
        self._rtk_shell_results += 1
        if metadata.get("filter_attempted") is True:
            self._rtk_filter_attempts += 1
        applied = metadata.get("applied") is True
        self._rtk_compressed_results += int(applied)
        self._rtk_filter_failures += int(metadata.get("status") == "filter_failed")
        self._rtk_incomplete_capture += int(metadata.get("raw_capture_complete") is False)
        self._rtk_raw_artifacts += int(metadata.get("raw_available") is True)
        artifact = metadata.get("raw_artifact")
        self._rtk_artifact_bytes += self._compression_count(
            artifact.get("bytes", 0)) if isinstance(artifact, dict) else 0
        # Only an accepted reduction represents saved output. These values come
        # from last_stats as final serialized sizes, including metadata overhead.
        if applied:
            self._rtk_before_bytes += self._compression_count(metadata.get("before_bytes"))
            self._rtk_after_bytes += self._compression_count(metadata.get("after_bytes"))
            self._rtk_before_chars += self._compression_count(metadata.get("before_chars"))
            self._rtk_after_chars += self._compression_count(metadata.get("after_chars"))

    def _merge_output_compression(self, values):
        for field in OUTPUT_COMPRESSION_COUNTERS:
            value = values.get("_rtk_" + field, values.get(field, 0)) if isinstance(values, dict) else 0
            setattr(self, "_rtk_" + field,
                    getattr(self, "_rtk_" + field) + self._compression_count(value))

    def _primitive_output_compression(self):
        return {"_rtk_" + field: getattr(self, "_rtk_" + field)
                for field in OUTPUT_COMPRESSION_COUNTERS}

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
        stats_snapshot = None
        compression_merged = False
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
            stats_snapshot = child.stats()
            return {"id": identifier, "status": status,
                    "summary": summary[:cap] + ("\n[TRUNCATED]" if truncated else ""),
                    "api_calls": child._total_calls, "tool_calls": child._tool_calls_total,
                    "usage": stats_snapshot["usage_totals"]}
        finally:
            if child is not None:
                if not compression_merged:
                    compression_merged = True
                    self._merge_output_compression(child._primitive_output_compression())
                self._subagent_api_calls_total += child._total_calls
                self._subagent_tool_calls_total += child._tool_calls_total
                child.close()
            else:
                child_client.close()
            self._emit("subagent_end", {"id": identifier, "status": status})

    def delegate(self, task):
        """Explicit /sub command; record a complete tool turn like model delegation."""
        if self.history_only:
            raise ValueError('历史查看模式不能委派任务；请用 /api NAME 新建可用会话。')
        if not isinstance(task, str) or not task.strip() or len(task) > 16000:
            raise ValueError("task must contain 1–16000 characters")
        if not isinstance(self.task_state, dict):
            self._ensure_task_state('/sub ' + task)
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
        stop_reason = {'blocked': 'approval_denied' if self._approval_denied else 'user_blocked',
                       'complete': 'complete', 'interrupted': 'interrupted'}.get(status, status)
        self.last_run = {"status": status, "stop_reason": stop_reason, "auto": False,
                         "continuations": 0, "message": f"[{status}] {summary}",
                         "can_continue": status not in ('blocked', 'interrupted')}
        if isinstance(self.task_state, dict):
            self.task_state.update(status=status, stop_reason=stop_reason)
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
        compression = {
            "enabled": self.config.rtk_enabled,
            "shell_results": self._rtk_shell_results,
            "filter_attempts": self._rtk_filter_attempts,
            "compressed_results": self._rtk_compressed_results,
            "filter_failures": self._rtk_filter_failures,
            "incomplete_capture": self._rtk_incomplete_capture,
            "raw_artifacts": self._rtk_raw_artifacts,
            "artifact_bytes": self._rtk_artifact_bytes,
            "before_bytes": self._rtk_before_bytes,
            "after_bytes": self._rtk_after_bytes,
            "bytes_saved": max(0, self._rtk_before_bytes - self._rtk_after_bytes),
            "before_chars": self._rtk_before_chars,
            "after_chars": self._rtk_after_chars,
            "chars_saved": max(0, self._rtk_before_chars - self._rtk_after_chars),
            "unit": "UTF-8 bytes and characters, not actual tokenizer or provider tokens",
            "attribution": "applied final serialized reductions only, including metadata overhead; legacy clipping is excluded",
            "scope": "this agent plus delegated primitive counters merged once in finally, including interrupted children",
        }
        return {"api_calls_total": self._total_calls,
                "model_logical_calls_total": self._total_calls,
                "http_attempts_last": getattr(self.client, "last_attempts", 0),
                "http_attempts_total": self._http_attempts_total,
                "http_retries_total": self._http_retries_total,
                "tool_calls_total": self._tool_calls_total,
                "tool_calls_attempted": self._tool_calls_total,
                "tool_calls_executed": self._tool_calls_executed,
                "tool_calls_denied": self._tool_calls_denied,
                "tool_calls_not_run": self._tool_calls_not_run,
                "direct_shell_commands": self._direct_shell_count, "active_skills": list(self.active_skills),
                "output_compression": compression,
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
                "task_state": deepcopy(self.task_state),
                "stop": {"reason": self.last_run.get("stop_reason"),
                         "stop_reason": self.last_run.get("stop_reason"),
                         "status": self.last_run.get("status"),
                         "message": self.last_run.get("message"),
                         "continue": self.last_run.get("continue_hint"),
                         "step": self.last_run.get("steps", 0),
                         "segment": self.last_run.get("segment", self.task_state.get("segments", 0)
                                                       if isinstance(self.task_state, dict) else 0),
                         "task_budget": self._task_budget()},
                "tool_schema_chars": len(json.dumps(self.tools, ensure_ascii=False, separators=(",", ":"))),
                "request_history_chars": list(self._request_history_chars),
                "current_history_chars": self._history_chars(),
                "context_budget": {'chars': context_chars(self.messages, self.tools),
                                   'limit': self.config.max_context_chars,
                                   'tokens': self.token_counter.measure(self.messages, self.tools,
                                                                        allow_network=not self.history_only),
                                   'unit': 'characters fallback plus labeled token estimate/server count'}}

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
