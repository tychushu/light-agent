"""Bounded response ingestion and SSE framing, independent of either wire API."""
import codecs


def limited_draft(content='', reasoning='', calls=()):
    """Receive-cap recovery must itself fit a checkpoint; tools remain inert."""
    pending, remaining = [], 64000
    for call in calls[:32]:
        function = call.get('function', {})
        raw = function.get('arguments', '')
        raw = raw if isinstance(raw, str) else ''
        partial = raw[:remaining]
        remaining -= len(partial)
        pending.append({'id': str(call.get('id', ''))[:256], 'type': 'function', 'function': {
            'name': str(function.get('name', ''))[:256], 'arguments': partial}})
    result = {'role': 'assistant', 'content': content[-64000:],
              'reasoning_content': reasoning[-64000:], 'truncated': True}
    if pending:
        result['_incomplete_tool_calls'] = pending
    return result


def response_chunks(response, limit):
    from .client import ResponseLimitError
    total = 0
    # Do not ask httpx to coalesce chunks: that delays live token output.
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > limit:
            raise ResponseLimitError('LLM response exceeds max_response_bytes; partial output is a draft.',
                                     limited_draft())
        yield chunk


def iter_sse_data(response, limit):
    """Accept CR/LF/CRLF and split UTF-8; cap bytes before buffering a long line."""
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    pending, data = [], []
    previous_cr = False

    def texts():
        for chunk in response_chunks(response, limit):
            yield decoder.decode(chunk)
        yield decoder.decode(b'', final=True)

    for text in texts():
        if not text:
            continue
        if previous_cr and text.startswith('\n'):
            text = text[1:]
        previous_cr = text.endswith('\r')
        parts = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        pending.append(parts[0])
        for part in parts[1:]:
            line = ''.join(pending)
            pending = [part]
            if not line:
                if data:
                    yield '\n'.join(data)
                    data.clear()
            elif line.startswith('data:'):
                value = line[5:]
                data.append(value[1:] if value.startswith(' ') else value)
    line = ''.join(pending)
    if line.startswith('data:'):
        value = line[5:]
        data.append(value[1:] if value.startswith(' ') else value)
    if data:
        yield '\n'.join(data)
