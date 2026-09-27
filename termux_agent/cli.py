"""Plain terminal REPL; no terminal UI framework."""
import argparse
import json
import sys
import os
import sqlite3
from pathlib import Path
from .instructions import load_instructions

from .agent import Agent
from .config import Config
from .sessions import SessionStore, new_id, snapshot, restore

HELP = """/help           Show commands
/clear          Start fresh; keep previous session in history
/stats          Server token usage and context sizes
/config         Show effective configuration, without credentials
/debug          Toggle request usage and tool result diagnostics
/debug-context  Show last request messages and tools, with key redacted
/api [name]     List or switch API (new conversation)
/agent [path|off|directory|reload]  Session instructions (new conversation)
/sessions [N]   List saved sessions (default 50, max 500)
/session        Show ID; new, load ID, rename NAME, delete ID, fork, save
/history [N] [skip]  Show recent messages; skip to page backward
/exit           Save and exit
"""


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

    def redact(text):
        return text.replace(config.api_key, "[REDACTED]") if config.api_key else text

    def display(value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        # Do not let model/tool text inject terminal control sequences.
        text = ''.join(c if c in '\n\t' or c.isprintable() else f'\\x{ord(c):02x}' for c in text)
        print(redact(text), flush=True)

    def approve(name, args):
        display({"approval_required": name, "arguments": args})
        if not sys.stdin.isatty():
            display("Denied: approval requires an interactive terminal.")
            return False
        try:
            return input("Allow this tool call? [y/N] ").strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            return False

    def event(kind, payload):
        if debug or kind == "tool_start":
            display({kind: payload})

    def build_agent(next_config, text):
        kwargs = {"approve": approve, "event": event}
        if text:
            kwargs["instructions"] = text
        return Agent(next_config, **kwargs)

    agent = build_agent(config, instructions)
    store = None if options.no_session else SessionStore()
    session_id, revision = new_id(), 0

    def save_current(force=False):
        nonlocal revision
        if store and (force or revision or len(agent.messages) > 1):
            scope = {"directory": str(directory), "config_path": config_path,
                     "session_file": session_file, "disabled": disabled,
                     "instructions": instructions, "instruction_path": instruction_path}
            revision = store.save(session_id, revision, snapshot(agent, scope))

    def load_session(identifier):
        nonlocal agent, config, directory, config_path, session_file, disabled
        nonlocal instructions, instruction_path, session_id, revision
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

    try:
        if options.session:
            load_session(options.session)
        if options.prompt is not None:
            display(agent.run(options.prompt))
            save_current()
            return
        display("Termux Agent — /help for commands")
        display({"api": config.api_profile, "directory": str(directory), "agent_file": instruction_path, "session": session_id if store else "in-memory"})
        while True:
            try:
                prompt = input("ta> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not prompt:
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
                name = prompt[4:].strip()
                try:
                    if not name:
                        display({"current": config.api_profile, "profiles": Config.profiles(config_path)})
                    else:
                        candidate = Config.load(config_path, name)
                        save_current()
                        replacement = build_agent(candidate, instructions)
                        agent.client.close()
                        config, agent = candidate, replacement
                        session_id, revision = new_id(), 0
                        display(f"API switched to {name}; new conversation and stats.")
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
                        display(rendered[:4000] + (" [TRUNCATED]" if len(rendered) > 4000 else ""))
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
                try:
                    display(agent.run(prompt))
                    save_current()
                except KeyboardInterrupt:
                    display("Interrupted.")
                except Exception as exc:
                    display(f"Error: {exc}")
    except Exception as exc:
        display(f"Error: {exc}")
        raise SystemExit(1) from None
    finally:
        try:
            save_current()
        except (ValueError, OSError, sqlite3.Error) as exc:
            display(f"Session save failed: {exc}; this turn was not saved.")
        agent.client.close()
        if store:
            store.close()


if __name__ == "__main__":
    main()
