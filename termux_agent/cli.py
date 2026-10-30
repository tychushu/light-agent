"""Plain terminal REPL; no terminal UI framework."""
import argparse
import copy
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
from .config import Config
from .sessions import SessionStore, new_id, snapshot, restore, validate_title
from .skills import SkillRegistry
from .api_profiles import ProfileStore
from .terminal_ui import Columns, Completer, COMMANDS, format_context_status
from .local_input import load_text_submission, path_value
from .display_privacy import WRITE_TOOLS, hide_write_payloads, safe_tool_arguments
from .approval import reason as approval_reason
from .history import history_entry

HELP = """/help           显示命令帮助（Show commands）
Tab             补全命令、API 配置、会话 ID 和技能名称
~/path 或 /path  补全本地路径；回车将 UTF-8 文本文件内容提交给模型
/clear          新建空白会话，保留原会话历史
/compact [N]    自动精简跨轮次历史，保留用户指令；指定 N 则只保留最近 N 轮
/compact task [N]  压缩当前单任务工具链；保留最近 N 个完整工具组，不调用模型
/continue [N]   从检查点继续同一任务，开始一个新的受控模型调用段
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
/api add NAME URL MODEL [KEY_ENV] [local|cloud]  添加 API 配置
/api default NAME   设置以后启动时默认使用的 API 配置
/api show NAME      查看指定 API 配置，不显示密钥
/api remove NAME    删除通过 /api add 添加的 API 配置
/agent [path|off|directory|reload]  选择或重载目录/会话 agent.md；新建会话
/skill list|info|load|unload|clear   列出、查看、手动加载、卸载或清空技能
/copy [text]    复制指定文字或最后一次回答到 Android 剪贴板
/paste          显示 Android 剪贴板文字
!command        直接运行 Shell，不调用模型；审批规则仍生效
/sessions [N]   列出会话：时间、ID、标题、API / 模型、消息数、路径；默认 50，最多 500
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
/session delete ID   删除非当前会话，需要确认
/history [N] [skip]  查看最近 N 条消息，skip 可向前翻页
/sub TASK       直接委派一个子任务；/sub 查看执行上限
/exit           保存当前会话并退出
KEY_ENV 是密钥环境变量名；NAME / ID / TEXT 等是需要替换的参数。
"""


_readline = None
_history_file = None


def configure_readline(history_file=None):
    """Enable terminal editing for input(); no global tty changes or history file."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return True
    try:
        import readline
    except ImportError:
        return False
    global _readline, _history_file
    _readline = readline
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
    keys = {v for k, v in os.environ.items()
            if any(part in k.upper() for part in ("API_KEY", "API_TOKEN", "_SECRET")) and len(v) >= 4}
    values = []
    for i in range(1, _readline.get_current_history_length() + 1):
        item = _readline.get_history_item(i)
        if item:
            for key in keys:
                item = item.replace(key, "[REDACTED]")
            values.append(item)
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


def _trim_readline(start):
    if _readline:
        while _readline.get_current_history_length() > start:
            _readline.remove_history_item(start)


def _continued(line):
    slashes = len(line) - len(line.rstrip("\\"))
    return slashes % 2 == 1


def read_prompt(prompt="ta> ", continuation="... "):
    """Read ordinary or explicit triple-quote/backslash-continued input."""
    start = _readline.get_current_history_length() if _readline else 0
    first = input(prompt)
    quoted = first == '"""'
    if not quoted and not _continued(first):
        return first
    lines = [] if quoted else [first[:-1]]
    try:
        while True:
            line = input(continuation)
            if quoted and line == '"""':
                break
            continued = _continued(line)
            lines.append(line[:-1] if continued else line)
            if not quoted and not continued:
                break
    except BaseException:
        _trim_readline(start)
        raise
    _trim_readline(start)
    result = "\n".join(lines)
    if _readline:
        _readline.add_history(result)
    return result


