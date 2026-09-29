"""Local SQLite sessions: atomic snapshots, revision checks, no API credentials."""
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .config import Config

COUNTERS = ('_total_calls', '_tool_calls_total', '_usage_history',
            '_request_history_chars', '_conversation_count', '_direct_shell_count')


def new_id():
    return uuid.uuid4().hex[:12]


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


def snapshot(agent, scope):
    validate_messages(agent.messages)
    value = {'version': 2, 'config': agent.config.public_dict(), 'scope': scope,
             'messages': agent.messages, 'active_skills': agent.get_active_skills(), 'counters': {k: getattr(agent, k) for k in COUNTERS}}
    keys = {v for k, v in os.environ.items() if k.endswith('_API_KEY') and v}
    if agent.config.api_key:
        keys.add(agent.config.api_key)
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


def restore(payload, build_agent):
    if payload.get('version') not in (1, 2):
        raise ValueError('Unsupported session version')
    validate_messages(payload['messages'])
    scope, saved = payload['scope'], payload['config']
    config = Config.load(scope.get('config_path'), saved['api_profile'])
    if (config.base_url.rstrip('/'), config.model) != (saved['base_url'].rstrip('/'), saved['model']):
        raise ValueError('API profile endpoint/model changed; restore its original config or start a new session')
    if not Path(scope['directory']).is_dir():
        raise ValueError('Session directory no longer exists')
    for key in COUNTERS:
        value = payload['counters'][key]
        if key in ('_usage_history', '_request_history_chars'):
            if not isinstance(value, list):
                raise ValueError('Invalid session statistics')
        elif not isinstance(value, int) or value < 0:
            raise ValueError('Invalid session statistics')
    agent = build_agent(config, scope['instructions'])
    for name, content in payload.get('active_skills', {}).items():
        agent.load_skill(name, content)
    agent.messages = payload['messages']
    agent.system = agent.messages[0]['content']
    for key in COUNTERS:
        setattr(agent, key, payload['counters'][key])
    return config, agent, scope


class SessionStore:
    def __init__(self, root=None):
        root = Path(root) if root else Path.home() / '.local/share/termux-agent'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        path = root / 'sessions.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions '
                        '(id TEXT PRIMARY KEY, title TEXT, updated REAL, revision INTEGER, payload TEXT)')
        self.db.commit()

    @staticmethod
    def check_id(identifier):
        if not re.fullmatch('[0-9a-f]{12}', identifier):
            raise ValueError('Use the exact 12-character session ID from /sessions')

    def save(self, identifier, revision, payload):
        self.check_id(identifier)
        text = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        if len(text.encode()) > 20 * 1024 * 1024:
            raise ValueError('Session exceeds 20 MiB; start a new session')
        with self.db:
            if revision == 0:
                title = next((m.get('content', '')[:60] for m in payload['messages'] if m['role'] == 'user'), 'New session')
                self.db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)', (identifier, title, time.time(), 1, text))
            else:
                result = self.db.execute('UPDATE sessions SET payload=?,updated=?,revision=revision+1 WHERE id=? AND revision=?',
                                         (text, time.time(), identifier, revision))
                if result.rowcount != 1:
                    raise ValueError('Session changed/deleted in another process; use /session fork to preserve this conversation separately')
        return revision + 1

    def load(self, identifier):
        self.check_id(identifier)
        row = self.db.execute('SELECT revision,payload FROM sessions WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('Session not found')
        return row[0], json.loads(row[1])

    def listing(self, limit=50):
        rows = self.db.execute('SELECT id,title,updated,revision,payload FROM sessions ORDER BY updated DESC LIMIT ?', (limit,))
        result = []
        for identifier, title, updated, revision, text in rows:
            payload = json.loads(text)
            result.append({'id': identifier, 'title': title, 'updated': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(updated)),
                           'api': payload['config']['api_profile'], 'model': payload['config']['model'],
                           'directory': payload['scope']['directory'], 'messages': len(payload['messages'])})
        return result

    def rename(self, identifier, title):
        self.check_id(identifier)
        if not title.strip() or len(title) > 120:
            raise ValueError('Title must contain 1–120 characters')
        with self.db:
            if self.db.execute('UPDATE sessions SET title=? WHERE id=?', (title, identifier)).rowcount != 1:
                raise ValueError('Session not found')

    def delete(self, identifier):
        self.check_id(identifier)
        with self.db:
            if self.db.execute('DELETE FROM sessions WHERE id=?', (identifier,)).rowcount != 1:
                raise ValueError('Session not found')

    def close(self):
        self.db.close()
