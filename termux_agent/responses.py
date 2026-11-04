"""Responses decoding, loaded only for profiles using that wire protocol."""
from copy import deepcopy
import json
from .streaming import iter_sse_data, limited_draft


def _failure(value, label, client=None, event_type=None):
    from .client import (ProviderError, ContextLengthError, RateLimitError,
                         upstream_error_fields, upstream_error_text, is_context_error)
    from .retry import rate_limit_error
    error = upstream_error_fields(value)
    code = error.get('code')
    if is_context_error(value):
        return ContextLengthError('上下文超过模型窗口；请使用 /compact task、/compact N 或 /clear。',
                                  pre_accept=False)

    api_key = getattr(getattr(client, 'config', None), 'api_key', '')
    parts = [upstream_error_text(code, api_key, 80),
             upstream_error_text(error.get('message', ''), api_key, 2000)]
    detail = ': '.join(part for part in parts if part)
    if not detail and isinstance(event_type, str):
        # Some gateways send a bare failure event; say so rather than leaving an empty label.
        detail = upstream_error_text(f'上游未提供错误详情，事件 {event_type}', api_key, 120)
    if rate_limit_error(value):
        return RateLimitError(f'{label}（{detail}）' if detail else label, pre_accept=False)
    return ProviderError(f'{label}（{detail}）' if detail else label, pre_accept=False, transient=False)


def _decode_output(output, *, incomplete=False):
    from .client import IncompleteOutputError, ProviderError
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        raise ProviderError('Responses output must be an array of items')
    content, reasoning, calls, refusals, saved = [], [], [], [], []
    for item in output:
        kind = item.get('type')
        if kind in ('message', 'reasoning'):
            saved.append(deepcopy(item))
            parts = item.get('content') or []
            if not isinstance(parts, list):
                raise ProviderError('Invalid Responses content parts')
            if kind == 'reasoning':
                parts = parts + (item.get('summary') or [])
            for part in parts:
                if isinstance(part, str):
                    (reasoning if kind == 'reasoning' else content).append(part)
                elif isinstance(part, dict):
                    text = part.get('text')
                    if isinstance(text, str):
                        (reasoning if kind == 'reasoning' else content).append(text)
                    if part.get('type') == 'refusal' and isinstance(part.get('refusal'), str):
                        refusals.append(part['refusal'])
        elif kind == 'function_call':
            saved.append(deepcopy(item))
            calls.append({'id': item.get('call_id') or item.get('id', ''), 'type': 'function',
                          'function': {'name': item.get('name', ''), 'arguments': item.get('arguments', '{}')}})
    answer = {'role': 'assistant', 'content': ''.join(content), '_responses_output': saved}
    if reasoning:
        answer['reasoning_content'] = ''.join(reasoning)
    if refusals:
        answer['refusal'] = ''.join(refusals)
    if incomplete:
        if calls:
            answer['_incomplete_tool_calls'] = calls
    elif calls:
        answer['tool_calls'] = calls
    return answer


def message(client, result):
    from .client import IncompleteOutputError
    usage = result.get('usage')
    if isinstance(usage, dict):
        client.last_usage = dict(usage)
        for source, target in (('input_tokens', 'prompt_tokens'), ('output_tokens', 'completion_tokens'),
                               ('output_tokens_details', 'completion_tokens_details')):
            if source in usage:
                client.last_usage[target] = usage[source]
    status = result.get('status', 'completed')
    if result.get('error') or status == 'failed':
        raise _failure(result, 'Responses 请求失败', client=client)
    if status != 'completed':
        details = result.get('incomplete_details') or {}
        reason = details.get('reason') if isinstance(details, dict) else None
        client.last_finish_reason = 'length' if reason in ('max_output_tokens', 'context_shift', 'truncated') else None
        client.context_limited = reason in ('context_shift', 'truncated')
        answer = _decode_output(result.get('output', []), incomplete=True)
        text = ('Responses 输出在达到长度上限时截断；正文或工具参数只是未完成草稿，其中工具调用不会执行。'
                if client.last_finish_reason == 'length'
                else 'Responses 响应未完成；正文或工具参数只是草稿，工具调用不会执行。')
        raise IncompleteOutputError(text, answer, context_limited=client.context_limited)
    answer = _decode_output(result.get('output', []))
    client.last_finish_reason = 'content_filter' if answer.get('refusal') else 'stop'
    return answer