def main():
    parser = argparse.ArgumentParser(description="轻量原生 Termux LLM Agent；交互命令帮助见 /help")
    parser.add_argument("--config", help="指定 TOML 配置文件路径")
    parser.add_argument("--prompt", help="执行一条提示词后退出")
    parser.add_argument("--api", help="选择配置中的 API 名称")
    parser.add_argument("--directory", default=None, help="设置工作目录，仅加载该目录的 agent.md")
    parser.add_argument("--agent", help="指定会话指令文件，替代目录 agent.md")
    parser.add_argument("--no-agent", action="store_true", help="不加载 agent.md 指令文件")
    parser.add_argument("--session", help="按 ID 恢复已保存会话及其作用域")
    parser.add_argument("--no-session", action="store_true", help="仅在内存中运行，不保存会话")
    options = parser.parse_args()
    if options.prompt is None and not configure_readline(
            Path.home() / ".local/share/termux-agent/repl_history"):
        print("Warning: Python readline is unavailable; terminal key editing may not work.", file=sys.stderr)
    if options.session and (options.no_session or options.api or options.directory or options.agent or options.no_agent or options.config):
        parser.error("--session restores its saved scope; do not combine it with scope overrides")
    try:
        config_path = str(Path(options.config).expanduser().resolve()) if options.config else None
        directory = Path(options.directory or ".").expanduser().resolve()
        session_file, disabled = options.agent, options.no_agent
        if options.session:
            instructions, instruction_path, config = "", None, Config()
        else:
            instructions, instruction_path = load_instructions(directory, session_file, disabled)
            config = Config.load(config_path, options.api)
        os.chdir(directory)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.error(str(exc))
    debug = False
    registry = SkillRegistry()  # Lazy: no scan/read until /skill is requested.
    profile_store = ProfileStore(config_path)
    tool_started = {}
    stream_wrote = False
    read_grant = False
    stream_draft = {}
    draft_saved_at = 0.0
    saved_fingerprint = None
    loaded_row = None
    source_meta = None
    active_branch = True
    source_fingerprint = None
    branch_state = {'kind': 'active'}
    formatter = Columns(sys.stdout, enabled=sys.stdout.isatty() and options.prompt is None)
    last_session_ids = ()
    cli_commands = tuple(dict.fromkeys((*COMMANDS, "/resume")))

    def redact(text):
        return text.replace(config.api_key, "[REDACTED]") if config.api_key else text

    def display(value, role="Info", stamp=None):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        # Do not let model/tool text inject terminal control sequences.
        text = ''.join(c if c in '\n\t' or c.isprintable() else f'\\x{ord(c):02x}' for c in text)
        formatter.message(role, redact(text), stamp=stamp)

    def approve(name, args):
        nonlocal read_grant
        if name == "read_file" and read_grant:
            return True
        visible = safe_tool_arguments(name, args) if config.silent_writes else args
        display({"approval_required": name, "arguments": visible,
                 "reason": approval_reason(name, args)}, "Approve")
        if not sys.stdin.isatty():
            display("Denied: approval requires an interactive terminal.")
            return False
        try:
            prompt = ("Allow this tool call? [y/N/a=all reads this task] "
                      if name == "read_file" else "Allow this tool call? [y/N] ")
            choice = input(prompt).strip().lower()
            if name == "read_file" and choice in ("a", "all"):
                read_grant = True
                return True
            return choice in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False

    def event(kind, payload):
        nonlocal stream_draft, draft_saved_at
        if kind == 'request_start':
            stream_draft, draft_saved_at = {}, 0.0
            return
        if kind == 'context_warning':
            display(f"上下文字符预算已用 {payload['chars'] / payload['limit']:.0%}；"
                    '可用 /compact 缩短上下文（字符预算不等于模型 token 窗口）。', 'Context')
            return
        if kind == 'step_start':
            if debug:
                display({"step": payload.get("step"), "segment": payload.get("segment"),
                         "task_logical_calls": payload.get("task_logical_calls"),
                         "max_task_logical_calls": payload.get("max_task_logical_calls")}, "Progress")
            return
        if kind == 'segment_extension':
            if debug:
                display({"segment_extension": payload}, "Progress")
            return
        if kind == 'task_compacted':
            display(f"已压缩上下文 {payload['before_chars']:,} → {payload['after_chars']:,} 字符；"
                    f"完整历史：{payload['archive']}" +
                    ('' if payload['persistent'] else '（仅内存；退出后丢失）'), 'Context')
            return
        if kind in ("subagent_start", "subagent_end"):
            detail = (f"model={payload['model']} · steps≤{payload['max_steps']}"
                      if kind == "subagent_start" else payload["status"])
            display(f"{payload['id']} · {detail}", "Sub")
            return
        if kind == "subagent_retry":
            retry_output(payload, payload["id"])
            return
        if kind == "continuation":
            display({"automatic_continuation": payload})
            return
        name = payload.get("name")
        writing = name in WRITE_TOOLS
        if debug or kind == "tool_start" or (writing and kind == "tool_result"):
            visible = hide_write_payloads(payload) if config.silent_writes else payload
            display({kind: visible}, "SubTool" if payload.get("subagent") else "Tool")
        if kind == "tool_start":
            tool_started[payload.get("name")] = time.monotonic()
        elif kind == "tool_result":
            elapsed = time.monotonic() - tool_started.pop(payload.get("name"), time.monotonic())
            if elapsed >= 10 and shutil.which("termux-vibrate"):
                try:
                    subprocess.Popen(["termux-vibrate", "-d", "100"], stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    pass

    def stream_output(kind, value):
        nonlocal stream_wrote, draft_saved_at
        safe = "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" for c in value)
        formatter.feed("Think" if kind == "reasoning" else "Agent", redact(safe))
        stream_wrote = True
        if store and revision and active_branch:
            field = 'reasoning_content' if kind == 'reasoning' else 'content'
            combined = stream_draft.get(field, '') + redact(value)
            if len(combined) > 64000:
                stream_draft['truncated'] = True
            stream_draft[field] = combined[-64000:]
            now = time.monotonic()
            if not draft_saved_at or now - draft_saved_at >= 2:
                store.save_draft(session_id, revision, stream_draft)
                draft_saved_at = now

    def show_answer(value):
        nonlocal stream_wrote
        streamed = getattr(agent.client, "streamed", False)
        if stream_wrote:
            formatter.finish()
        stopped = getattr(agent, 'last_run', {}).get("status") in (
            "limit", "incomplete", "context_limit", "blocked", "stalled", "error", "empty_response")
        failure = stopped or value.startswith(("LLM 请求失败", "LLM 请求已中断", "已达到最大模型请求",
                                                "同一任务的硬预算", "自动任务", "任务因审批", "工具执行已中断"))
        if value and (not streamed or not getattr(agent.client, "streamed_text", False) or failure):
            display(value, "Agent")
        stream_wrote = False

    def retry_output(info, label="API"):
        cause = f"HTTP {info['status']}" if info.get("status") else info["cause"]
        display(f"{label} retry {info['attempt']}/{info['max_attempts']} · "
                f"{cause} · wait {info['wait_seconds']:g}s", "Retry")

    def build_agent(next_config, text, base_system=None, client=None):
        kwargs = {"approve": approve, "event": event}
        if text:
            kwargs["instructions"] = text
        if base_system is not None:
            kwargs["base_system"] = base_system
        if client is not None:
            kwargs['client'] = client
        built = Agent(next_config, **kwargs)
        built.client.on_stream = stream_output
        built.client.on_retry = retry_output
        built.checkpoint = lambda: save_current(checkpoint=True)
        built.branch_after_compact = lambda archive_id, messages, details: branch_after_compact(
            archive_id, messages, details)
        return built

    try:
        agent = None if options.session else build_agent(config, instructions)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    try:
        store = None if options.no_session else SessionStore()
    except (OSError, sqlite3.Error) as exc:
        if agent is not None:
            agent.client.close()
        parser.error(f'Session store unavailable: {exc}')
    session_id, revision = new_id(), 0

    if _readline:
        completer = Completer(
            _readline,
            profiles=lambda: [row["name"] for row in profile_store.list()],
            skills=lambda: [skill.name for skill in registry.list_skills()],
            active_skills=lambda: list(agent.active_skills),
            session_ids=lambda: store.ids() if store else [],
            directory=lambda: directory,
            session_choices=lambda: [str(i) for i in range(1, len(last_session_ids) + 1)],
        )
        _readline.set_completer_delims(" \t\n")
        _readline.set_completer(completer)
        if "libedit" in (_readline.__doc__ or ""):
            _readline.parse_and_bind("bind ^I rl_complete")
        else:
            _readline.parse_and_bind("set show-all-if-ambiguous off")
            _readline.parse_and_bind("tab: menu-complete")

    def history_mode(current=None):
        return getattr(agent if current is None else current, 'history_only', False) is True

    def current_scope():
        return {"directory": str(directory), "config_path": config_path,
                "session_file": session_file, "disabled": disabled,
                "instructions": instructions, "instruction_path": instruction_path}

    def payload_fingerprint(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def current_branch(archive_id=None):
        value = dict(branch_state) if active_branch else {
            'kind': 'active',
            'source_session_id': session_id,
            'source_title': source_meta.get('title') if source_meta else None,
            'source_updated': source_meta.get('updated') if source_meta else None,
            'source_revision': source_meta.get('revision') if source_meta else None,
        }
        value['kind'] = 'active'
        if archive_id:
            value['archive_session_id'] = archive_id
        return value

    def create_active_branch(value, title, archive_id=None, draft=None):
        nonlocal session_id, revision, saved_fingerprint, active_branch
        nonlocal loaded_row, source_fingerprint, branch_state, source_meta
        identifier = new_id()
        branch = current_branch(archive_id)
        value['branch'] = branch
        store.save(identifier, 0, value, title=title)
        if draft:
            store.save_draft(identifier, 1, draft)
        session_id, revision = identifier, 1
        active_branch = True
        loaded_row = None
        source_fingerprint = None
        branch_state = branch
        source_meta = {'title': branch.get('source_title'), 'updated': branch.get('source_updated'),
                       'revision': branch.get('source_revision')}
        saved_fingerprint = payload_fingerprint(value)
        if agent is not None:
            agent.archive_id = identifier
        return identifier

    def save_current(force=False, checkpoint=False):
        nonlocal revision, saved_fingerprint, active_branch, loaded_row, branch_state
        if agent is not None and history_mode():
            return
        if store and agent is not None and (force or revision or len(agent.messages) > 1):
            value = snapshot(agent, current_scope(), checkpoint=checkpoint)
            fingerprint = payload_fingerprint(value)
            plain = snapshot(agent, current_scope(), checkpoint=checkpoint)
            plain.pop('branch', None)
            plain_fingerprint = payload_fingerprint(plain)
            unchanged_source = not active_branch and plain_fingerprint == source_fingerprint
            if not force and not checkpoint and (fingerprint == saved_fingerprint or unchanged_source):
                return
            clear_draft = checkpoint and getattr(agent, 'last_run', {}).get('status') not in ('error', 'interrupted')
            if not active_branch:
                branch = current_branch()
                value['branch'] = branch
                revision = store.save(session_id, revision, value, clear_draft=clear_draft)
                active_branch, loaded_row = True, None
                branch_state = branch
            else:
                value['branch'] = current_branch()
                revision = store.save(session_id, revision, value, clear_draft=clear_draft)
            saved_fingerprint = payload_fingerprint(value)

    def branch_after_compact(archive_id, messages, details):
        nonlocal session_id, revision, saved_fingerprint
        if not store:
            agent.in_memory_archives.append({'id': archive_id,
                                             'messages': copy.deepcopy(agent.messages)})
            return False
        title = current_title()
        value = snapshot(agent, current_scope(), branch=current_branch(archive_id))
        value['messages'] = copy.deepcopy(messages)
        draft = store.draft(archive_id)
        create_active_branch(value, title[:95] + ' · compact task', archive_id=archive_id, draft=draft)
        return True

    def submit_input(value, automatic=None):
        nonlocal stream_wrote, read_grant
        if history_mode():
            display('当前为历史查看模式；请用 /api NAME 选择可用 API 并新建会话。', 'History')
            return False
        read_grant = False
        try:
            submission = load_text_submission(value, COMMANDS, config.max_input_file_chars)
            if submission is not None:
                display({"file": str(submission.path), "chars": len(submission.text),
                         "api": config.api_profile})
                value = submission.text
            answer = agent.run(value) if automatic is None else agent.run(value, auto_continue=automatic)
            show_answer(answer)
            save_current()
            return True
        except KeyboardInterrupt:
            if stream_wrote:
                formatter.finish()
                stream_wrote = False
            display("Interrupted.")
        except Exception as exc:
            if stream_wrote:
                formatter.finish()
                stream_wrote = False
            display(f"Error: {exc}")
        return False

    def current_title():
        try:
            title = store.title(session_id) if store and revision else None
        except sqlite3.Error:
            return '[unavailable]'
        return ' '.join(str(title).split()) if title else 'New session'

    def rename_current(value):
        title = validate_title(value)
        save_current(force=True)
        store.rename(session_id, title, revision)
        display(f"{session_id} · {title}", "Session")

    def switch_api(name, persist=False):
        nonlocal config, agent, session_id, revision, saved_fingerprint
        nonlocal active_branch, loaded_row, source_fingerprint, branch_state
        candidate = Config.load(config_path, name)
        if name == config.api_profile and not history_mode():
            if persist:
                profile_store.set_default(name)
            return False
        save_current()
        if persist:
            profile_store.set_default(name)
        replacement = build_agent(candidate, instructions)
        getattr(agent, 'close', agent.client.close)()
        config, agent = candidate, replacement
        session_id, revision = new_id(), 0
        active_branch, loaded_row, source_fingerprint = True, None, None
        branch_state, saved_fingerprint = {'kind': 'active'}, None
        agent.archive_id = None
        return True

    def show_session_listing(limit=50, query=None, empty_text=None):
        """Query, render and cache one exact metadata listing without loading rows."""
        nonlocal last_session_ids
        if not store:
            display("Persistence disabled by --no-session.")
            last_session_ids = ()
            return ()
        rows = store.listing(limit, query=query) if query is not None else store.listing(limit)
        last_session_ids = tuple(row['id'] for row in rows)
        if not formatter.enabled:
            display(rows)
            if not rows and empty_text:
                display(empty_text)
        else:
            if rows:
                display(f"{len(rows)} saved sessions" +
                        (f" · title contains {query}" if query else ''), 'Sessions')
            else:
                display(empty_text, 'Sessions')
            formatter.sessions(rows, session_id, Path.home(), redact)
        return last_session_ids

    def resolve_session_target(value):
        value = value.strip()
        if not value:
            raise ValueError("Use /resume ID|N or /session load ID|N")
        if len(value) == 12 and all(char in "0123456789abcdef" for char in value):
            return value
        if value.isascii() and value.isdigit():
            if not last_session_ids:
                raise ValueError("先用 /sessions 或 /resume 查看会话，再按序号选择。")
            index = int(value)
            if not 1 <= index <= len(last_session_ids):
                raise ValueError("会话序号超出最后一次显示的列表范围。")
            return last_session_ids[index - 1]
        store.check_id(value)
        return value

    def resume_session(value):
        identifier = resolve_session_target(value)
        if identifier != session_id or history_mode():
            load_session(identifier)
        display({"id": session_id, "title": current_title(), "api": config.api_profile,
                 "directory": str(directory)})

    def resume_picker():
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            display("输入 /resume 序号或 ID 恢复；序号只对应上面显示的列表。")
            return
        try:
            value = input("选择序号或ID（回车取消）").strip()
        except (EOFError, KeyboardInterrupt):
            display("已取消会话选择。")
            return
        if not value:
            display("已取消会话选择。")
            return
        resume_session(value)

    def load_session(identifier, save_existing=True, active=False):
        nonlocal agent, config, directory, config_path, session_file, disabled
        nonlocal instructions, instruction_path, session_id, revision
        nonlocal registry, profile_store, saved_fingerprint, loaded_row
        nonlocal active_branch, source_fingerprint, branch_state, source_meta
        loaded_row = store.db.execute(
            'SELECT title,updated,revision,payload FROM sessions WHERE id=?', (identifier,)).fetchone()
        next_revision, payload = store.load(identifier)
        next_config, replacement, scope = restore(payload, build_agent, history_fallback=True)
        target_directory = Path(scope['directory'])
        if history_mode(replacement) and not target_directory.is_dir():
            target_directory = Path.cwd()
        try:
            if save_existing:
                save_current()
            os.chdir(target_directory)
        except Exception:
            getattr(replacement, 'close', replacement.client.close)()
            raise
        if agent is not None:
            getattr(agent, 'close', agent.client.close)()
        config, agent = next_config, replacement
        directory, config_path = target_directory, scope['config_path']
        session_file, disabled = scope['session_file'], scope['disabled']
        instructions, instruction_path = scope['instructions'], scope['instruction_path']
        session_id, revision = identifier, next_revision
        active_branch = active
        branch_state = dict(payload.get('branch') or {'kind': 'active'})
        if active_branch:
            loaded_row = None
            source_fingerprint = None
        else:
            source_meta = {'title': loaded_row[0], 'updated': loaded_row[1], 'revision': loaded_row[2]}
            source_fingerprint = payload_fingerprint(snapshot(replacement, current_scope()))
        saved_fingerprint = payload_fingerprint(snapshot(replacement, current_scope(), branch=branch_state))
        replacement.archive_id = identifier
        registry = SkillRegistry()
        profile_store = ProfileStore(config_path)
        if history_mode():
            display('历史查看模式：' + agent.client.reason + '可用 /history、/recall、/export 查看；'
                    '用 /api NAME 新建会话，原历史保持保存。', 'History')
        draft = store.draft(identifier)
        incomplete = agent.incomplete_draft
        if payload.get('checkpoint') or draft or incomplete:
            display('恢复了未完成任务的检查点；工具状态不明时先核实实际结果。', 'Recovery')
        if draft:
            display('上次流式草稿（未完成，未加入模型上下文；/export 可完整导出）：\n' +
                    (draft.get('content') or draft.get('reasoning_content') or '')[:4000], 'Draft')
        if incomplete:
            text = incomplete.get('content') or incomplete.get('reasoning_content') or ''
            display('恢复了未完成输出草稿；未完成工具调用只保留在草稿中，未加入模型上下文：\n' +
                    text[:4000], 'Draft')

    def compact_current(keep):
        from .context import compact_messages
        nonlocal session_id, revision
        if not 1 <= keep <= 100:
            raise ValueError('Use /compact [1–100 recent user turns]')
        original = session_id
        title = current_title()
        payload = snapshot(agent, current_scope())
        messages, omitted = compact_messages(payload['messages'], keep, original)
        if not omitted:
            display('消息轮次未超过保留数量，无需精简。', 'Context')
            return
        payload['messages'] = messages
        identifier = new_id()
        store.save(identifier, 0, payload)
        store.rename(identifier, title[:100] + ' · compact')
        load_session(identifier, save_existing=False, active=True)
        display(f'完整历史保留于 {original}；当前会话 {identifier} 已省略 {omitted} 条早期消息。', 'Context')

    try:
        if options.session:
            load_session(options.session)
        if options.prompt is not None:
            if not submit_input(options.prompt):
                raise SystemExit(1)
            return
        display("Termux Agent — /help for commands")
        display({"api": config.api_profile, "directory": str(directory), "agent_file": instruction_path, "session": session_id if store else "in-memory"})
        display({"auto_continue": config.auto_continue, "max_steps": config.max_steps})
        if formatter.enabled:
            display("Tab 补全命令和 ~/本地路径；文件路径回车提交文本。")
        while True:
            try:
                if formatter.enabled and sys.stdin.isatty():
                    session_label = session_id + (" (memory)" if store is None else "")
                    display(f"Session={session_label} | Model={config.model} | Cwd={directory}\n"
                            f"Title={current_title()}" + (' | Mode=历史查看' if history_mode() else '') +
                            '\n' + format_context_status(agent.context_status()), "Status")
                prompt = read_prompt(formatter.prompt(), formatter.continuation()).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not prompt:
                continue
            if history_mode() and prompt.split(maxsplit=1)[0] not in (
                    '/help', '/sessions', '/session', '/resume', '/history', '/stats', '/status', '/config', '/debug',
                    '/debug-context', '/silent', '/api', '/title', '/recall', '/export', '/copy', '/paste', '/exit'):
                display('当前为历史查看模式；请用 /api NAME 选择可用 API 并新建会话。', 'History')
                continue
            try:
                local_path = path_value(prompt, cli_commands)
            except ValueError as exc:
                display(f"Error: {exc}")
                continue
            if local_path is not None:
                submit_input(prompt)
                continue
            if prompt == "/exit":
                try:
                    save_current()
                except (ValueError, OSError, sqlite3.Error) as exc:
                    display(f"Save failed: {exc}")
                    continue
                break
            if prompt == "/help":
                display(HELP)
            elif prompt == "/clear":
                try:
                    save_current()
                    replacement = build_agent(config, instructions)
                    getattr(agent, 'close', agent.client.close)()
                    agent = replacement
                    session_id, revision = new_id(), 0
                    active_branch, loaded_row, source_fingerprint = True, None, None
                    branch_state, saved_fingerprint = {'kind': 'active'}, None
                    agent.archive_id = None
                    display("New session; previous conversation remains saved.")
                except (ValueError, OSError, sqlite3.Error) as exc:
                    display(f"Error: {exc}")
            elif prompt == "/stats":
                display(agent.stats())
            elif prompt == '/status':
                display(agent.stats()['stop'])
            elif prompt == '/continue' or prompt.startswith('/continue '):
                try:
                    value = prompt[9:].strip()
                    answer = agent.continue_task(int(value) if value else None)
                    show_answer(answer); save_current()
                except (ValueError, OSError, sqlite3.Error) as exc:
                    display(f'Error: {exc}')
            elif prompt == '/compact' or prompt.startswith('/compact '):
                try:
                    if not store:
                        raise ValueError('/compact 需要会话持久化，请去掉 --no-session')
                    value = prompt[8:].strip()
                    if value == 'task' or value.startswith('task '):
                        from .context import compact_task_messages
                        keep = int(value[4:].strip() or 2)
                        original = session_id; title = current_title()
                        payload = snapshot(agent, current_scope())
                        messages, details = compact_task_messages(payload['messages'], keep, original)
                        if not details['omitted_groups'] and not details['reduced_calls']:
                            display('当前任务没有可压缩的完整工具组；用 /compact 精简更早的历史，'
                                    '或 /compact 1 仅保留最近一轮。', 'Context')
                        else:
                            payload['messages'] = messages
                            identifier = create_active_branch(
                                payload, title[:95] + ' · compact task', archive_id=original,
                                draft=store.draft(original))
                            load_session(identifier, save_existing=False, active=True)
                            display(f'完整任务历史保留于 {original}；当前会话 {identifier}：{details}', 'Context')
                    else:
                        if value:
                            compact_current(int(value))
                        elif not agent._compact_for_context(force=True):
                            display('没有可安全精简的历史；可用 /compact 1 仅保留最近一轮。', 'Context')
                except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
                    display(f'Error: {exc}')
            elif prompt == '/recall' or prompt.startswith('/recall '):
                try:
                    if not store:
                        raise ValueError('/recall 需要会话持久化')
                    save_current()
                    rows = store.recall(prompt[7:].strip(), silent_writes=config.silent_writes)
                    display(rows if rows else '未找到匹配会话。', 'Recall')
                except (ValueError, OSError, sqlite3.Error) as exc:
                    display(f'Error: {exc}')
            elif prompt == '/export' or prompt.startswith('/export '):
                try:
                    if not store:
                        raise ValueError('/export 需要会话持久化')
                    args = shlex.split(prompt)[1:]
                    target = session_id
                    if args and args[0] not in ('markdown', 'json'):
                        target = args.pop(0)
                        store.check_id(target)
                    if len(args) > 2:
                        raise ValueError('Use /export [ID] [markdown|json] [PATH]')
                    if target == session_id:
                        save_current(force=True)
                    path = store.export(target, args[0] if args else 'markdown', args[1] if len(args) > 1 else None)
                    display(f'已导出：{path}（本地明文，权限 0600）', 'Export')
                except (ValueError, OSError, sqlite3.Error) as exc:
                    display(f'Error: {exc}')
            elif prompt == "/config":
                display({**config.public_dict(), "directory": str(directory), "agent_file": instruction_path})
            elif prompt == "/debug":
                debug = not debug
                display(f"Debug {'on' if debug else 'off'}")
            elif prompt == "/debug-context":
                display(agent.debug_context())
            elif prompt == "/silent" or prompt.startswith("/silent "):
                value = prompt[7:].strip()
                if value not in ("", "on", "off"):
                    display("Use /silent on or /silent off")
                else:
                    if value:
                        config.silent_writes = value == "on"
                    display({"silent_writes": config.silent_writes})
            elif prompt == "/auto" or prompt.startswith("/auto "):
                value = prompt[5:].strip()
                try:
                    if value in ("on", "off"):
                        config.auto_continue = value == "on"
                    elif value.startswith("steps "):
                        count = int(value[6:].strip())
                        if not 1 <= count <= 256:
                            raise ValueError("Automatic request limit must be 1–256")
                        config.max_steps = count
                    elif value:
                        raise ValueError("Use /auto on|off|steps N")
                    display({"auto_continue": config.auto_continue, "max_steps": config.max_steps,
                             "max_continuations": config.max_continuations})
                except ValueError as exc:
                    display(f"Error: {exc}")
            elif prompt == "/run" or prompt.startswith("/run "):
                task = prompt[4:].strip()
                if task:
                    submit_input(task, automatic=True)
                else:
                    display("Use /run TASK or /run ~/task-instructions.txt")
            elif prompt == "/api" or prompt.startswith("/api "):
                value = prompt[4:].strip()
                try:
                    rows = profile_store.list()
                    if not value or value == "list":
                        display({"current": config.api_profile, "default": profile_store.get_default(),
                                 "profiles": [{"number": i, **row} for i, row in enumerate(rows, 1)]})
                    elif value.startswith("add "):
                        parts = shlex.split(value[4:])
                        flags = [part for part in parts if part.startswith("--")]
                        positional = [part for part in parts if not part.startswith("--")]
                        if not 3 <= len(positional) <= 5:
                            raise ValueError("Use /api add NAME URL MODEL [KEY_ENV] [local|cloud]")
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
                                raise ValueError(f"Unknown API option: {flag}")
                        path = profile_store.add(name, base_url, model, key_env, kind,
                                                 stream, timeout, user_agent)
                        display({"added": name, "path": str(path), "switch": f"/api {name}"})
                    elif value.startswith("show "):
                        name = value[5:].strip()
                        matches = [row for row in rows if row["name"] == name]
                        if name == "default":
                            display(Config.load(config_path, name).public_dict())
                        elif matches:
                            display(matches[0])
                        else:
                            raise ValueError(f"Unknown API profile: {name}")
                    elif value.startswith("remove "):
                        name = value[7:].strip()
                        if name == config.api_profile:
                            raise ValueError("Switch to another API before removing the active profile")
                        backup = profile_store.remove(name)
                        display({"removed": name, "backup": str(backup)})
                    else:
                        persist = value.startswith("default ")
                        names = [row["name"] for row in rows]
                        name = value[8:].strip() if persist else value.removeprefix("switch ").strip()
                        if name in ("next", "prev"):
                            if not names:
                                raise ValueError("No named API profiles configured")
                            index = names.index(config.api_profile) if config.api_profile in names else (
                                -1 if name == "next" else 0)
                            name = names[(index + (1 if name == "next" else -1)) % len(names)]
                        elif name.isdigit():
                            number = int(name)
                            if not 1 <= number <= len(names):
                                raise ValueError("API number is not in /api list")
                            name = names[number - 1]
                        switched = switch_api(name, persist)
                        display({"api": name, "switched": switched,
                                 "default": profile_store.get_default()})
                except (ValueError, OSError, sqlite3.Error) as exc:
                    display(f"Error: {exc}")
            elif prompt == "/agent" or prompt.startswith("/agent "):
                value = prompt[6:].strip()
                if not value:
                    display({"agent_file": instruction_path, "chars": len(instructions)})
                    continue
                try:
                    next_file, next_disabled = session_file, disabled
                    if value == "off":
                        next_disabled = True
                    elif value == "directory":
                        next_file, next_disabled = None, False
                    elif value != "reload":
                        next_file, next_disabled = value, False
                    text, source = load_instructions(directory, next_file, next_disabled)
                    save_current()
                    replacement = build_agent(config, text)
                    agent.client.close()
                    agent = replacement
                    session_id, revision = new_id(), 0
                    instructions, instruction_path = text, source
                    session_file, disabled = next_file, next_disabled
                    display({"agent_file": source, "chars": len(text), "new_conversation": True})
                except (ValueError, OSError, sqlite3.Error) as exc:
                    display(f"Error: {exc}")
            elif prompt == "/skill" or prompt.startswith("/skill "):
                try:
                    parts = prompt.split(maxsplit=2)
                    action = parts[1] if len(parts) > 1 else "list"
                    name = parts[2] if len(parts) > 2 else ""
                    if action == "list":
                        active = agent.get_active_skills()
                        display([{"name": skill.name, "active": skill.name in active,
                                  "description": skill.description} for skill in registry.list_skills()])
                    elif action == "info" and name:
                        skill = registry.find(name)
                        display({"name": skill.name, "description": skill.description, "path": str(skill.path),
                                 "active": name in agent.active_skills})
                    elif action == "load" and name:
                        skill, body = registry.load(name)
                        previous = agent.get_active_skills()
                        agent.load_skill(skill.name, body)
                        try:
                            save_current(force=True)
                        except Exception:
                            agent.clear_skills()
                            for active_name, content in previous.items():
                                agent.load_skill(active_name, content)
                            raise
                        display(f"Loaded {skill.name} into this session.")
                    elif action in ("unload", "clear"):
                        if action == "unload" and not name:
                            raise ValueError("Use /skill unload NAME")
                        previous = agent.get_active_skills()
                        if action == "clear":
                            agent.clear_skills()
                        else:
                            agent.unload_skill(name)
                        try:
                            save_current(force=True)
                        except Exception:
                            agent.clear_skills()
                            for active_name, content in previous.items():
                                agent.load_skill(active_name, content)
                            raise
                        display("Active skills cleared." if action == "clear" else f"Unloaded {name}.")
                    else:
                        display("Use /skill list|info NAME|load NAME|unload NAME|clear")
                except (ValueError, OSError, sqlite3.Error) as exc:
                    display(f"Error: {exc}")
            elif prompt.startswith("/copy"):
                try:
                    text = prompt[5:].lstrip()
                    if not text:
                        text = next((m.get("content") for m in reversed(agent.messages)
                                     if m.get("role") == "assistant" and m.get("content")), "")
                    command = shutil.which("termux-clipboard-set")
                    if not command:
                        raise RuntimeError("termux-clipboard-set is unavailable; install Termux:API")
                    result = subprocess.run([command], input=text, text=True, capture_output=True, timeout=10)
                    if result.returncode:
                        raise RuntimeError(result.stderr.strip() or "clipboard write failed")
                    display("Copied to Android clipboard." if text else "Nothing to copy.")
                except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                    display(f"Error: {exc}")
            elif prompt == "/paste":
                command = shutil.which("termux-clipboard-get")
                if not command:
                    display("termux-clipboard-get is unavailable; install Termux:API")
                    continue
                try:
                    result = subprocess.run([command], text=True, capture_output=True, timeout=10, check=True)
                    if len(result.stdout) > config.max_output_chars:
                        display(result.stdout[:config.max_output_chars] + "\n[TRUNCATED]")
                    else:
                        display(result.stdout)
                except (OSError, subprocess.SubprocessError) as exc:
                    display(f"Clipboard read failed: {exc}")
            elif prompt == "/sub" or prompt.startswith("/sub "):
                task = prompt[4:].strip()
                if not task:
                    display({"max_subagents": config.max_subagents,
                             "subagent_max_steps": min(config.subagent_max_steps, config.max_steps),
                             "api": config.api_profile, "execution": "synchronous; no nested delegation"}, "Sub")
                    continue
                read_grant = False
                try:
                    result = agent.delegate(task)
                    display(f"{result.get('id', 'sub')} · {agent.last_run['status']}", "Sub")
                    display(result.get("summary", result.get("error", "")), "Sub")
                    save_current()
                except (ValueError, OSError, sqlite3.Error) as exc:
                    display(f"Sub-agent failed: {exc}", "Sub")
            elif prompt.startswith("!"):
                command = prompt[1:].strip()
                if command:
                    if agent._approved("shell", {"command": command}):
                        from .tools import execute
                        agent._direct_shell_count += 1
                        result = execute("shell", {"command": command, "cwd": str(directory)}, config.max_output_chars)
                        display(result, "Shell")
                        try:
                            save_current()
                        except (ValueError, OSError, sqlite3.Error) as exc:
                            display(f"Session save failed: {exc}")
                    else:
                        display("Shell command denied or approval unavailable.")
            elif prompt == "/title" or prompt.startswith("/title "):
                if not store:
                    display("Persistence disabled by --no-session.")
                    continue
                try:
                    value = prompt[6:].strip()
                    if value:
                        rename_current(value)
                    else:
                        display(f"{session_id} · {current_title()}", "Session")
                except (ValueError, OSError, sqlite3.Error) as exc:
                    display(f"Error: {exc}")
            elif prompt == "/sessions" or prompt.startswith("/sessions "):
                try:
                    value = prompt[9:].strip()
                    query = None
                    if value == "find" or value.startswith("find "):
                        if not value[4:].strip():
                            raise ValueError("Use /sessions find TEXT")
                        query = validate_title(value[4:].strip())
                        limit = 50
                    else:
                        limit = int(value or 50)
                    if not 1 <= limit <= 500:
                        raise ValueError("Session count must be 1–500")
                    if not store:
                        display("Persistence disabled by --no-session.")
                        continue
                    show_session_listing(limit, query)
                except (ValueError, sqlite3.Error) as exc:
                    display(f"Error: {exc}")
            elif prompt == "/resume" or prompt.startswith("/resume "):
                try:
                    if not store:
                        display("Persistence disabled by --no-session.")
                        continue
                    value = prompt[7:].strip()
                    if value:
                        resume_session(value)
                    else:
                        rows = show_session_listing(50, empty_text="没有已保存会话。")
                        if rows:
                            resume_picker()
                except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
                    display(f"Error: {exc}")
            elif prompt == "/history" or prompt.startswith("/history "):
                try:
                    parts = prompt[8:].split()
                    count, skip = int(parts[0]) if parts else 20, int(parts[1]) if len(parts) > 1 else 0
                    if not 1 <= count <= 100 or skip < 0 or len(parts) > 2:
                        raise ValueError("Use /history [1–100] [non-negative skip]")
                    history = agent.messages[1:]
                    end = max(0, len(history) - skip)
                    start = max(0, end-count)
                    display(f"History {start + 1 if start < end else 0}–{end} / {len(history)}"
                            " · --:-- = time not recorded")
                    for message in history[start:end]:
                        role, rendered = history_entry(message, config.silent_writes)
                        display(rendered, role, stamp="--:--")
                except ValueError as exc:
                    display(f"Error: {exc}")
            elif prompt == "/session" or prompt.startswith("/session "):
                if not store:
                    display("Persistence disabled by --no-session.")
                    continue
                command, _, value = prompt[8:].strip().partition(" ")
                try:
                    if not command:
                        display({"id": session_id, "title": current_title(), "saved": bool(revision)})
                    elif command in ("new", "fork"):
                        if history_mode():
                            raise ValueError('历史查看模式保留原快照；请用 /api NAME 新建可用会话。')
                        if command == "new":
                            save_current()
                            replacement = build_agent(config, instructions)
                            getattr(agent, 'close', agent.client.close)()
                            agent = replacement
                        session_id, revision = new_id(), 0
                        save_current(force=True)
                        if value:
                            store.rename(session_id, value)
                        display({"id": session_id, "title": current_title()})
                    elif command == "load":
                        resume_session(value)
                    elif command == "rename":
                        rename_current(value)
                    elif command == "rename-id":
                        target, _, title = value.partition(' ')
                        title = validate_title(title)
                        if target == session_id:
                            rename_current(title)
                        else:
                            store.rename(target, title)
                            display(f"{target} · {title}", 'Session')
                    elif command == "save":
                        save_current(force=True)
                        display('历史已保存；查看模式不重写原快照。' if history_mode() else {"id": session_id, "saved": True})
                    elif command == "delete":
                        target = value.removesuffix(" --yes")
                        if target == session_id:
                            raise ValueError("Switch to another session before deleting the active one")
                        store.load(target)
                        confirmed = value.endswith(" --yes")
                        if not confirmed and sys.stdin.isatty():
                            confirmed = input(f"Delete session {target}? [y/N] ").strip().lower() in ('y', 'yes')
                        if confirmed:
                            store.delete(target)
                            display("Session deleted.")
                        else:
                            display("Deletion cancelled; use --yes for explicit noninteractive deletion.")
                    else:
                        display("Use /session new|fork|load ID|N|rename NAME|rename-id ID NAME|delete ID|save")
                except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
                    display(f"Error: {exc}")
            elif prompt.startswith("/"):
                display("Unknown command; use /help.")
            else:
                submit_input(prompt)
    except Exception as exc:
        display(f"Error: {exc}")
        raise SystemExit(1) from None
    finally:
        try:
            save_current()
        except (ValueError, OSError, sqlite3.Error) as exc:
            display(f"Session save failed: {exc}; this turn was not saved.")
        if agent is not None:
            getattr(agent, 'close', agent.client.close)()
        save_readline_history()
        if store:
            store.close()


if __name__ == "__main__":
    main()
