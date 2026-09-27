"""Explicit session file or current directory only; never search global paths."""
from pathlib import Path


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
