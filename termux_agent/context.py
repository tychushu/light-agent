"""Character budgets and explicit, lossless-to-archive context reduction."""
import json


def context_chars(messages, tools=()):
    return sum(len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
               for value in messages) + len(json.dumps(tools, ensure_ascii=False, separators=(',', ':')))


def compact_messages(messages, keep_turns, archive_id):
    from .sessions import validate_messages
    validate_messages(messages)
    if isinstance(keep_turns, bool) or not isinstance(keep_turns, int) or not 1 <= keep_turns <= 100:
        raise ValueError('Use /compact [1–100 recent user turns]')
    starts = [i for i, m in enumerate(messages) if m['role'] == 'user' and
              not str(m.get('content', '')).startswith('[Context archive:')]
    if len(starts) <= keep_turns:
        return list(messages), 0
    start = starts[-keep_turns]
    note = {'role': 'user', 'content':
            f'[Context archive: {archive_id}] Earlier messages were omitted from this context. '
            'The complete conversation remains saved in that session. Do not infer omitted facts '
            'or instructions; ask the user to recover details with /recall or /session load when needed.'}
    result = [messages[0], note, *messages[start:]]
    validate_messages(result)
    return result, start - 1
