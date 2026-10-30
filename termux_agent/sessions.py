"""Local SQLite sessions: atomic snapshots, revision checks, no API credentials."""
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .config import Config

COUNTERS = ('_total_calls', '_tool_calls_total', '_usage_history',
            '_request_history_chars', '_conversation_count', '_direct_shell_count',
            '_subagent_calls_total', '_subagent_api_calls_total', '_subagent_tool_calls_total',
            '_tool_calls_executed', '_tool_calls_denied', '_tool_calls_not_run',
            '_tool_calls_approval_denied', '_tool_calls_hard_limit_not_run', '_tool_write_unknown',
            '_http_attempts_total', '_http_retries_total', '_context_recoveries', '_task_compactions',
            '_rtk_shell_results', '_rtk_filter_attempts', '_rtk_compressed_results',
            '_rtk_filter_failures', '_rtk_incomplete_capture', '_rtk_raw_artifacts',
            '_rtk_artifact_bytes', '_rtk_before_bytes', '_rtk_after_bytes',
            '_rtk_before_chars', '_rtk_after_chars')


def new_id():
    return uuid.uuid4().hex[:12]


def validate_title(title):
    if not isinstance(title, str):
        raise ValueError('Title must contain 1–120 characters on one line')
    title = title.strip()
    if (not title or len(title) > 120 or
            any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in '\u2028\u2029' for c in title)):
        raise ValueError('Title must contain 1–120 characters on one line')
    return title


def validate_messages(messages):
    if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict) or messages[0].get('role') != 'system':
        raise ValueError('Invalid session messages')
    pending = set()
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError('Invalid message')
        role = message.get('role')
        if role not in ('system', 'user', 'assistant', 'tool') or (role == 'system' and i):
            raise ValueError('Invalid message role')
        if pending and role != 'tool':
            raise ValueError('Incomplete tool turn')
        if role == 'tool':
            identifier = message.get('tool_call_id')
            if identifier not in pending:
                raise ValueError('Unexpected tool result')
            pending.remove(identifier)
        if role == 'assistant' and message.get('tool_calls'):
            calls = message['tool_calls']
            from .agent import Agent
            if Agent._validate_calls(calls):
                raise ValueError('Invalid tool calls')
            pending = {call['id'] for call in calls}
    if pending:
        raise ValueError('Incomplete tool turn')


def _safe_int(value, default=0, minimum=0):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= minimum else default


def _safe_float(value, default=0.0, minimum=0.0):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if value == value and value not in (float('inf'), float('-inf')) and value >= minimum:
                return float(value)
        except (OverflowError, TypeError):
            pass
    return float(default)


def validate_task_state(value, config=None):
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    out = dict(value)
    out['id'] = out.get('id') if isinstance(out.get('id'), str) and out['id'] else new_id()
    out['goal'] = out.get('goal') if isinstance(out.get('goal'), str) else ''
    out['status'] = out.get('status') if isinstance(out.get('status'), str) else 'unknown'
    stop_reason = out.get('stop_reason')
    out['stop_reason'] = stop_reason if isinstance(stop_reason, str) else None
    out['started'] = _safe_float(out.get('started'), time.time())
    out['active_seconds'] = _safe_float(out.get('active_seconds'))
    for key in ('segments', 'logical_calls', 'tokens', 'recovery_requests', 'compactions'):
        out[key] = _safe_int(out.get(key))
    defaults = {
        'max_logical_calls': getattr(config, 'max_task_calls', 32),
        'max_active_seconds': getattr(config, 'max_task_seconds', 600.0),
        'max_tokens': getattr(config, 'max_task_tokens', 0),
    }
    for key, fallback in defaults.items():
        out[key] = _safe_int(out.get(key), fallback) if key != 'max_active_seconds' else _safe_float(out.get(key), fallback)
    out['token_source'] = out.get('token_source') if isinstance(out.get('token_source'), str) else 'unknown'
    out['token_budget_exact'] = out.get('token_budget_exact') if isinstance(out.get('token_budget_exact'), bool) else True
    evidence = out.get('progress_evidence', [])
    out['progress_evidence'] = [dict(item) for item in evidence if isinstance(item, dict)][-32:] if isinstance(evidence, list) else []
    return out


