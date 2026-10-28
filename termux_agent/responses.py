"""Responses decoding, loaded only for profiles using that wire protocol."""
from copy import deepcopy
import json


def _failure(value, label, client=None):
    from .client import ProviderError, ContextLengthError, upstream_error_fields, upstream_error_text
    error = upstream_error_fields(value)
    code = error.get('code')
    if code in ('context_length_exceeded', 'context_window_exceeded', 'prompt_too_long',
                'context_shift', 'context_shift_error'):
        return ContextLengthError('上下文超过模型窗口；请使用 /compact task、/compact N 或 /clear。',
                                  pre_accept=False)

    api_key = getattr(getattr(client, 'config', None), 'api_key', '')
    parts = [upstream_error_text(code, api_key, 80),
             upstream_error_text(error.get('message', ''), api_key, 2000)]
    detail = ': '.join(part for part in parts if part)
    return ProviderError(f'{label}（{detail}）' if detail else label)


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
        raise IncompleteOutputError(text, answer)
    answer = _decode_output(result.get('output', []))
    client.last_finish_reason = 'content_filter' if answer.get('refusal') else 'stop'
    return answer


def read_sse(client, response):
    from .client import ClientError
    items, content, reasoning, data_lines = {}, [], [], []
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
            raise _failure(event.get('response', event), 'Responses 流式请求失败', client=client)
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
        elif kind == 'response.function_call_arguments.delta':
            delta = event.get('delta')
            if index not in items or items[index].get('type') != 'function_call' or not isinstance(delta, str):
                raise ClientError('Responses function argument delta has no matching call')
            items[index]['arguments'] = items[index].get('arguments', '') + delta
        elif kind == 'response.function_call_arguments.done':
            if index not in items or not isinstance(event.get('arguments'), str):
                raise ClientError('Invalid Responses completed arguments')
            items[index]['arguments'] = event['arguments']
        elif kind == 'response.output_text.delta' or kind in (
                'response.reasoning.delta', 'response.reasoning_text.delta', 'response.reasoning_summary_text.delta'):
            delta = event.get('delta')
            if not isinstance(delta, str):
                raise ClientError('Invalid Responses text delta')
            target = content if kind == 'response.output_text.delta' else reasoning
            target.append(delta)
            client._emit('content' if target is content else 'reasoning', delta)

    for line in response.iter_lines():
        if not line:
            if data_lines:
                consume('\n'.join(data_lines))
                data_lines.clear()
        elif line.startswith('data:'):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        consume('\n'.join(data_lines))
    if terminal is None:
        raise ClientError('Responses 流在完成事件前结束；未执行其中的工具调用。')
    if 'output' not in terminal:
        output = [items[i] for i in sorted(items)]
        if content and not any(i.get('type') == 'message' and i.get('content') for i in output):
            output = [i for i in output if i.get('type') != 'message']
            output.append({'type': 'message', 'role': 'assistant',
                           'content': [{'type': 'output_text', 'text': ''.join(content)}]})
        terminal['output'] = output
    answer = message(client, terminal)
    if reasoning and not answer.get('reasoning_content'):
        answer['reasoning_content'] = ''.join(reasoning)
    emitted = ''.join(content)
    if emitted and answer['content'].startswith(emitted) and len(answer['content']) > len(emitted):
        client._emit('content', answer['content'][len(emitted):])
    if client._marker_filter is not None:
        client._emit_visible('content', client._marker_filter.feed('', final=True))
    return answer
