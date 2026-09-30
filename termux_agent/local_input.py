"""Read an explicitly submitted local text path, with bounded input."""
from dataclasses import dataclass
import os
from pathlib import Path
import stat


@dataclass(frozen=True)
class TextSubmission:
    path: Path
    text: str


def path_value(value, commands=()):
    value = value.strip()
    if (len(value) >= 2 and value[0] in (chr(34), chr(39)) and value[1] in ("/", "~", "～") and value[-1] != value[0]):
        raise ValueError("Close the quote around the local path")
    quoted = len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]
    if quoted:
        value = value[1:-1]
    if not value.startswith(("~", "～", "/")):
        return None
    if not quoted and value.split(maxsplit=1)[0] in commands:
        return None
    if "\n" in value or "\r" in value:
        raise ValueError("Enter one local path per submission")
    if value.startswith("～"):
        value = "~" + value[1:]
    return value


def load_text_submission(value, commands=(), max_chars=64_000):
    value = path_value(value, commands)
    if value is None:
        return None
    try:
        path = Path(value).expanduser().resolve()
    except RuntimeError:
        raise ValueError("Unknown home-directory shorthand; use ~/path") from None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Choose a regular text file; directories and devices cannot be submitted")
    if info.st_size > max_chars * 4 + 3:
        raise ValueError(f"Input file exceeds the {max_chars}-character limit")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Choose a regular text file")
        raw = stream.read(max_chars * 4 + 4)
    if b"\0" in raw:
        raise ValueError("Binary files cannot be submitted as text")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("Input file must be UTF-8 text") from None
    if len(text) > max_chars:
        raise ValueError(f"Input file exceeds the {max_chars}-character limit")
    if not text.strip():
        raise ValueError("Input file is empty")
    return TextSubmission(path, text)
