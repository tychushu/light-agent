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
            '_subagent_calls_total', '_subagent_api_calls_total', '_subagent_tool_calls_total')


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


def snapshot(agent, scope, checkpoint=False):
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
    value = {'version': 3, 'config': agent.config.public_dict(), 'scope': scope,
             'base_system': agent.base_system,
             'messages': messages, 'active_skills': agent.get_active_skills(), 'counters': {k: getattr(agent, k) for k in COUNTERS}}
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
    if payload.get('version') not in (1, 2, 3):
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
    counters = payload['counters']
    for key in COUNTERS:
        value = counters.get(key, 0)
        if key in ('_usage_history', '_request_history_chars'):
            if not isinstance(value, list):
                raise ValueError('Invalid session statistics')
        elif not isinstance(value, int) or value < 0:
            raise ValueError('Invalid session statistics')
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

    def save(self, identifier, revision, payload, clear_draft=True):
        self.check_id(identifier)
        text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        if len(text.encode()) > 20 * 1024 * 1024:
            raise ValueError('Session exceeds 20 MiB; start a new session')
        with self.db:
            if revision == 0:
                first = next((m.get('content', '') for m in payload['messages'] if m['role'] == 'user'), '')
                title = (' '.join(first.split())[:60] if isinstance(first, str) else '') or 'New session'
                self.db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)', (identifier, title, time.time(), 1, text))
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
        text = json.dumps(scrub_keys(draft), ensure_ascii=False)
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
        return json.loads(row[0]) if row else None

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

    def rename(self, identifier, title):
        self.check_id(identifier)
        title = validate_title(title)
        with self.db:
            if self.db.execute('UPDATE sessions SET title=? WHERE id=?', (title, identifier)).rowcount != 1:
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