def validate_last_run(value):
    if not isinstance(value, dict):
        return {'status': 'idle', 'stop_reason': 'idle', 'auto': False, 'continuations': 0,
                'steps': 0, 'message': '尚无任务。', 'continue_hint': '输入任务，或用 /session load ID 恢复。'}
    out = dict(value)
    for key in ('status', 'message', 'continue_hint', 'stop_reason', 'blocked_by'):
        if key in out and not isinstance(out[key], (str, type(None))):
            out[key] = str(out[key])
    out.setdefault('status', 'idle')
    out.setdefault('stop_reason', out.get('status'))
    out['auto'] = out.get('auto') if isinstance(out.get('auto'), bool) else False
    out['can_continue'] = out.get('can_continue') if isinstance(out.get('can_continue'), bool) else True
    out['continuations'] = _safe_int(out.get('continuations'))
    out['steps'] = _safe_int(out.get('steps'))
    for key in ('task_budget', 'context'):
        out[key] = dict(out[key]) if isinstance(out.get(key), dict) else {}
    return out


def validate_draft(value):
    if not isinstance(value, dict):
        return None
    out = dict(value)
    for key in ('content', 'reasoning_content'):
        out[key] = out[key] if isinstance(out.get(key), str) else ''
    for key in ('_incomplete_tool_calls', '_responses_output'):
        out[key] = [dict(item) for item in out[key] if isinstance(item, dict)] if isinstance(out.get(key), list) else []
        if not out[key]:
            out.pop(key, None)
    if '_incomplete_output' in out and not isinstance(out['_incomplete_output'], (bool, dict, str)):
        out.pop('_incomplete_output')
    if out.get('truncated') is not None and not isinstance(out.get('truncated'), bool):
        out['truncated'] = bool(out['truncated'])
    return out


def validate_archives(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict) and isinstance(item.get('id'), str) and isinstance(item.get('messages'), list):
            result.append(copy.deepcopy(item))
    return result[-16:]


def scrub_keys(value, extra=()):
    keys = {v for k, v in os.environ.items() if k.endswith('_API_KEY') and v}
    keys.update(v for v in extra if v)
    def scrub(obj):
        if isinstance(obj, str):
            for key in keys:
                obj = obj.replace(key, '[REDACTED]')
            return obj
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        return obj
    return scrub(value)


def snapshot(agent, scope, checkpoint=False, branch=None):
    messages = copy.deepcopy(agent.messages) if checkpoint else agent.messages
    if checkpoint:
        pending = {}
        for message in messages:
            if message.get('role') == 'assistant' and message.get('tool_calls'):
                pending = {call['id']: call for call in message['tool_calls']}
            elif message.get('role') == 'tool':
                pending.pop(message.get('tool_call_id'), None)
        for identifier in pending:
            messages.append({'role': 'tool', 'tool_call_id': identifier, 'content': json.dumps({
                'error': 'Interrupted checkpoint: tool outcome is unknown. It may have run. '
                         'Inspect actual state before retrying; do not assume success or replay automatically.'})})
    validate_messages(messages)
    value = {'version': 4, 'config': agent.config.public_dict(), 'scope': scope,
             'base_system': agent.base_system,
             'messages': messages, 'active_skills': agent.get_active_skills(), 'counters': {k: getattr(agent, k) for k in COUNTERS},
             'task_state': agent.task_state, 'last_run': agent.last_run,
             'incomplete_draft': agent.incomplete_draft,
             'in_memory_archives': agent.in_memory_archives}
    if branch is not None:
        value['branch'] = branch
    if checkpoint:
        value['checkpoint'] = True
    return scrub_keys(value, (agent.config.api_key,))