def read_sse(client, response):
    from .client import ClientError
    items, content, reasoning, arguments = {}, [], [], {}
    terminal = None

    def consume(data):
        nonlocal terminal
        if data.strip() in ('[DONE]', 'null', ''):
            return
        try:
            event = json.loads(data)
        except ValueError:
            raise ClientError('LLM endpoint returned malformed SSE JSON') from None
        if not isinstance(event, dict):
            return
        kind = event.get('type', '')
        if kind in ('error', 'response.failed'):
            raise _failure(event.get('response', event), 'Responses 流式请求失败', client=client, event_type=kind)
        if kind in ('response.completed', 'response.incomplete'):
            terminal = event.get('response')
            if not isinstance(terminal, dict):
                raise ClientError('Invalid Responses terminal event')
            terminal = dict(terminal)
            terminal.setdefault('status', 'completed' if kind == 'response.completed' else 'incomplete')
            return
        index = event.get('output_index', 0)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ClientError('Invalid Responses output index')
        if kind in ('response.output_item.added', 'response.output_item.done'):
            item = event.get('item')
            if not isinstance(item, dict):
                raise ClientError('Invalid Responses output item')
            items[index] = deepcopy(item)
            if item.get('type') == 'function_call':
                value = item.get('arguments', '')
                if not isinstance(value, str):
                    raise ClientError('Invalid Responses function arguments')
                if 'arguments' in item or index not in arguments:
                    arguments[index] = [value]
        elif kind == 'response.function_call_arguments.delta':
            delta = event.get('delta')
            if index not in items or items[index].get('type') != 'function_call' or not isinstance(delta, str):
                raise ClientError('Responses function argument delta has no matching call')
            arguments.setdefault(index, []).append(delta)
        elif kind == 'response.function_call_arguments.done':
            if index not in items or not isinstance(event.get('arguments'), str):
                raise ClientError('Invalid Responses completed arguments')
            arguments[index] = [event['arguments']]
        elif kind == 'response.output_text.delta' or kind in (
                'response.reasoning.delta', 'response.reasoning_text.delta', 'response.reasoning_summary_text.delta'):
            delta = event.get('delta')
            if not isinstance(delta, str):
                raise ClientError('Invalid Responses text delta')
            target = content if kind == 'response.output_text.delta' else reasoning
            target.append(delta)
            client._emit('content' if target is content else 'reasoning', delta)

    def collected_output():
        result = []
        for index in sorted(items):
            item = dict(items[index])
            if item.get('type') == 'function_call':
                item['arguments'] = ''.join(arguments.get(index, [item.get('arguments', '')]))
            result.append(item)
        return result

    def draft():
        calls = [{'id': item.get('call_id') or item.get('id', ''), 'function': {
            'name': item.get('name', ''), 'arguments': item.get('arguments', '')}}
            for item in collected_output() if item.get('type') == 'function_call']
        return limited_draft(''.join(content), ''.join(reasoning), calls)

    try:
        for data in iter_sse_data(response, client.config.max_response_bytes):
            consume(data)
    except Exception as exc:
        exc.assistant_message = draft()
        raise
    if terminal is None:
        error = ClientError('Responses 流在完成事件前结束；未执行其中的工具调用。')
        error.assistant_message = draft()
        raise error
    if 'output' not in terminal:
        output = collected_output()
        if content and not any(i.get('type') == 'message' and i.get('content') for i in output):
            output = [i for i in output if i.get('type') != 'message']
            output.append({'type': 'message', 'role': 'assistant',
                           'content': [{'type': 'output_text', 'text': ''.join(content)}]})
        terminal['output'] = output
    try:
        answer = message(client, terminal)
    except ClientError as exc:
        if not isinstance(getattr(exc, 'assistant_message', None), dict):
            exc.assistant_message = draft()
        raise
    if reasoning and not answer.get('reasoning_content'):
        answer['reasoning_content'] = ''.join(reasoning)
    emitted = ''.join(content)
    if emitted and answer['content'].startswith(emitted) and len(answer['content']) > len(emitted):
        client._emit('content', answer['content'][len(emitted):])
    if client._marker_filter is not None:
        client._emit_visible('content', client._marker_filter.feed('', final=True))
    return answer
