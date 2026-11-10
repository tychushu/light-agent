"""Responses decoding, loaded only for profiles using that wire protocol."""
from copy import deepcopy
import json
from .streaming import iter_sse_data, limited_draft


# Responses lifecycle events that are pure connection metadata: they carry no
# body, reasoning, output item, tool fragment or side effect. Any other event
# on the stream before a failure disqualifies a same-wire replay.
_METADATA_EVENTS = frozenset({
    'response.created', 'response.in_progress', 'response.queued',
})


def _failure(value, label, client=None, event_type=None, *, empty_stream=False):
    from .client import (ProviderError, ContextLengthError, RateLimitError, EmptyStreamRateLimit,
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
        cls = EmptyStreamRateLimit if empty_stream else RateLimitError
        return cls(f'{label}（{detail}）' if detail else label, pre_accept=False)
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
    from .client import ClientError, EmptyStreamRateLimit
    items, content, reasoning, arguments = {}, [], [], {}
    # Text/thinking fragments are kept per output_index so a failure-frame
    # snapshot can be merged item by item (stable id first) instead of via a
    # whole-string suffix scan that mixes unrelated messages.
    text_fragments, reasoning_fragments, text_ids = {}, {}, {}
    terminal = None
    pristine = True  # only connection metadata seen so far

    def consume(data):
        nonlocal terminal, pristine
        stripped = data.strip()
        if stripped in ('[DONE]', 'null', ''):
            if stripped == '[DONE]':
                # A termination marker closes empty-stream retry eligibility:
                # a later error frame is not a fresh connection failure.
                pristine = False
            return
        try:
            event = json.loads(data)
        except ValueError:
            raise ClientError('LLM endpoint returned malformed SSE JSON') from None
        if not isinstance(event, dict):
            pristine = False
            return
        kind = event.get('type', '')
        if not isinstance(kind, str):
            # A non-string type is an unknown event: keep the original
            # ignore-and-continue decoding, but forfeit this round's retry.
            pristine = False
            kind = ''
        if kind in ('error', 'response.failed'):
            value = event.get('response', event)
            failure_output = value.get('output') if isinstance(value, dict) else None
            if failure_output:
                # The failure frame itself carries output items: that is partial
                # work, so it can never be replayed. Preserve it as an inert draft.
                pristine = False
                if isinstance(failure_output, list):
                    absorb_output(failure_output)
            # Only an output-free (pristine) stream may be replayed with the
            # same wire request. Any partial body, thinking, output item, tool
            # fragment, or unknown possibly-side-effect event keeps this failure
            # terminal and preserves its draft.
            raise _failure(value, 'Responses 流式请求失败', client=client,
                           event_type=kind, empty_stream=pristine)
        if kind in _METADATA_EVENTS:
            # Pure lifecycle metadata, provided it carries no output items.
            payload = event.get('response')
            if isinstance(payload, dict) and payload.get('output'):
                pristine = False
            return
        pristine = False
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
            if item.get('type') in ('message', 'reasoning'):
                ident = item.get('id')
                if isinstance(ident, str) and ident:
                    text_ids[index] = ident
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
            is_text = kind == 'response.output_text.delta'
            target = content if is_text else reasoning
            target.append(delta)
            (text_fragments if is_text else reasoning_fragments).setdefault(index, []).append(delta)
            # A gateway may omit output_item.added and attach the stable item
            # identity to each delta instead; keep it so two items sharing a
            # text prefix are still told apart.
            ident = event.get('item_id')
            if isinstance(ident, str) and ident:
                text_ids.setdefault(index, ident)
            client._emit('content' if is_text else 'reasoning', delta)

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

    def absorb_output(output):
        """Merge a failure frame's own output snapshot item by item.

        Text/thinking fragments are tracked per output_index from the stream.
        A snapshot item is aligned by stable id -> output_index -> unclaimed
        fragment prefix, then merged into that one item's fragments; fragments
        the snapshot does not cover keep their original index order. Tools are
        aligned by id/call_id/index and stay inert, one entry per call_id.
        """
        def item_text(item):
            parts = item.get('content')
            parts = parts if isinstance(parts, list) else []
            if item.get('type') == 'reasoning':
                summary = item.get('summary')
                if isinstance(summary, list):
                    parts = parts + summary
            pieces = []
            for part in parts:
                if isinstance(part, str):
                    pieces.append(part)
                elif isinstance(part, dict) and isinstance(part.get('text'), str):
                    pieces.append(part['text'])
            return ''.join(pieces)

        def stable_id(item):
            for name in ('id', 'item_id', 'call_id'):
                value = item.get(name)
                if isinstance(value, str) and value:
                    return value
            return None

        def foreign_identity(index, ident):
            # An index already bound to a different stable id owns its own
            # draft: a new/unrelated snapshot item must not claim it through
            # either the output_index or the text-prefix fallback.
            known = text_ids.get(index)
            return bool(ident and known and known != ident)

        def same_identity(a, b):
            for name in ('id', 'call_id'):
                left, right = a.get(name), b.get(name)
                if (isinstance(left, str) and left and isinstance(right, str)
                        and right and left == right):
                    return True
            return False

        def conflicting_identity(a, b):
            for name in ('id', 'call_id'):
                left, right = a.get(name), b.get(name)
                if (isinstance(left, str) and left and isinstance(right, str)
                        and right and left != right):
                    return True
            return False

        def merge_fragments(store, index, snap):
            # Merge one item's own fragments only; never scan the whole string.
            existing = ''.join(store.get(index, []))
            if snap == existing or existing.startswith(snap):
                return  # streamed deltas already cover this text
            if snap.startswith(existing):
                store[index] = [snap]  # complete snapshot, no double append
                return
            # Divergent: keep this item's streamed fragments as-is. Uncovered
            # items keep their own fragments; nothing is duplicated here.
            store.setdefault(index, [])

        def align_text(item, offset, store, claimed):
            snap = item_text(item)
            ident = stable_id(item)
            # Stable ID wins over positional guessing.
            if ident:
                for index, known in text_ids.items():
                    if known == ident and index not in claimed:
                        return index
                for index, existing in items.items():
                    if (isinstance(existing, dict) and index not in claimed
                            and same_identity(existing, item)):
                        return index
            explicit = item.get('output_index')
            if (isinstance(explicit, int) and not isinstance(explicit, bool)
                    and explicit >= 0 and explicit not in claimed
                    and not foreign_identity(explicit, ident)):
                return explicit
            # Otherwise claim the item whose own fragments match this snapshot,
            # but never one whose known identity differs from this item's.
            for index in [offset] + sorted(store):
                if index in claimed or foreign_identity(index, ident):
                    continue
                existing = ''.join(store.get(index, []))
                if existing and (snap.startswith(existing) or existing.startswith(snap)):
                    return index
            return None

        next_index = max(set(text_fragments) | set(reasoning_fragments) | set(items), default=-1) + 1
        claimed_text, claimed_reasoning = set(), set()

        # Pass 1: text / thinking items, merged per item in index order.
        for offset, item in enumerate(output):
            if not isinstance(item, dict) or item.get('type') not in ('message', 'reasoning'):
                continue
            is_reasoning = item.get('type') == 'reasoning'
            store = reasoning_fragments if is_reasoning else text_fragments
            claimed = claimed_reasoning if is_reasoning else claimed_text
            target = align_text(item, offset, store, claimed)
            if target is None:
                target = next_index
                next_index += 1
            claimed.add(target)
            merge_fragments(store, target, item_text(item))

        # Pass 2: tool items, aligned by stable id/call_id first.
        for offset, item in enumerate(output):
            if not isinstance(item, dict) or item.get('type') != 'function_call':
                continue
            target = None
            for index, other in items.items():
                if (isinstance(other, dict) and other.get('type') == 'function_call'
                        and same_identity(other, item)):
                    target = index
                    break
            if target is None:
                explicit = item.get('output_index')
                if (isinstance(explicit, int) and not isinstance(explicit, bool)
                        and explicit >= 0):
                    at = items.get(explicit)
                    if (isinstance(at, dict) and at.get('type') == 'function_call'
                            and not conflicting_identity(at, item)):
                        target = explicit
            if target is None:
                at = items.get(offset)
                if (isinstance(at, dict) and at.get('type') == 'function_call'
                        and not conflicting_identity(at, item)):
                    target = offset
            if target is None:
                target = max(items, default=-1) + 1
            merged = dict(items.get(target) or {})
            for name, value in item.items():
                if name in ('content', 'summary', 'arguments', 'output_index'):
                    continue
                if not merged.get(name) and value is not None:
                    merged[name] = deepcopy(value)
            items[target] = merged
            value = item.get('arguments', '')
            value = value if isinstance(value, str) else ''
            prior = ''.join(arguments.get(target, []))
            if value and value.startswith(prior):
                arguments[target] = [value]  # complete args, no doubled fragment
            elif prior.startswith(value):
                pass  # streamed deltas are already more complete
            else:
                arguments.setdefault(target, [])

        # Recompose the flat draft text from the merged per-item fragments.
        content[:] = [piece for index in sorted(text_fragments)
                      for piece in text_fragments[index]]
        reasoning[:] = [piece for index in sorted(reasoning_fragments)
                        for piece in reasoning_fragments[index]]

    try:
        for data in iter_sse_data(response, client.config.max_response_bytes):
            consume(data)
    except EmptyStreamRateLimit:
        # No draft to keep: an output-free stream has nothing to preserve.
        raise
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