class HistoryClient:
    """Local history has no dependency on a live provider or its credentials."""
    history_only = True
    last_request = last_usage = last_finish_reason = None
    streamed = streamed_text = False
    last_attempts = 0

    def __init__(self, reason):
        self.reason = reason

    def chat(self, *args, **kwargs):
        from .client import ClientError
        raise ClientError('历史查看模式不能调用模型；请用 /api NAME 新建可用会话。')

    def close(self):
        pass


def restore(payload, build_agent, *, history_fallback=False):
    if payload.get('version') not in (1, 2, 3, 4):
        raise ValueError('Unsupported session version')
    validate_messages(payload['messages'])
    scope, saved = payload['scope'], payload['config']
    history_reason = ''
    try:
        config = Config.load(scope.get('config_path'), saved['api_profile'])
        if (config.base_url.rstrip('/'), config.model, config.wire_api) != (
                saved['base_url'].rstrip('/'), saved['model'], saved.get('wire_api', 'chat_completions')):
            raise ValueError('API profile endpoint/model/protocol changed; restore its original config or start a new session')
    except (ValueError, OSError):
        if not history_fallback:
            raise
        history_reason = '原 API 配置不可用，或地址、模型、协议已变更。'
        config = Config(**{k: v for k, v in saved.items() if k in Config.__dataclass_fields__ and k != 'api_key'})
    if not Path(scope['directory']).is_dir():
        if not history_fallback:
            raise ValueError('Session directory no longer exists')
        history_reason += '原工作目录已不存在，保留当前目录查看历史。'
    counters = payload.get('counters')
    counters = counters if isinstance(counters, dict) else {}
    for key in COUNTERS:
        value = counters.get(key, 0)
        if key in ('_usage_history', '_request_history_chars'):
            if not isinstance(value, list):
                counters[key] = []
        elif not isinstance(value, int) or value < 0:
            counters[key] = 0
    active_skills = payload.get('active_skills', {})
    if not isinstance(active_skills, dict):
        raise ValueError('Invalid session skills')
    if any(not isinstance(name, str) or not isinstance(content, str)
           for name, content in active_skills.items()):
        raise ValueError('Invalid session skills')
    base = payload.get('base_system')
    if base is None:
        base = payload['messages'][0]['content']
        if not isinstance(base, str):
            raise ValueError('Invalid saved system prompt')
        for name, content in reversed(list(active_skills.items())):
            block = f"\n\n<!-- SKILL_START: {name} -->\n{content}\n<!-- SKILL_END: {name} -->"
            base = base.removesuffix(block)
    if not isinstance(base, str) or not base.strip():
        raise ValueError('Invalid saved system prompt')
    kwargs = {'base_system': base}
    if history_reason:
        config.api_key = ''
        kwargs['client'] = HistoryClient(history_reason)
    agent = build_agent(config, scope['instructions'], **kwargs)
    for name, content in active_skills.items():
        agent.load_skill(name, content)
    agent.messages = payload['messages']
    agent.system = agent.messages[0]['content']
    for key in COUNTERS:
        setattr(agent, key, counters.get(key, 0))
    agent.task_state = validate_task_state(payload.get('task_state'), config)
    agent.last_run = validate_last_run(payload.get('last_run', agent.last_run))
    agent.incomplete_draft = validate_draft(payload.get('incomplete_draft'))
    agent.in_memory_archives = validate_archives(payload.get('in_memory_archives'))
    agent._unsafe_write_fingerprints = agent._scan_unsafe_writes(agent.messages)
    return config, agent, scope


