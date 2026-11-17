"""Plain terminal REPL; no terminal UI framework."""
import argparse
import copy
import hashlib
import json
import sys
import os
import sqlite3
import shlex
import subprocess
import shutil
import time
from pathlib import Path
from .instructions import load_instructions

from .agent import Agent
from .client import Client, ClientError
from .config import Config
from .sessions import SessionStore, new_id, snapshot, restore, validate_title, has_unknown_tool_results
from .skills import SkillRegistry
from .api_profiles import ProfileStore
from .terminal_ui import Columns, Completer, COMMANDS, format_context_status
from .local_input import load_text_submission, path_value
from .display_privacy import WRITE_TOOLS, hide_write_payloads, safe_tool_arguments
from .approval import reason as approval_reason
from .history import history_entry
from .i18n import text as ui_text, help_text
from .security import active_secret_values, child_environment, redact_text

HELP = """/help           显示命令帮助（Show commands）
Tab             补全命令、API 配置、会话 ID 和技能名称
~/path 或 /path  补全本地路径；回车将 UTF-8 文本文件内容提交给模型
/clear          新建空白会话，保留原会话历史
/compact [N]    自动精简跨轮次历史，保留用户指令；指定 N 则只保留最近 N 轮
/compact task [N]  压缩当前单任务工具链；保留最近 N 个完整工具组，不调用模型
/continue [N]   从检查点继续同一任务，开始一个新的受控模型调用段
continue/resume  未完成任务的简短续跑输入；不新建任务，引号内文字按原文发送
/status         查看上次停止原因和继续方式
/recall TEXT    在已保存会话正文和工具参数中检索，最多显示 20 个会话
/export [ID] [markdown|json] [PATH]  导出会话；默认当前会话、Markdown、新文件
/stats          查看模型调用、RTK 应用后 UTF-8 字节/字符节省（非 token）、服务端 token 用量和上下文大小
/config         查看当前生效配置，不显示密钥
/debug          开关请求用量和工具结果的调试显示
/debug-context  查看上次请求的消息和工具，密钥会脱敏
/silent [on|off]  开关写入、编辑和委派参数的静默显示
/auto [on|off|steps N]  开关自动连续执行，或设置模型调用上限
/run TASK       连续执行已授权任务，直到完成、阻塞或达到上限
/api [name|number|next]  列出或切换 API 配置；切换后新建会话
/api models [NAME]  动态查询指定 API 的可用模型；默认当前 API
/api model [ID|N] [NAME]  选用模型并新建会话；序号使用最近一次模型列表
/api add NAME URL MODEL [KEY_ENV] [local|cloud]  添加 API 配置
/api default NAME   设置以后启动时默认使用的 API 配置
/api show NAME      查看指定 API 配置，不显示密钥
/api remove NAME    删除通过 /api add 添加的 API 配置
/agent [path|off|directory|reload]  选择或重载目录/会话 agent.md；新建会话
/skill list|info|load|unload|clear   列出、查看、手动加载、卸载或清空技能
/copy [text]    复制指定文字或最后一次回答到 Android 剪贴板
/paste          显示 Android 剪贴板文字
!command        直接运行 Shell，不调用模型；审批规则仍生效
/sessions [N]   列出会话及 API / 模型，折叠旧压缩链；默认 50，最多 500
/sessions all [N]  展开旧版压缩分支
/sessions find TEXT  按标题关键词查找已保存会话
/resume [ID|N]  恢复会话；N 使用最后一次会话列表中的序号
/resume         列出最近 50 个会话，TTY 中可直接选择序号或 ID
/title [NAME]   查看或修改当前会话标题，无需调用模型
/session        查看当前会话 ID、标题和保存状态
/session new [NAME]  新建空白会话，可指定标题
/session load ID|N   恢复会话；N 使用最后一次列表序号，原 API 失效时进入历史查看模式
/session rename NAME  修改当前会话标题
/session rename-id ID NAME  修改其他会话标题，不切换会话
/session fork [NAME]  将当前对话复制为独立会话，可指定标题
/session save        保存当前会话
/session archives [ID|N]  列出会话的压缩归档
/session archive ARCHIVE_ID  查看压缩前快照的最近消息；完整内容可用 /export 导出
/session delete [ID|N]  按列表序号或 ID 删除；不带参数进入选择，需确认
/history [N] [skip]  查看最近 N 条消息，skip 可向前翻页
/sub TASK       直接委派一个子任务；/sub 查看执行上限
/exit           保存当前会话并退出
KEY_ENV 是密钥环境变量名；NAME / ID / TEXT 等是需要替换的参数。
"""


_readline = None
_history_file = None

BARE_COMMAND_HINT = ('裸输入 {v0} 不会执行命令；如需执行请使用 {v1}。'
                     '如确实要作为文字发送，请输入带引号的 "{v0}" 或完整句子。')
BARE_COMMANDS = {name: '/' + name for name in (
    'continue', 'clear', 'compact', 'status', 'help', 'exit', 'resume',
)}


def _history_input(value):
    value = value.strip()
    return bool(value) and not (
        value.split(maxsplit=1)[0] in COMMANDS
        or value.lower() in BARE_COMMANDS
        or value.startswith('!')
    )


def configure_readline(history_file=None):
    """Enable terminal editing and remember only explicit task inputs."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True
    try:
        import readline
    except ImportError:
        return False
    global _readline, _history_file
    _readline = readline
    readline.set_auto_history(False)
    if history_file:
        _history_file = Path(history_file)
        _history_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _history_file.parent.chmod(0o700)
        if _history_file.exists() and _history_file.stat().st_size <= 512 * 1024:
            try:
                readline.read_history_file(str(_history_file))
            except (OSError, ValueError):
                pass
        readline.set_history_length(2000)
    for index in range(readline.get_current_history_length() - 1, -1, -1):
        if not _history_input(readline.get_history_item(index + 1) or ''):
            readline.remove_history_item(index)
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind -e")
        for binding in (r'bind "^H" em-delete-prev-char', r'bind "^?" em-delete-prev-char',
                        r'bind "^[[3~" ed-delete-next-char'):
            readline.parse_and_bind(binding)
    else:
        readline.parse_and_bind("set editing-mode emacs")
        for binding in (r'"\C-h": backward-delete-char', r'"\C-?": backward-delete-char',
                        r'"\e[3~": delete-char'):
            readline.parse_and_bind(binding)
    return True


def save_readline_history():
    if not _readline or not _history_file:
        return
    secrets = active_secret_values()
    values = []
    for i in range(1, _readline.get_current_history_length() + 1):
        item = _readline.get_history_item(i)
        if item and _history_input(item):
            values.append(redact_text(item, secrets=secrets))
    temp = _history_file.with_name(_history_file.name + ".tmp")
    try:
        temp.touch(mode=0o600, exist_ok=True)
        temp.chmod(0o600)
        _readline.clear_history()
        for item in values:
            _readline.add_history(item)
        _readline.write_history_file(str(temp))
        temp.chmod(0o600)
        os.replace(temp, _history_file)
    except OSError:
        temp.unlink(missing_ok=True)


def _continued(line):
    slashes = len(line) - len(line.rstrip("\\"))
    return slashes % 2 == 1


def read_prompt(prompt="ta> ", continuation="... "):
    """Read ordinary or explicit triple-quote/backslash-continued input."""
    first = input(prompt)
    quoted = first == '"""'
    if quoted or _continued(first):
        lines = [] if quoted else [first[:-1]]
        while True:
            line = input(continuation)
            if quoted and line == '"""':
                break
            continued = _continued(line)
            lines.append(line[:-1] if continued else line)
            if not quoted and not continued:
                break
    else:
        lines = [first]
    result = "\n".join(lines)
    if _readline and _history_input(result):
        _readline.add_history(result)
    return result


