"""Explicit session file or current directory only; never search global paths."""
from pathlib import Path
from importlib.resources import files
import os


def system_prompt_path():
    return Path.home() / ".config/termux-agent/system_prompt.txt"


def _read_system_prompt(source):
    try:
        with source.open("r", encoding="utf-8") as stream:
            value = stream.read(16_003)
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Cannot read system prompt {source}: {exc}") from exc
    if len(value) > 16_002:
        raise ValueError(f"System prompt exceeds 16000 characters: {source}")
    value = value.rstrip("\r\n")
    if not value.strip():
        raise ValueError(f"System prompt is empty: {source}")
    if len(value) > 16_000:
        raise ValueError(f"System prompt exceeds 16000 characters: {source}")
    return value


def load_system_prompt():
    source = system_prompt_path()
    if not source.exists():
        template = _read_system_prompt(files("termux_agent").joinpath("system_prompt.txt"))
        source.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(template + "\n")
    return _read_system_prompt(source)


def load_instructions(directory, session_file=None, disabled=False):
    if disabled:
        return "", None
    directory = Path(directory).resolve()
    if session_file:
        source = Path(session_file).expanduser()
        if not source.is_absolute():
            source = directory / source
    else:
        # Starting in HOME must not turn ~/agent.md into global instructions.
        if directory == Path.home().resolve():
            return "", None
        source = directory / "agent.md"
        if not source.exists():
            return "", None
    with source.open("r", encoding="utf-8") as stream:
        content = stream.read(16001)
    if len(content) > 16000:
        raise ValueError("agent.md exceeds 16000 characters; shorten it before loading")
    return content, str(source.resolve())