class SessionStore:
    def __init__(self, root=None):
        root = Path(root) if root else Path.home() / '.local/share/termux-agent'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        self.root = root
        path = root / 'sessions.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.db = sqlite3.connect(path, timeout=5)
        try:
            self.db.execute('PRAGMA busy_timeout=5000')
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
        except sqlite3.Error:
            self.db.close()
            raise
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions '
                        '(id TEXT PRIMARY KEY, title TEXT, updated REAL, revision INTEGER, payload TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS drafts '
                        '(id TEXT PRIMARY KEY, revision INTEGER, updated REAL, payload TEXT)')
        self.db.commit()
        self._secure_files()

    def _secure_files(self):
        for suffix in ('', '-wal', '-shm'):
            try:
                (self.root / ('sessions.sqlite3' + suffix)).chmod(0o600)
            except FileNotFoundError:
                pass

    @staticmethod
    def check_id(identifier):
        if not re.fullmatch('[0-9a-f]{12}', identifier):
            raise ValueError('Use the exact 12-character session ID from /sessions')

    def save(self, identifier, revision, payload, clear_draft=True, title=None):
        self.check_id(identifier)
        text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        if len(text.encode()) > 20 * 1024 * 1024:
            raise ValueError('Session exceeds 20 MiB; start a new session')
        with self.db:
            if revision == 0:
                first = next((m.get('content', '') for m in payload['messages'] if m['role'] == 'user'), '')
                saved_title = validate_title(title) if title is not None else (
                    (' '.join(first.split())[:60] if isinstance(first, str) else '') or 'New session')
                self.db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',
                                (identifier, saved_title, time.time(), 1, text))
            else:
                result = self.db.execute('UPDATE sessions SET payload=?,updated=?,revision=revision+1 WHERE id=? AND revision=?',
                                         (text, time.time(), identifier, revision))
                if result.rowcount != 1:
                    raise ValueError('Session changed/deleted in another process; use /session fork to preserve this conversation separately')
            if clear_draft:
                self.db.execute('DELETE FROM drafts WHERE id=?', (identifier,))
            else:
                self.db.execute('UPDATE drafts SET revision=? WHERE id=? AND revision=?',
                                (revision + 1, identifier, revision))
        self._secure_files()
        return revision + 1

    def save_draft(self, identifier, revision, draft):
        self.check_id(identifier)
        safe = validate_draft(draft)
        if safe is None:
            raise ValueError('Draft must be an object')
        text = json.dumps(scrub_keys(safe), ensure_ascii=False)
        if len(text) > 256_000:
            raise ValueError('Stream draft exceeds 256000 characters')
        with self.db:
            result = self.db.execute('INSERT OR REPLACE INTO drafts '
                                     'SELECT id,revision,?,? FROM sessions WHERE id=? AND revision=?',
                                     (time.time(), text, identifier, revision))
            if result.rowcount != 1:
                raise ValueError('Session changed while streaming; draft was not overwritten')
        self._secure_files()

    def draft(self, identifier):
        self.check_id(identifier)
        row = self.db.execute('SELECT d.payload FROM drafts d JOIN sessions s ON s.id=d.id '
                              'AND s.revision=d.revision WHERE d.id=?', (identifier,)).fetchone()
        if not row:
            return None
        try:
            return validate_draft(json.loads(row[0]))
        except (TypeError, ValueError):
            return {'content': '', 'reasoning_content': '', 'invalid_saved_draft': True}

    def load(self, identifier):
        self.check_id(identifier)
        row = self.db.execute('SELECT revision,payload FROM sessions WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('Session not found')
        return row[0], json.loads(row[1])

    def listing(self, limit=50, query=None):
        sql = 'SELECT id,title,updated,revision,payload FROM sessions'
        params = []
        if query is not None:
            query = validate_title(query)
            sql += " WHERE instr(lower(coalesce(title,'')),lower(?)) > 0"
            params.append(query)
        sql += ' ORDER BY updated DESC LIMIT ?'
        rows = self.db.execute(sql, (*params, limit))
        result = []
        for identifier, title, updated, revision, text in rows:
            payload = json.loads(text)
            result.append({'id': identifier, 'title': title, 'updated': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(updated)),
                           'api': payload['config']['api_profile'], 'model': payload['config']['model'],
                           'directory': payload['scope']['directory'], 'messages': len(payload['messages'])})
        return result

    def ids(self, limit=100):
        return [row[0] for row in self.db.execute(
            'SELECT id FROM sessions ORDER BY updated DESC LIMIT ?', (limit,))]

    def recall(self, query, limit=20, silent_writes=True):
        """On-demand literal search: no startup scan or persistent full-text index."""
        from .history import history_entry
        query = validate_title(query).casefold()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('Recall limit must be between 1 and 100')
        results = []
        for identifier, title, updated, text in self.db.execute(
                'SELECT id,title,updated,payload FROM sessions ORDER BY updated DESC'):
            payload = json.loads(text)
            index, snippet = None, None
            if query in (title or '').casefold():
                snippet = title
            else:
                for index, message in enumerate(payload['messages'][1:], 1):
                    raw = json.dumps({'content': message.get('content'), 'tool_calls': message.get('tool_calls')},
                                     ensure_ascii=False)
                    if query in raw.casefold():
                        visible = history_entry(message, silent_writes, limit=20 * 1024 * 1024)[1]
                        position = visible.casefold().find(query)
                        if position < 0:
                            snippet = '命中已保存内容；按静默显示规则隐藏正文。'
                        else:
                            start = max(0, position - 60)
                            snippet = ('…' if start else '') + visible[start:start + 200]
                        break
            if snippet is not None:
                results.append({'id': identifier, 'title': title,
                                'updated': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(updated)),
                                'message': index, 'snippet': ' '.join(snippet.split())[:220]})
                if len(results) >= limit:
                    break
        return results

    def export(self, identifier, format='markdown', path=None):
        self.check_id(identifier)
        if format not in ('markdown', 'json'):
            raise ValueError('Export format must be markdown or json')
        row = self.db.execute('SELECT title,updated,revision,payload FROM sessions WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('Session not found')
        revision, payload = row[2], json.loads(row[3])
        data = {'id': identifier, 'title': row[0], 'updated': row[1], 'revision': revision,
                'payload': payload, 'draft': self.draft(identifier)}
        text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
        if format == 'markdown':
            from .history import history_entry
            def block(body):
                fence = '`' * max(3, max((len(m.group()) + 1 for m in re.finditer(r'`+', body)), default=3))
                return fence + 'text\n' + body + '\n' + fence + '\n'
            parts = [f'# {row[0] or "Session"}\n\nSession: {identifier}\n']
            for index, message in enumerate(payload['messages']):
                body = history_entry(message, silent_writes=False, limit=20 * 1024 * 1024)[1]
                if message.get('reasoning_content'):
                    body += '\n\n[Reasoning]\n' + str(message['reasoning_content'])
                parts.append(f'## {index} · {message["role"]}\n\n' + block(body))
            if data['draft']:
                parts.append('## Interrupted stream draft\n\n' + block(json.dumps(data['draft'], ensure_ascii=False, indent=2)))
            text = '\n'.join(parts)
        target = Path(path).expanduser() if path else self.root / 'exports' / (
            identifier + '-' + str(time.time_ns()) + ('.json' if format == 'json' else '.md'))
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target.resolve()

    def rename(self, identifier, title, revision=None):
        self.check_id(identifier)
        title = validate_title(title)
        with self.db:
            if revision is None:
                result = self.db.execute('UPDATE sessions SET title=? WHERE id=?', (title, identifier))
            else:
                result = self.db.execute('UPDATE sessions SET title=? WHERE id=? AND revision=?',
                                         (title, identifier, revision))
            if result.rowcount != 1:
                raise ValueError('Session not found')
        return title

    def title(self, identifier):
        self.check_id(identifier)
        row = self.db.execute('SELECT title FROM sessions WHERE id=?', (identifier,)).fetchone()
        return row[0] if row is not None else None

    def delete(self, identifier):
        self.check_id(identifier)
        with self.db:
            if self.db.execute('DELETE FROM sessions WHERE id=?', (identifier,)).rowcount != 1:
                raise ValueError('Session not found')
            self.db.execute('DELETE FROM drafts WHERE id=?', (identifier,))

    def close(self):
        self.db.close()