class ReplController:
    """Interactive terminal REPL controller owning active session, UI, and persistence state."""

    _HISTORY_ALLOWED_COMMANDS = (
        '/help', '/sessions', '/session', '/resume', '/history', '/stats', '/status',
        '/config', '/debug', '/debug-context', '/silent', '/api', '/title',
        '/recall', '/export', '/copy', '/paste', '/exit',
    )

    def __init__(self, options, *, language, config_path, directory, session_file, disabled,
                 instructions, instruction_path, config, parser):
        self.options = options
        self.language = language
        self.config_path = config_path
        self.directory = directory
        self.session_file = session_file
        self.disabled = disabled
        self.instructions = instructions
        self.instruction_path = instruction_path
        self.config = config
        self.parser = parser
        self.debug = False
        self.registry = SkillRegistry()  # Lazy: no scan/read until /skill is requested.
        self.profile_store = ProfileStore(config_path)
        self.tool_started = {}
        self.stream_wrote = False
        self.read_grant = False
        self.stream_draft = {}
        self.draft_saved_at = 0.0
        self.saved_fingerprint = None
        self.loaded_row = None
        self.source_meta = None
        self.active_branch = True
        self.source_fingerprint = None
        self.branch_state = {'kind': 'active'}
        self.formatter = Columns(sys.stdout, enabled=sys.stdout.isatty() and options.prompt is None)
        self.last_session_ids = ()
        self.model_catalog = None
        self.cli_commands = tuple(dict.fromkeys((*COMMANDS, "/resume")))
        self.session_id = new_id()
        self.revision = 0
        self.agent = None
        self.store = None
        self._init_runtime()

    def _init_runtime(self):
        try:
            self.agent = None if self.options.session else self.build_agent(self.config, self.instructions)
        except (ValueError, OSError) as exc:
            self.parser.error(str(exc))
        try:
            self.store = None if self.options.no_session else SessionStore()
        except (OSError, sqlite3.Error) as exc:
            if self.agent is not None:
                getattr(self.agent, 'close', self.agent.client.close)()
            self.parser.error(self.tr('Session store unavailable: {v0}', v0=exc))
        self._configure_completer()

    def _configure_completer(self):
        if not _readline:
            return
        completer = Completer(
            _readline,
            profiles=lambda: [row["name"] for row in self.profile_store.list()],
            skills=lambda: [skill.name for skill in self.registry.list_skills()],
            active_skills=lambda: list(self.agent.active_skills),
            session_ids=lambda: self.store.ids() if self.store else [],
            directory=lambda: self.directory,
            session_choices=lambda: [str(i) for i in range(1, len(self.last_session_ids) + 1)],
            model_choices=self.model_choices,
        )
        _readline.set_completer_delims(" \t\n")
        _readline.set_completer(completer)
        if "libedit" in (_readline.__doc__ or ""):
            _readline.parse_and_bind("bind ^I rl_complete")
        else:
            _readline.parse_and_bind("set show-all-if-ambiguous off")
            _readline.parse_and_bind("tab: menu-complete")

    def tr(self, template, **values):
        return ui_text(template, self.language, **values)

    def redact(self, text):
        return redact_text(text, (self.config.api_key,))

    def display(self, value, role="Info", stamp=None):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        # Do not let model/tool text inject terminal control sequences.
        text = ''.join(c if c in '\n\t' or c.isprintable() else f'\\x{ord(c):02x}' for c in text)
        self.formatter.message(role, self.redact(text), stamp=stamp)

    def model_choices(self):
        if self.model_catalog is None:
            return ()
        models = self.model_catalog[1]
        return (*models, *(str(i) for i in range(1, len(models) + 1)))

    def approve(self, name, args):
        if name == "read_file" and self.read_grant:
            return True
        visible = safe_tool_arguments(name, args) if self.config.silent_writes else args
        self.display({"approval_required": name, "arguments": visible,
                      "reason": self.tr(approval_reason(name, args))}, "Approve")
        if not sys.stdin.isatty():
            self.display(self.tr('Denied: approval requires an interactive terminal.'))
            return False
        try:
            prompt = self.tr("Allow this tool call? [y/N/a=all reads this task] "
                             if name == "read_file" else "Allow this tool call? [y/N] ")
            choice = input(prompt).strip().lower()
            if name == "read_file" and choice in ("a", "all"):
                self.read_grant = True
                return True
            return choice in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False

    def event(self, kind, payload):
        if kind == 'request_start':
            self.stream_draft, self.draft_saved_at = {}, 0.0
            return
        if kind == 'context_warning':
            self.display(
                self.tr('上下文字符预算已用 {v0:.0%}；可用 /compact 缩短上下文（字符预算不等于模型 token 窗口）。',
                        v0=payload['chars'] / payload['limit']),
                'Context',
            )
            return
        if kind == 'step_start':
            if self.debug:
                self.display({"step": payload.get("step"), "segment": payload.get("segment"),
                              "task_logical_calls": payload.get("task_logical_calls"),
                              "max_task_logical_calls": payload.get("max_task_logical_calls")}, "Progress")
            return
        if kind == 'segment_extension':
            if self.debug:
                self.display({"segment_extension": payload}, "Progress")
            return
        if kind == 'auto_resume':
            wait = payload.get('wait_seconds') or 0
            self.display(
                self.tr('自动续跑 {v0}/{v1}：{v2}', v0=payload['count'], v1=payload['max'], v2=payload['reason'])
                + (self.tr('；等待 {v0:g} 秒后继续', v0=wait) if wait else ''),
                'Resume',
            )
            return
        if kind == 'resume_routed':
            self.display(self.tr('按 /continue 续跑原任务，不新建任务：{v0}', v0=payload.get('goal', '')), 'Resume')
            return
        if kind == 'task_compacted':
            self.display(
                self.tr('已压缩上下文 {v0:,} → {v1:,} 字符；完整历史：{v2}',
                        v0=payload['before_chars'], v1=payload['after_chars'], v2=payload['archive'])
                + ('' if payload['persistent'] else self.tr('（仅内存；退出后丢失）')),
                'Context',
            )
            return
        if kind in ("subagent_start", "subagent_end"):
            detail = (f"model={payload['model']} · steps≤{payload['max_steps']}"
                      if kind == "subagent_start" else payload["status"])
            self.display(f"{payload['id']} · {detail}", "Sub")
            return
        if kind == "subagent_retry":
            self.retry_output(payload, payload["id"])
            return
        if kind == "continuation":
            self.display({"automatic_continuation": payload})
            return
        name = payload.get("name")
        writing = name in WRITE_TOOLS
        if self.debug or kind == "tool_start" or (writing and kind == "tool_result"):
            visible = hide_write_payloads(payload) if self.config.silent_writes else payload
            self.display({kind: visible}, "SubTool" if payload.get("subagent") else "Tool")
        if kind == "tool_start":
            self.tool_started[payload.get("name")] = time.monotonic()
        elif kind == "tool_result":
            elapsed = time.monotonic() - self.tool_started.pop(payload.get("name"), time.monotonic())
            if elapsed >= 10 and shutil.which("termux-vibrate"):
                try:
                    subprocess.Popen(["termux-vibrate", "-d", "100"], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_environment())
                except OSError:
                    pass

    def stream_output(self, kind, value):
        safe = "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" for c in value)
        self.formatter.feed("Think" if kind == "reasoning" else "Agent", self.redact(safe))
        self.stream_wrote = True
        if self.store and self.revision and self.active_branch:
            field = 'reasoning_content' if kind == 'reasoning' else 'content'
            combined = self.stream_draft.get(field, '') + self.redact(value)
            if len(combined) > 64000:
                self.stream_draft['truncated'] = True
            self.stream_draft[field] = combined[-64000:]
            now = time.monotonic()
            if not self.draft_saved_at or now - self.draft_saved_at >= 2:
                self.store.save_draft(self.session_id, self.revision, self.stream_draft)
                self.draft_saved_at = now

    def show_answer(self, value):
        streamed = getattr(self.agent.client, "streamed", False)
        if self.stream_wrote:
            self.formatter.finish()
        stopped = getattr(self.agent, 'last_run', {}).get("status") in (
            "limit", "incomplete", "context_limit", "blocked", "stalled", "error", "empty_response")
        failure = stopped or value.startswith(("LLM 请求失败", "LLM 请求已中断", "已达到最大模型请求",
                                                "同一任务的硬预算", "自动任务", "任务因审批", "工具执行已中断"))
        if value and (not streamed or not getattr(self.agent.client, "streamed_text", False) or failure):
            self.display(value, "Agent")
        self.stream_wrote = False

    def retry_output(self, info, label="API"):
        cause = f"HTTP {info['status']}" if info.get("status") else info["cause"]
        self.display(
            self.tr('{v0} retry {v1}/{v2} · {v3} · wait {v4:g}s',
                    v0=label, v1=info['attempt'], v2=info['max_attempts'], v3=cause, v4=info['wait_seconds']),
            "Retry",
        )

    def build_agent(self, next_config, text, base_system=None, client=None):
        kwargs = {"approve": self.approve, "event": self.event}
        if text:
            kwargs["instructions"] = text
        if base_system is not None:
            kwargs["base_system"] = base_system
        if client is not None:
            kwargs['client'] = client
        built = Agent(next_config, **kwargs)
        built.client.on_stream = self.stream_output
        built.client.on_retry = self.retry_output
        built.checkpoint = lambda: self.save_current(checkpoint=True)
        if not self.options.no_session:
            built.archive_after_compact = lambda *args: self.save_compaction(*args)
        return built

    def history_mode(self, current=None):
        return getattr(self.agent if current is None else current, 'history_only', False) is True

    def current_scope(self):
        return {"directory": str(self.directory), "config_path": self.config_path,
                "session_file": self.session_file, "disabled": self.disabled,
                "instructions": self.instructions, "instruction_path": self.instruction_path}

    @staticmethod
    def payload_fingerprint(value):
        # The marker describes how a snapshot was written, not changed task data.
        # A finish checkpoint must deduplicate the following ordinary save too.
        data = {k: v for k, v in value.items() if k != 'checkpoint'}
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(encoded.encode('utf-8')).hexdigest()

    def current_branch(self, archive_id=None):
        value = dict(self.branch_state)
        if not self.active_branch:
            value.setdefault('source_session_id', self.session_id)
            for name in ('title', 'updated', 'revision'):
                value.setdefault('source_' + name, (self.source_meta or {}).get(name))
        value['kind'] = 'active'
        if archive_id:
            value['archive_session_id'] = archive_id
        return value

    def create_active_branch(self, value, title, archive_id=None, draft=None):
        identifier = new_id()
        branch = self.current_branch(archive_id)
        value['branch'] = branch
        self.store.save(identifier, 0, value, title=title)
        if draft:
            self.store.save_draft(identifier, 1, draft)
        self.session_id, self.revision = identifier, 1
        self.active_branch = True
        self.loaded_row = None
        self.source_fingerprint = None
        self.branch_state = branch
        self.source_meta = {'title': branch.get('source_title'), 'updated': branch.get('source_updated'),
                            'revision': branch.get('source_revision')}
        self.saved_fingerprint = self.payload_fingerprint(value)
        if self.agent is not None:
            self.agent.archive_id = identifier
        return identifier

    def save_current(self, force=False, checkpoint=False):
        if self.agent is not None and self.history_mode():
            return
        if self.store and self.agent is not None and (force or self.revision or len(self.agent.messages) > 1):
            branch = self.current_branch()
            value = snapshot(self.agent, self.current_scope(), checkpoint=checkpoint, branch=branch)
            fingerprint = self.payload_fingerprint(value)
            if not force and not checkpoint:
                if fingerprint == self.saved_fingerprint:
                    return
                if not self.active_branch:
                    plain = {k: v for k, v in value.items() if k != 'branch'}
                    if self.payload_fingerprint(plain) == self.source_fingerprint:
                        return
            clear_draft = checkpoint and getattr(self.agent, 'last_run', {}).get('status') not in ('error', 'interrupted')
            self.revision = self.store.save(self.session_id, self.revision, value, clear_draft=clear_draft)
            if not self.active_branch:
                self.active_branch, self.loaded_row = True, None
                self.branch_state = branch
            self.saved_fingerprint = fingerprint
            # Fresh sessions need a real persisted archive ID before their first
            # automatic compaction; in-memory IDs are invalid SQLite session IDs.
            self.agent.archive_id = self.session_id

    def save_compaction(self, archive_id, source, messages):
        scope = self.current_scope()
        branch = self.current_branch()
        original = snapshot(self.agent, scope, branch=branch, messages=source)
        reduced = snapshot(self.agent, scope, checkpoint=True, branch=branch, messages=messages)
        # Include the transition counters in the same commit as the new context.
        reduced['counters']['_task_compactions'] += 1
        if reduced['task_state'] is not None:
            reduced['task_state']['compactions'] += 1
        fingerprint = self.payload_fingerprint(reduced)
        self.revision = self.store.compact(self.session_id, self.revision, archive_id, original, reduced)
        self.saved_fingerprint = fingerprint
        self.active_branch, self.loaded_row, self.source_fingerprint = True, None, None
        self.branch_state = branch
        self.agent.archive_id = self.session_id

    def show_interruption(self, exc):
        if self.stream_wrote:
            self.formatter.finish()
            self.stream_wrote = False
        self.display(self.tr('Interrupted.'))
        for note in getattr(exc, '__notes__', ()):
            if note.startswith('Interruption checkpoint failed: '):
                self.display(
                    self.tr('中断检查点保存失败（{v0}）；请核实最新状态。',
                            v0=note.removeprefix('Interruption checkpoint failed: ')),
                    'Recovery',
                )

    def submit_input(self, value, automatic=None):
        if self.history_mode():
            self.display(self.tr('当前为历史查看模式；请用 /api NAME 选择可用 API 并新建会话。'), 'History')
            return False
        self.read_grant = False
        try:
            submission = load_text_submission(value, COMMANDS, self.config.max_input_file_chars)
            if submission is not None:
                self.display({"file": str(submission.path), "chars": len(submission.text),
                              "api": self.config.api_profile})
                value = submission.text
            answer = self.agent.run(value) if automatic is None else self.agent.run(value, auto_continue=automatic)
            self.show_answer(answer)
            self.save_current()
            return True
        except KeyboardInterrupt as exc:
            self.show_interruption(exc)
        except Exception as exc:
            if self.stream_wrote:
                self.formatter.finish()
                self.stream_wrote = False
            self.display(self.tr('Error: {v0}', v0=exc))
        return False

    def current_title(self):
        try:
            title = self.store.title(self.session_id) if self.store and self.revision else None
        except sqlite3.Error:
            return '[unavailable]'
        return ' '.join(str(title).split()) if title else 'New session'

    def rename_current(self, value):
        title = validate_title(value)
        self.save_current(force=True)
        self.store.rename(self.session_id, title, self.revision)
        self.display(f"{self.session_id} · {title}", "Session")

    def reset_session_io(self):
        self.stream_draft, self.draft_saved_at, self.read_grant = {}, 0.0, False
        self.model_catalog = None
        self.tool_started.clear()

    def reset_session_tracking(self, branch=None):
        self.session_id, self.revision = new_id(), 0
        self.active_branch, self.loaded_row, self.source_fingerprint = True, None, None
        self.branch_state, self.saved_fingerprint = branch or {'kind': 'active'}, None
        self.source_meta = None
        self.agent.archive_id = None
        self.reset_session_io()

    def start_new_session(self, next_config, next_instructions):
        """Publish a fully built replacement only after saving the current session."""
        replacement = self.build_agent(next_config, next_instructions)
        try:
            self.save_current()
            getattr(self.agent, 'close', self.agent.client.close)()
        except BaseException as primary:
            try:
                getattr(replacement, 'close', replacement.client.close)()
            except BaseException as cleanup:
                primary.add_note('Replacement cleanup failed: ' + type(cleanup).__name__)
            raise
        self.config, self.agent = next_config, replacement
        self.language, self.instructions = self.config.ui_language, next_instructions
        self.registry = SkillRegistry()
        self.reset_session_tracking()

    @staticmethod
    def catalog_identity(candidate):
        return (candidate.api_profile, candidate.base_url.rstrip('/'), candidate.wire_api, candidate.api_key)

    def switch_api(self, name, persist=False):
        candidate = Config.load(self.config_path, name)
        self.model_catalog = None
        if (self.catalog_identity(candidate) == self.catalog_identity(self.config)
                and candidate.model == self.config.model
                and not self.config.profile_model and not self.history_mode()):
            if persist:
                self.profile_store.set_default(name)
            return False
        if persist:
            self.profile_store.set_default(name)
        self.start_new_session(candidate, self.instructions)
        return True

    @staticmethod
    def fetch_models(candidate):
        query = Client(candidate)
        try:
            return tuple(query.list_models())
        finally:
            query.close()

    def show_models(self, name):
        self.model_catalog = None
        candidate = Config.load(self.config_path, name)
        models = self.fetch_models(candidate)
        self.model_catalog = (candidate, models)
        selected = self.config.model if name == self.config.api_profile else candidate.model
        lines = [self.tr('API={v0} · 可用模型 {v1} 个', v0=name, v1=len(models)),
                 candidate.base_url.rstrip('/') + '/models']
        lines.extend(f'{i:>3} {"*" if model == selected else " "} {model}' for i, model in enumerate(models, 1))
        if selected not in models:
            lines.append(self.tr('配置中的模型 {v0} 不在当前返回列表中。', v0=selected))
        lines.append(self.tr('用 /api model ID或序号 选用模型。'))
        self.display('\n'.join(lines), 'Models')

    def select_model(self, value):
        parts = shlex.split(value)
        if not 1 <= len(parts) <= 2:
            raise ValueError(self.tr('Use /api model ID|N [NAME]'))
        choice = parts[0]
        catalog = self.model_catalog
        name = catalog[0].api_profile if catalog else self.config.api_profile
        if len(parts) == 2:
            name = parts[1]
        candidate = Config.load(self.config_path, name)
        if catalog and self.catalog_identity(catalog[0]) == self.catalog_identity(candidate):
            models = catalog[1]
        else:
            if choice.isascii() and choice.isdigit():
                raise ValueError(self.tr('先用 /api models [NAME] 查询，再按列表序号选用模型。'))
            models = self.fetch_models(candidate)
            catalog = None
        if choice in models:
            selected = choice
        elif choice.isascii() and choice.isdigit() and 1 <= int(choice) <= len(models):
            selected = models[int(choice) - 1]
        else:
            raise ValueError(self.tr('模型不在当前列表中；请用 /api models [NAME] 重新查询。'))
        same_api = self.catalog_identity(candidate) == self.catalog_identity(self.config) and not self.history_mode()
        profile_model = candidate.model if selected != candidate.model else ''
        if same_api and selected == self.config.model and self.config.profile_model == profile_model:
            return False
        replacement = copy.copy(self.config) if same_api else candidate
        replacement.profile_model = profile_model
        replacement.model = selected
        replacement.validate()
        self.start_new_session(replacement, self.instructions)
        self.model_catalog = catalog
        return True

    def show_session_listing(self, limit=50, query=None, empty_text=None, all_rows=False):
        """Query, render and cache one exact metadata listing without loading rows."""
        empty_text = self.tr(empty_text or '没有已保存会话。')
        if not self.store:
            self.display(self.tr('Persistence disabled by --no-session.'))
            self.last_session_ids = ()
            return ()
        rows = self.store.listing(limit, query=query, all_rows=all_rows, active_id=self.session_id)
        self.last_session_ids = tuple(row['id'] for row in rows)
        if not self.formatter.enabled:
            self.display(rows)
            if not rows and empty_text:
                self.display(empty_text)
        else:
            if rows:
                self.display(
                    self.tr('{v0} saved sessions', v0=len(rows))
                    + (self.tr(' · title contains {v0}', v0=query) if query else ''),
                    'Sessions',
                )
            else:
                self.display(empty_text, 'Sessions')
            self.formatter.sessions(rows, self.session_id, Path.home(), self.redact, language=self.language)
        return self.last_session_ids

    def resolve_session_target(self, value):
        value = value.strip()
        if not value:
            raise ValueError(self.tr('Use /resume ID|N or /session load ID|N'))
        if len(value) == 12 and all(char in "0123456789abcdef" for char in value):
            return value
        if value.isascii() and value.isdigit():
            if not self.last_session_ids:
                raise ValueError(self.tr('先用 /sessions 或 /resume 查看会话，再按序号选择。'))
            index = int(value)
            if not 1 <= index <= len(self.last_session_ids):
                raise ValueError(self.tr('会话序号超出最后一次显示的列表范围。'))
            return self.last_session_ids[index - 1]
        self.store.check_id(value)
        return value

    def resume_session(self, value):
        identifier = self.resolve_session_target(value)
        if identifier != self.session_id or self.history_mode():
            self.load_session(identifier)
        self.display({"id": self.session_id, "title": self.current_title(),
                      "api": self.config.api_profile, "directory": str(self.directory)})

    def resume_picker(self):
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            self.display(self.tr('输入 /resume 序号或 ID 恢复；序号只对应上面显示的列表。'))
            return
        try:
            value = input(self.tr('选择序号或ID（回车取消）')).strip()
        except (EOFError, KeyboardInterrupt):
            self.display(self.tr('已取消会话选择。'))
            return
        if not value:
            self.display(self.tr('已取消会话选择。'))
            return
        self.resume_session(value)

    def delete_session(self, value):
        parts = value.split()
        if not parts:
            rows = self.show_session_listing(50, empty_text="没有已保存会话。")
            if not rows:
                return
            if not (sys.stdin.isatty() and sys.stdout.isatty()):
                self.display(self.tr('用 /session delete 序号或ID 删除；非交互执行需显式添加 --yes。'))
                return
            try:
                selection = input(self.tr('删除序号或ID（回车取消）')).strip()
            except (EOFError, KeyboardInterrupt):
                self.display(self.tr('已取消删除。'))
                return
            if not selection:
                self.display(self.tr('已取消删除。'))
                return
            parts = [selection]
        if len(parts) > 2 or parts[0] == '--yes' or (len(parts) == 2 and parts[1] != '--yes'):
            raise ValueError(self.tr('Use /session delete ID|N [--yes]'))
        target = self.resolve_session_target(parts[0])
        if target == self.session_id:
            raise ValueError(self.tr('Switch to another session before deleting the active one'))
        metadata = self.store.deletion_target(target)
        # A title is display data, not a prompt or a control sequence.
        confirmation = {"delete_session": target, "title": metadata[0]}
        archives = self.store.archives(target)
        if archives:
            confirmation['delete_archives'] = len(archives)
        self.display(confirmation, "Session")
        confirmed = len(parts) == 2
        if not confirmed and sys.stdin.isatty():
            try:
                confirmed = input(self.tr('Delete session {v0}? [y/N] ', v0=target)).strip().lower() in ('y', 'yes')
            except (EOFError, KeyboardInterrupt):
                confirmed = False
        if confirmed:
            self.store.delete(target, expected=metadata)
            self.display(self.tr('Session deleted.'))
        else:
            self.display(self.tr('Deletion cancelled; use --yes for explicit noninteractive deletion.'))

    def load_session(self, identifier, save_existing=True, active=False):
        with self.store._read_snapshot():
            self.loaded_row = self.store.db.execute(
                'SELECT title,updated,revision FROM sessions WHERE id=?', (identifier,)).fetchone()
            next_revision, payload = self.store.load(identifier)
        next_config, replacement, scope = restore(payload, self.build_agent, history_fallback=True)
        target_directory = Path(scope['directory'])
        if self.history_mode(replacement) and not target_directory.is_dir():
            target_directory = Path.cwd()
        try:
            if save_existing:
                self.save_current()
            os.chdir(target_directory)
        except Exception:
            getattr(replacement, 'close', replacement.client.close)()
            raise
        if self.agent is not None:
            getattr(self.agent, 'close', self.agent.client.close)()
        self.config, self.agent = next_config, replacement
        self.language = self.config.ui_language
        self.directory, self.config_path = target_directory, scope['config_path']
        self.session_file, self.disabled = scope['session_file'], scope['disabled']
        self.instructions, self.instruction_path = scope['instructions'], scope['instruction_path']
        self.session_id, self.revision = identifier, next_revision
        self.active_branch = active
        self.branch_state = dict(payload.get('branch') or {'kind': 'active'})
        current_snap = snapshot(replacement, self.current_scope(), branch=self.branch_state)
        if self.active_branch:
            self.loaded_row = None
            self.source_fingerprint = None
        else:
            self.source_meta = {
                'title': self.loaded_row[0], 'updated': self.loaded_row[1], 'revision': self.loaded_row[2],
            }
            self.source_fingerprint = self.payload_fingerprint({k: v for k, v in current_snap.items() if k != 'branch'})
        self.saved_fingerprint = self.payload_fingerprint(current_snap)
        replacement.archive_id = identifier
        self.reset_session_io()
        self.registry = SkillRegistry()
        self.profile_store = ProfileStore(self.config_path)
        if self.history_mode():
            self.display(
                self.tr('历史查看模式：') + self.agent.client.reason
                + self.tr('可用 /history、/recall、/export 查看；用 /api NAME 新建会话，原历史保持保存。'),
                'History',
            )
        draft = self.store.draft(identifier)
        incomplete = self.agent.incomplete_draft
        unfinished_checkpoint = payload.get('checkpoint') and self.agent.last_run.get('status') not in ('complete', 'idle')
        if unfinished_checkpoint or draft or incomplete or has_unknown_tool_results(self.agent.messages):
            self.display(self.tr('恢复了未完成任务的检查点；工具状态不明时先核实实际结果。'), 'Recovery')
        if draft:
            self.display(
                self.tr('上次流式草稿（未完成，未加入模型上下文；/export 可完整导出）：\n')
                + (draft.get('content') or draft.get('reasoning_content') or '')[:4000],
                'Draft',
            )
        if incomplete:
            text = incomplete.get('content') or incomplete.get('reasoning_content') or ''
            self.display(
                self.tr('恢复了未完成输出草稿；未完成工具调用只保留在草稿中，未加入模型上下文：\n') + text[:4000],
                'Draft',
            )

    def compact_current(self, keep):
        from .context import compact_messages
        if not 1 <= keep <= 100:
            raise ValueError(self.tr('Use /compact [1–100 recent user turns]'))
        original = self.session_id
        title = self.current_title()
        payload = snapshot(self.agent, self.current_scope())
        messages, omitted = compact_messages(payload['messages'], keep, original)
        if not omitted:
            self.display(self.tr('消息轮次未超过保留数量，无需精简。'), 'Context')
            return
        payload['messages'] = messages
        identifier = new_id()
        self.store.save(identifier, 0, payload)
        self.store.rename(identifier, title[:100] + ' · compact')
        self.load_session(identifier, save_existing=False, active=True)
        self.display(
            self.tr('完整历史保留于 {v0}；当前会话 {v1} 已省略 {v2} 条早期消息。',
                    v0=original, v1=identifier, v2=omitted),
            'Context',
        )

    def _print_startup_banner(self):
        self.display(self.tr('Termux Agent — /help for commands'))
        self.display({"api": self.config.api_profile, "directory": str(self.directory),
                      "agent_file": self.instruction_path,
                      "session": self.session_id if self.store else "in-memory"})
        # Effective limits of the active profile: a key placed under another [apis.*]
        # table silently falls back to defaults, and this line makes that visible.
        self.display({
            "auto_continue": self.config.auto_continue,
            "max_steps": self.config.max_steps,
            "max_task_calls": self.config.max_task_calls,
            "max_task_seconds": self.config.max_task_seconds,
            "auto_resume": self.config.auto_resume_max if self.config.auto_continue and self.config.auto_resume else 0,
        })
        if self.formatter.enabled:
            self.display(self.tr('Tab 补全命令和 ~/本地路径；文件路径回车提交文本。'))

    def _cmd_compact(self, value):
        try:
            if not self.store:
                raise ValueError(self.tr('/compact 需要会话持久化，请去掉 --no-session'))
            if value == 'task' or value.startswith('task '):
                from .context import compact_task_messages
                keep = int(value[4:].strip() or 2)
                original = self.session_id
                title = self.current_title()
                payload = snapshot(self.agent, self.current_scope())
                messages, details = compact_task_messages(payload['messages'], keep, original)
                if not details['omitted_groups'] and not details['reduced_calls']:
                    self.display(
                        self.tr('当前任务没有可压缩的完整工具组；用 /compact 精简更早的历史，或 /compact 1 仅保留最近一轮。'),
                        'Context',
                    )
                else:
                    payload['messages'] = messages
                    identifier = self.create_active_branch(
                        payload, title[:95] + ' · compact task', archive_id=original,
                        draft=self.store.draft(original))
                    self.load_session(identifier, save_existing=False, active=True)
                    self.display(
                        self.tr('完整任务历史保留于 {v0}；当前会话 {v1}：{v2}',
                                v0=original, v1=identifier, v2=details),
                        'Context',
                    )
            else:
                if value:
                    self.compact_current(int(value))
                elif not self.agent._compact_for_context(force=True):
                    self.display(self.tr('没有可安全精简的历史；可用 /compact 1 仅保留最近一轮。'), 'Context')
        except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
            self.display(self.tr('Error: {v0}', v0=exc))

    def _cmd_api(self, value):
        try:
            rows = self.profile_store.list()
            if value == 'models' or value.startswith('models '):
                self.show_models(value[6:].strip() or self.config.api_profile)
            elif value == 'model' or value.startswith('model '):
                choice = value[5:].strip()
                switched = self.select_model(choice) if choice else False
                self.display({'api': self.config.api_profile, 'model': self.config.model,
                              'switched': switched, 'session': self.session_id}, 'Models')
            elif not value or value == "list":
                self.display({"current": self.config.api_profile, "default": self.profile_store.get_default(),
                              "profiles": [{"number": i, **row} for i, row in enumerate(rows, 1)]})
            elif value.startswith("add "):
                parts = shlex.split(value[4:])
                flags = [part for part in parts if part.startswith("--")]
                positional = [part for part in parts if not part.startswith("--")]
                if not 3 <= len(positional) <= 5:
                    raise ValueError(self.tr('Use /api add NAME URL MODEL [KEY_ENV] [local|cloud]'))
                name, base_url, model = positional[:3]
                extra = positional[3:]
                key_env = ""
                kind = "auto"
                if extra:
                    if extra[0] in ("local", "cloud"):
                        kind = extra[0]
                    else:
                        key_env = extra[0]
                if len(extra) == 2:
                    kind = extra[1]
                stream = True
                timeout = None
                user_agent = None
                for flag in flags:
                    if flag == "--no-stream":
                        stream = False
                    elif flag.startswith("--timeout="):
                        timeout = float(flag.partition("=")[2])
                    elif flag.startswith("--user-agent="):
                        user_agent = flag.partition("=")[2]
                    else:
                        raise ValueError(self.tr('Unknown API option: {v0}', v0=flag))
                path = self.profile_store.add(name, base_url, model, key_env, kind,
                                              stream, timeout, user_agent)
                self.display({"added": name, "path": str(path), "switch": f"/api {name}"})
            elif value.startswith("show "):
                name = value[5:].strip()
                matches = [row for row in rows if row["name"] == name]
                if name == "default":
                    self.display(Config.load(self.config_path, name).public_dict())
                elif matches:
                    self.display(matches[0])
                else:
                    raise ValueError(self.tr('Unknown API profile: {v0}', v0=name))
            elif value.startswith("remove "):
                name = value[7:].strip()
                if name == self.config.api_profile:
                    raise ValueError(self.tr('Switch to another API before removing the active profile'))
                backup = self.profile_store.remove(name)
                self.display({"removed": name, "backup": str(backup)})
            else:
                persist = value.startswith("default ")
                names = [row["name"] for row in rows]
                name = value[8:].strip() if persist else value.removeprefix("switch ").strip()
                if name in ("next", "prev"):
                    if not names:
                        raise ValueError(self.tr('No named API profiles configured'))
                    index = names.index(self.config.api_profile) if self.config.api_profile in names else (
                        -1 if name == "next" else 0)
                    name = names[(index + (1 if name == "next" else -1)) % len(names)]
                elif name.isdigit():
                    number = int(name)
                    if not 1 <= number <= len(names):
                        raise ValueError(self.tr('API number is not in /api list'))
                    name = names[number - 1]
                switched = self.switch_api(name, persist)
                self.display({"api": name, "switched": switched,
                              "default": self.profile_store.get_default()})
        except (ValueError, OSError, sqlite3.Error, ClientError) as exc:
            self.display(self.tr('Error: {v0}', v0=exc))

    def _cmd_agent(self, value):
        if not value:
            self.display({"agent_file": self.instruction_path, "chars": len(self.instructions)})
            return
        try:
            next_file, next_disabled = self.session_file, self.disabled
            if value == "off":
                next_disabled = True
            elif value == "directory":
                next_file, next_disabled = None, False
            elif value != "reload":
                next_file, next_disabled = value, False
            text, source = load_instructions(self.directory, next_file, next_disabled)
            self.start_new_session(self.config, text)
            self.instructions, self.instruction_path = text, source
            self.session_file, self.disabled = next_file, next_disabled
            self.display({"agent_file": source, "chars": len(text), "new_conversation": True})
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.display(self.tr('Error: {v0}', v0=exc))

    def _cmd_skill(self, prompt):
        try:
            parts = prompt.split(maxsplit=2)
            action = parts[1] if len(parts) > 1 else "list"
            name = parts[2] if len(parts) > 2 else ""
            if action == "list":
                active = self.agent.get_active_skills()
                self.display([{"name": skill.name, "active": skill.name in active,
                               "description": skill.description} for skill in self.registry.list_skills()])
            elif action == "info" and name:
                skill = self.registry.find(name)
                self.display({"name": skill.name, "description": skill.description, "path": str(skill.path),
                              "active": name in self.agent.active_skills})
            elif action == "load" and name:
                skill, body = self.registry.load(name)
                previous = self.agent.get_active_skills()
                self.agent.load_skill(skill.name, body)
                try:
                    self.save_current(force=True)
                except Exception:
                    self.agent.clear_skills()
                    for active_name, content in previous.items():
                        self.agent.load_skill(active_name, content)
                    raise
                self.display(self.tr('Loaded {v0} into this session.', v0=skill.name))
            elif action in ("unload", "clear"):
                if action == "unload" and not name:
                    raise ValueError(self.tr('Use /skill unload NAME'))
                previous = self.agent.get_active_skills()
                if action == "clear":
                    self.agent.clear_skills()
                else:
                    self.agent.unload_skill(name)
                try:
                    self.save_current(force=True)
                except Exception:
                    self.agent.clear_skills()
                    for active_name, content in previous.items():
                        self.agent.load_skill(active_name, content)
                    raise
                self.display(self.tr('Active skills cleared.') if action == "clear"
                             else self.tr('Unloaded {v0}.', v0=name))
            else:
                self.display(self.tr('Use /skill list|info NAME|load NAME|unload NAME|clear'))
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.display(self.tr('Error: {v0}', v0=exc))

    def _cmd_session(self, prompt):
        if not self.store:
            self.display(self.tr('Persistence disabled by --no-session.'))
            return
        command, _, value = prompt[8:].strip().partition(" ")
        try:
            if not command:
                self.display({"id": self.session_id, "title": self.current_title(), "saved": bool(self.revision)})
            elif command in ("new", "fork"):
                if self.history_mode():
                    raise ValueError(self.tr('历史查看模式保留原快照；请用 /api NAME 新建可用会话。'))
                if command == "new":
                    self.start_new_session(self.config, self.instructions)
                else:
                    self.save_current()
                    self.reset_session_tracking({'kind': 'active', 'source_session_id': self.session_id})
                self.save_current(force=True)
                if value:
                    self.store.rename(self.session_id, value)
                self.display({"id": self.session_id, "title": self.current_title()})
            elif command == "load":
                self.resume_session(value)
            elif command == 'archives':
                identifier = self.resolve_session_target(value) if value.strip() else self.session_id
                self.display(self.store.archives(identifier), 'Archive')
            elif command == 'archive':
                archive = self.store.load_archive(value.strip())
                self.display({k: archive[k] for k in ('id', 'session_id', 'title', 'revision')}, 'Archive')
                for message in archive['payload']['messages'][1:][-20:]:
                    role, rendered = history_entry(message, self.config.silent_writes)
                    self.display(rendered, role, stamp='--:--')
            elif command == "rename":
                self.rename_current(value)
            elif command == "rename-id":
                target, _, title = value.partition(' ')
                title = validate_title(title)
                if target == self.session_id:
                    self.rename_current(title)
                else:
                    self.store.rename(target, title)
                    self.display(f"{target} · {title}", 'Session')
            elif command == "save":
                self.save_current(force=True)
                self.display(self.tr('历史已保存；查看模式不重写原快照。') if self.history_mode()
                             else {"id": self.session_id, "saved": True})
            elif command == "delete":
                self.delete_session(value)
            else:
                self.display(self.tr('Use /session new|fork|load ID|N|rename NAME|rename-id ID NAME|delete ID|save'))
        except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
            self.display(self.tr('Error: {v0}', v0=exc))

    def _cmd_copy(self, prompt):
        try:
            text = prompt[5:].lstrip()
            if not text:
                text = next((m.get("content") for m in reversed(self.agent.messages)
                             if m.get("role") == "assistant" and m.get("content")), "")
            command = shutil.which("termux-clipboard-set")
            if not command:
                raise RuntimeError("termux-clipboard-set is unavailable; install Termux:API")
            result = subprocess.run([command], input=text, text=True, capture_output=True, timeout=10,
                                    env=child_environment())
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "clipboard write failed")
            self.display(self.tr('Copied to Android clipboard.') if text else self.tr('Nothing to copy.'))
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            self.display(self.tr('Error: {v0}', v0=exc))

    def _cmd_paste(self):
        command = shutil.which("termux-clipboard-get")
        if not command:
            self.display(self.tr('termux-clipboard-get is unavailable; install Termux:API'))
            return
        try:
            result = subprocess.run([command], text=True, capture_output=True, timeout=10, check=True,
                                    env=child_environment())
            if len(result.stdout) > self.config.max_output_chars:
                self.display(result.stdout[:self.config.max_output_chars] + self.tr('\n[TRUNCATED]'))
            else:
                self.display(result.stdout)
        except (OSError, subprocess.SubprocessError) as exc:
            self.display(self.tr('Clipboard read failed: {v0}', v0=exc))

    def _cmd_shell(self, command):
        if not command:
            return
        if self.agent._approved("shell", {"command": command}):
            from .tools import execute
            self.agent._direct_shell_count += 1
            result = execute("shell", {"command": command, "cwd": str(self.directory)}, self.config.max_output_chars)
            self.display(result, "Shell")
            try:
                self.save_current()
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.display(self.tr('Session save failed: {v0}', v0=exc))
        else:
            self.display(self.tr('Shell command denied or approval unavailable.'))

    def _dispatch_prompt(self, prompt):
        """Process one non-empty REPL line; return True only to exit loop."""
        bare_command = BARE_COMMANDS.get(prompt.lower())
        if bare_command and not (bare_command in ('/continue', '/resume') and self.agent._resumable()):
            self.display(self.tr(BARE_COMMAND_HINT, v0=prompt.lower(), v1=bare_command), 'Input')
            return False
        if self.history_mode() and prompt.split(maxsplit=1)[0] not in self._HISTORY_ALLOWED_COMMANDS:
            self.display(self.tr('当前为历史查看模式；请用 /api NAME 选择可用 API 并新建会话。'), 'History')
            return False
        try:
            local_path = path_value(prompt, self.cli_commands)
        except ValueError as exc:
            self.display(self.tr('Error: {v0}', v0=exc))
            return False
        if local_path is not None:
            self.submit_input(prompt)
            return False
        if prompt == "/exit":
            try:
                self.save_current()
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.display(self.tr('Save failed: {v0}', v0=exc))
                return False
            return True
        if prompt == "/help":
            self.display(help_text(HELP, self.language))
        elif prompt == "/clear":
            try:
                self.start_new_session(self.config, self.instructions)
                self.display(self.tr('New session; previous conversation remains saved.'))
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.display(self.tr('Error: {v0}', v0=exc))
        elif prompt == "/stats":
            self.display(self.agent.stats())
        elif prompt == '/status':
            self.display(self.agent.stats()['stop'])
        elif prompt == '/continue' or prompt.startswith('/continue '):
            try:
                value = prompt[9:].strip()
                answer = self.agent.continue_task(int(value) if value else None)
                self.show_answer(answer)
                self.save_current()
            except KeyboardInterrupt as exc:
                self.show_interruption(exc)
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.display(self.tr('Error: {v0}', v0=exc))
        elif prompt == '/compact' or prompt.startswith('/compact '):
            self._cmd_compact(prompt[8:].strip())
        elif prompt == '/recall' or prompt.startswith('/recall '):
            try:
                if not self.store:
                    raise ValueError(self.tr('/recall 需要会话持久化'))
                self.save_current()
                rows = self.store.recall(prompt[7:].strip(), silent_writes=self.config.silent_writes)
                self.display(rows if rows else self.tr('未找到匹配会话。'), 'Recall')
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.display(self.tr('Error: {v0}', v0=exc))
        elif prompt == '/export' or prompt.startswith('/export '):
            try:
                if not self.store:
                    raise ValueError(self.tr('/export 需要会话持久化'))
                args = shlex.split(prompt)[1:]
                target = self.session_id
                if args and args[0] not in ('markdown', 'json'):
                    target = args.pop(0)
                if len(args) > 2:
                    raise ValueError(self.tr('Use /export [ID] [markdown|json] [PATH]'))
                if target == self.session_id:
                    self.save_current(force=True)
                path = self.store.export(target, args[0] if args else 'markdown', args[1] if len(args) > 1 else None)
                self.display(self.tr('已导出：{v0}（本地明文，权限 0600）', v0=path), 'Export')
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.display(self.tr('Error: {v0}', v0=exc))
        elif prompt == "/config":
            self.display({**self.config.public_dict(), "directory": str(self.directory), "agent_file": self.instruction_path})
        elif prompt == "/debug":
            self.debug = not self.debug
            self.display(self.tr('Debug {v0}', v0='on' if self.debug else 'off'))
        elif prompt == "/debug-context":
            self.display(self.agent.debug_context())
        elif prompt == "/silent" or prompt.startswith("/silent "):
            value = prompt[7:].strip()
            if value not in ("", "on", "off"):
                self.display(self.tr('Use /silent on or /silent off'))
            else:
                if value:
                    self.config.silent_writes = value == "on"
                self.display({"silent_writes": self.config.silent_writes})
        elif prompt == "/auto" or prompt.startswith("/auto "):
            value = prompt[5:].strip()
            try:
                if value in ("on", "off"):
                    self.config.auto_continue = value == "on"
                elif value.startswith("steps "):
                    count = int(value[6:].strip())
                    if not 1 <= count <= 256:
                        raise ValueError(self.tr('Automatic request limit must be 1–256'))
                    self.config.max_steps = count
                elif value:
                    raise ValueError(self.tr('Use /auto on|off|steps N'))
                self.display({
                    "auto_continue": self.config.auto_continue,
                    "max_steps": self.config.max_steps,
                    "max_continuations": self.config.max_continuations,
                    "auto_resume": self.config.auto_resume_max if self.config.auto_continue and self.config.auto_resume else 0,
                })
            except ValueError as exc:
                self.display(self.tr('Error: {v0}', v0=exc))
        elif prompt == "/run" or prompt.startswith("/run "):
            task = prompt[4:].strip()
            if task:
                self.submit_input(task, automatic=True)
            else:
                self.display(self.tr('Use /run TASK or /run ~/task-instructions.txt'))
        elif prompt == "/api" or prompt.startswith("/api "):
            self._cmd_api(prompt[4:].strip())
        elif prompt == "/agent" or prompt.startswith("/agent "):
            self._cmd_agent(prompt[6:].strip())
        elif prompt == "/skill" or prompt.startswith("/skill "):
            self._cmd_skill(prompt)
        elif prompt.startswith("/copy"):
            self._cmd_copy(prompt)
        elif prompt == "/paste":
            self._cmd_paste()
        elif prompt == "/sub" or prompt.startswith("/sub "):
            task = prompt[4:].strip()
            if not task:
                self.display({"max_subagents": self.config.max_subagents,
                              "subagent_max_steps": min(self.config.subagent_max_steps, self.config.max_steps),
                              "api": self.config.api_profile, "execution": "synchronous; no nested delegation"}, "Sub")
                return False
            self.read_grant = False
            try:
                result = self.agent.delegate(task)
                self.display(f"{result.get('id', 'sub')} · {self.agent.last_run['status']}", "Sub")
                self.display(result.get("summary", result.get("error", "")), "Sub")
                self.save_current()
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.display(self.tr('Sub-agent failed: {v0}', v0=exc), "Sub")
        elif prompt.startswith("!"):
            self._cmd_shell(prompt[1:].strip())
        elif prompt == "/title" or prompt.startswith("/title "):
            if not self.store:
                self.display(self.tr('Persistence disabled by --no-session.'))
                return False
            try:
                value = prompt[6:].strip()
                if value:
                    self.rename_current(value)
                else:
                    self.display(f"{self.session_id} · {self.current_title()}", "Session")
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.display(self.tr('Error: {v0}', v0=exc))
        elif prompt == "/sessions" or prompt.startswith("/sessions "):
            try:
                value = prompt[9:].strip()
                query = None
                all_rows = value == 'all' or value.startswith('all ')
                if all_rows:
                    value = value[3:].strip()
                if value == "find" or value.startswith("find "):
                    if not value[4:].strip():
                        raise ValueError(self.tr('Use /sessions find TEXT'))
                    query = validate_title(value[4:].strip())
                    limit = 50
                else:
                    limit = int(value or 50)
                if not 1 <= limit <= 500:
                    raise ValueError(self.tr('Session count must be 1–500'))
                if not self.store:
                    self.display(self.tr('Persistence disabled by --no-session.'))
                    return False
                self.show_session_listing(limit, query, all_rows=all_rows)
            except (ValueError, sqlite3.Error) as exc:
                self.display(self.tr('Error: {v0}', v0=exc))
        elif prompt == "/resume" or prompt.startswith("/resume "):
            try:
                if not self.store:
                    self.display(self.tr('Persistence disabled by --no-session.'))
                    return False
                value = prompt[7:].strip()
                if value:
                    self.resume_session(value)
                else:
                    rows = self.show_session_listing(50, empty_text="没有已保存会话。")
                    if rows:
                        self.resume_picker()
            except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
                self.display(self.tr('Error: {v0}', v0=exc))
        elif prompt == "/history" or prompt.startswith("/history "):
            try:
                parts = prompt[8:].split()
                count, skip = int(parts[0]) if parts else 20, int(parts[1]) if len(parts) > 1 else 0
                if not 1 <= count <= 100 or skip < 0 or len(parts) > 2:
                    raise ValueError(self.tr('Use /history [1–100] [non-negative skip]'))
                history = self.agent.messages[1:]
                end = max(0, len(history) - skip)
                start = max(0, end - count)
                self.display(
                    self.tr('History {v0}–{v1} / {v2} · --:-- = time not recorded',
                            v0=start + 1 if start < end else 0, v1=end, v2=len(history)),
                )
                for message in history[start:end]:
                    role, rendered = history_entry(message, self.config.silent_writes)
                    self.display(rendered, role, stamp="--:--")
            except ValueError as exc:
                self.display(self.tr('Error: {v0}', v0=exc))
        elif prompt == "/session" or prompt.startswith("/session "):
            self._cmd_session(prompt)
        elif prompt.startswith("/"):
            self.display(self.tr('Unknown command; use /help.'))
        else:
            self.submit_input(prompt)
        return False

    def run(self):
        try:
            if self.options.session:
                self.load_session(self.options.session)
            if self.options.prompt is not None:
                if not self.submit_input(self.options.prompt):
                    raise SystemExit(1)
                return
            self._print_startup_banner()
            while True:
                try:
                    if self.formatter.enabled and sys.stdin.isatty():
                        session_label = self.session_id + (" (memory)" if self.store is None else "")
                        self.display(
                            self.tr('Session={v0} | Model={v1} | Cwd={v2}\nTitle={v3}',
                                    v0=session_label, v1=self.config.model,
                                    v2=self.directory, v3=self.current_title())
                            + (self.tr(' | Mode=历史查看') if self.history_mode() else '')
                            + '\n' + format_context_status(self.agent.context_status(), language=self.language),
                            "Status",
                        )
                    prompt = read_prompt(self.formatter.prompt(), self.formatter.continuation()).strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not prompt:
                    continue
                if self._dispatch_prompt(prompt):
                    break
        except Exception as exc:
            self.display(self.tr('Error: {v0}', v0=exc))
            raise SystemExit(1) from None
        finally:
            self.close()

    def close(self):
        try:
            self.save_current()
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.display(self.tr('Session save failed: {v0}; this turn was not saved.', v0=exc))
        if self.agent is not None:
            getattr(self.agent, 'close', self.agent.client.close)()
        save_readline_history()
        if self.store:
            self.store.close()


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--config')
    pre.add_argument('--session')
    preliminary, _unknown = pre.parse_known_args()
    try:
        language = Config.read_ui_language(preliminary.config)
    except (ValueError, OSError) as exc:
        if preliminary.session:
            language = 'zh'  # An invalid API config must not prevent local history access.
        else:
            pre.error(str(exc))

    def tr(template, **values):
        return ui_text(template, language, **values)

    parser = argparse.ArgumentParser(description=tr('轻量原生 Termux LLM Agent；交互命令帮助见 /help'))
    parser.add_argument("--config", help=tr('指定 TOML 配置文件路径'))
    parser.add_argument("--prompt", help=tr('执行一条提示词后退出'))
    parser.add_argument("--api", help=tr('选择配置中的 API 名称'))
    parser.add_argument("--directory", default=None, help=tr('设置工作目录，仅加载该目录的 agent.md'))
    parser.add_argument("--agent", help=tr('指定会话指令文件，替代目录 agent.md'))
    parser.add_argument("--no-agent", action="store_true", help=tr('不加载 agent.md 指令文件'))
    parser.add_argument("--session", help=tr('按 ID 恢复已保存会话及其作用域'))
    parser.add_argument("--no-session", action="store_true", help=tr('仅在内存中运行，不保存会话'))
    options = parser.parse_args()
    if options.prompt is None and not configure_readline(
            Path.home() / ".local/share/termux-agent/repl_history"):
        print(tr('Warning: Python readline is unavailable; terminal key editing may not work.'), file=sys.stderr)
    if options.session and (options.no_session or options.api or options.directory or options.agent or options.no_agent or options.config):
        parser.error(tr('--session restores its saved scope; do not combine it with scope overrides'))
    try:
        config_path = str(Path(options.config).expanduser().resolve()) if options.config else None
        directory = Path(options.directory or ".").expanduser().resolve()
        session_file, disabled = options.agent, options.no_agent
        if options.session:
            instructions, instruction_path, config = "", None, Config()
        else:
            instructions, instruction_path = load_instructions(directory, session_file, disabled)
            config = Config.load(config_path, options.api)
        language = config.ui_language
        os.chdir(directory)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))

    controller = ReplController(
        options,
        language=language,
        config_path=config_path,
        directory=directory,
        session_file=session_file,
        disabled=disabled,
        instructions=instructions,
        instruction_path=instruction_path,
        config=config,
        parser=parser,
    )
    controller.run()


if __name__ == "__main__":
    main()
