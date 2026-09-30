"""Plain terminal REPL; no terminal UI framework."""
import argparse
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
from .sessions import SessionStore, new_id, snapshot, restore
from .skills import SkillRegistry
from .api_profiles import ProfileStore
from .terminal_ui import Columns, Completer, COMMANDS
from .local_input import load_text_submission, path_value

HELP = """/help           Show commands
Tab             Complete command, API, session and Skill names
~/path or /path  Tab completes local files; Enter submits UTF-8 text
/clear          Start fresh; keep previous session in history
/stats          Server token usage and context sizes
/config         Show effective configuration, without credentials
/debug          Toggle request usage and tool result diagnostics
/debug-context  Show last request messages and tools, with key redacted
/api [name|number|next]  List or switch API (new conversation)
/api add NAME URL MODEL [KEY_ENV] [local|cloud]  Add a profile
/api default NAME   Use this profile on future launches
/api show NAME      View profile without credentials
/api remove NAME    Remove a profile added with /api add
/agent [path|off|directory|reload]  Session instructions (new conversation)
/skill list|info|load|unload|clear   Explicit Hermes Skill control
/copy [text]   Copy text/last answer to Android clipboard
/paste         Show Android clipboard text
!command       Run Shell directly (approval still applies)
/sessions [N]   List saved sessions (default 50, max 500)
/session        Show ID; new, load ID, rename NAME, delete ID, fork, save
/history [N] [skip]  Show recent messages; skip to page backward
/exit           Save and exit
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
    parser = argparse.ArgumentParser(description="Minimal native Termux LLM agent")
    parser.add_argument("--config", help="TOML configuration path")
    parser.add_argument("--prompt", help="Run one prompt and exit")
    parser.add_argument("--api", help="Named API profile from config")
    parser.add_argument("--directory", default=None, help="Working directory; loads only its agent.md")
    parser.add_argument("--agent", help="Explicit session instruction file; replaces directory agent.md")
    parser.add_argument("--no-agent", action="store_true", help="Disable instruction files")
    parser.add_argument("--session", help="Resume saved session ID")
    parser.add_argument("--no-session", action="store_true", help="Keep this run in memory only")
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
    formatter = Columns(sys.stdout, enabled=sys.stdout.isatty() and options.prompt is None)

    def redact(text):
        return text.replace(config.api_key, "[REDACTED]") if config.api_key else text

    def display(value, role="Info"):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        # Do not let model/tool text inject terminal control sequences.
        text = ''.join(c if c in '\n\t' or c.isprintable() else f'\\x{ord(c):02x}' for c in text)
        formatter.message(role, redact(text))

    def approve(name, args):
        display({"approval_required": name, "arguments": args}, "Approve")
        if not sys.stdin.isatty():
            display("Denied: approval requires an interactive terminal.")
            return False
        try:
            return input("Allow this tool call? [y/N] ").strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False

    def event(kind, payload):
        if debug or kind == "tool_start":
            display({kind: payload}, "Tool")
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
        nonlocal stream_wrote
        safe = "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" for c in value)
        formatter.feed("Think" if kind == "reasoning" else "Agent", redact(safe))
        stream_wrote = True

    def show_answer(value):
        nonlocal stream_wrote
        streamed = getattr(agent.client, "streamed", False)
        if stream_wrote:
            formatter.finish()
        failure = value.startswith(("LLM 请求失败", "LLM 请求已中断", "已达到最大模型请求"))
        if value and (not streamed or not getattr(agent.client, "streamed_text", False) or failure):
            display(value, "Agent")
        stream_wrote = False

    def build_agent(next_config, text):
        kwargs = {"approve": approve, "event": event}
        if text:
            kwargs["instructions"] = text
        built = Agent(next_config, **kwargs)
        built.client.on_stream = stream_output
        return built

    agent = build_agent(config, instructions)
    store = None if options.no_session else SessionStore()
    session_id, revision = new_id(), 0

    if _readline:
        completer = Completer(
            _readline,
            profiles=lambda: [row["name"] for row in profile_store.list()],
            skills=lambda: [skill.name for skill in registry.list_skills()],
            active_skills=lambda: list(agent.active_skills),
            session_ids=lambda: store.ids() if store else [],
            directory=lambda: directory,
        )
        _readline.set_completer_delims(" \t\n")
        _readline.set_completer(completer)
        if "libedit" in (_readline.__doc__ or ""):
            _readline.parse_and_bind("bind ^I rl_complete")
        else:
            _readline.parse_and_bind("set show-all-if-ambiguous off")
            _readline.parse_and_bind("tab: menu-complete")

    def save_current(force=False):
        nonlocal revision
        if store and (force or revision or len(agent.messages) > 1):
            scope = {"directory": str(directory), "config_path": config_path,
                     "session_file": session_file, "disabled": disabled,
                     "instructions": instructions, "instruction_path": instruction_path}
            revision = store.save(session_id, revision, snapshot(agent, scope))

    def submit_input(value):
        nonlocal stream_wrote
        try:
            submission = load_text_submission(value, COMMANDS, config.max_input_file_chars)
            if submission is not None:
                display({"file": str(submission.path), "chars": len(submission.text),
                         "api": config.api_profile})
                value = submission.text
            show_answer(agent.run(value))
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

    def switch_api(name, persist=False):
        nonlocal config, agent, session_id, revision
        candidate = Config.load(config_path, name)
        if name == config.api_profile:
            if persist:
                profile_store.set_default(name)
            return False
        save_current()
        if persist:
            profile_store.set_default(name)
        replacement = build_agent(candidate, instructions)
        agent.client.close()
        config, agent = candidate, replacement
        session_id, revision = new_id(), 0
        return True

    def load_session(identifier):
        nonlocal agent, config, directory, config_path, session_file, disabled
        nonlocal instructions, instruction_path, session_id, revision
        nonlocal registry, profile_store
        next_revision, payload = store.load(identifier)
        next_config, replacement, scope = restore(payload, build_agent)
        try:
            save_current()
            os.chdir(scope['directory'])
        except Exception:
            replacement.client.close()
            raise
        agent.client.close()
        config, agent = next_config, replacement
        directory, config_path = Path(scope['directory']), scope['config_path']
        session_file, disabled = scope['session_file'], scope['disabled']
        instructions, instruction_path = scope['instructions'], scope['instruction_path']
        session_id, revision = identifier, next_revision
        registry = SkillRegistry()
        profile_store = ProfileStore(config_path)

    try:
        if options.session:
            load_session(options.session)
        if options.prompt is not None:
            if not submit_input(options.prompt):
                raise SystemExit(1)
            return
        display("Termux Agent — /help for commands")
        display({"api": config.api_profile, "directory": str(directory), "agent_file": instruction_path, "session": session_id if store else "in-memory"})
        if formatter.enabled:
            display("Tab 补全命令和 ~/本地路径；文件路径回车提交文本。")
        while True:
            try:
                prompt = read_prompt(formatter.prompt(), formatter.continuation()).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not prompt:
                continue
            try:
                local_path = path_value(prompt, COMMANDS)
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
                    agent.client.close()
                    agent = replacement
                    session_id, revision = new_id(), 0
                    display("New session; previous conversation remains saved.")
                except (ValueError, OSError, sqlite3.Error) as exc:
                    display(f"Error: {exc}")
            elif prompt == "/stats":
                display(agent.stats())
            elif prompt == "/config":
                display({**config.public_dict(), "directory": str(directory), "agent_file": instruction_path})
            elif prompt == "/debug":
                debug = not debug
                display(f"Debug {'on' if debug else 'off'}")
            elif prompt == "/debug-context":
                display(agent.debug_context())
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
            elif prompt == "/sessions" or prompt.startswith("/sessions "):
                try:
                    limit = int(prompt[9:].strip() or 50)
                    if not 1 <= limit <= 500:
                        raise ValueError("Session count must be 1–500")
                    display(store.listing(limit) if store else "Persistence disabled by --no-session.")
                except (ValueError, sqlite3.Error) as exc:
                    display(f"Error: {exc}")
            elif prompt == "/history" or prompt.startswith("/history "):
                try:
                    parts = prompt[8:].split()
                    count, skip = int(parts[0]) if parts else 20, int(parts[1]) if len(parts) > 1 else 0
                    if not 1 <= count <= 100 or skip < 0 or len(parts) > 2:
                        raise ValueError("Use /history [1–100] [non-negative skip]")
                    history = agent.messages[1:]
                    end = max(0, len(history) - skip)
                    for message in history[max(0, end-count):end]:
                        visible = {k: v for k, v in message.items() if k != "reasoning_content"}
                        rendered = json.dumps(visible, ensure_ascii=False)
                        role = {"user": "You", "assistant": "Agent", "tool": "Tool"}.get(
                            message.get("role"), "Info")
                        display(rendered[:4000] + (" [TRUNCATED]" if len(rendered) > 4000 else ""), role)
                except ValueError as exc:
                    display(f"Error: {exc}")
            elif prompt == "/session" or prompt.startswith("/session "):
                if not store:
                    display("Persistence disabled by --no-session.")
                    continue
                command, _, value = prompt[8:].strip().partition(" ")
                try:
                    if not command:
                        display({"id": session_id, "saved": bool(revision)})
                    elif command in ("new", "fork"):
                        if command == "new":
                            save_current()
                            replacement = build_agent(config, instructions)
                            agent.client.close()
                            agent = replacement
                        session_id, revision = new_id(), 0
                        save_current(force=True)
                        if value:
                            store.rename(session_id, value)
                        display({"id": session_id})
                    elif command == "load":
                        if value != session_id:
                            load_session(value)
                        display({"id": session_id, "api": config.api_profile, "directory": str(directory)})
                    elif command == "rename":
                        save_current(force=True)
                        store.rename(session_id, value)
                    elif command == "save":
                        save_current(force=True)
                        display({"id": session_id, "saved": True})
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
                        display("Use /session new|fork|load ID|rename NAME|delete ID|save")
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
        agent.client.close()
        save_readline_history()
        if store:
            store.close()


if __name__ == "__main__":
    main()
