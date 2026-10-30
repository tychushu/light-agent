"""Optional, bounded RTK output compression for model shell results.

The module deliberately does not inspect prompts, rewrite commands, or run a
proxy. A command is classified conservatively, executed by the normal shell
path, and only its captured stdout is offered to ``rtk pipe --filter``.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


RAW_CAPTURE_BYTES = 1024 * 1024
RAW_STREAM_BYTES = RAW_CAPTURE_BYTES // 2
FILTER_TIMEOUT_SECONDS = 2.0
FILTER_OUTPUT_BYTES = 2 * 1024 * 1024
MIN_COMPRESSION_BYTES = 64
MIN_COMPRESSION_RATIO = 0.05
MIN_ARTIFACT_BYTES = 256
ARTIFACT_GLOBAL_QUOTA = 8 * 1024 * 1024
ARTIFACT_RETENTION = 32
ARTIFACT_NAME = re.compile(r"^shell-raw-[0-9a-f]{16}\.json$")
ARTIFACT_TEMP = re.compile(r"^\.shell-raw-[0-9a-f]{16}\.[0-9a-f]+\.tmp$")
_ARCHIVE_THREAD_LOCK = threading.Lock()


@dataclass(frozen=True)
class CompressionPlan:
    """A command/output pair eligible for one explicit RTK filter."""

    format: str
    binary: str


@dataclass
class FilterResult:
    output: bytes | None
    returncode: int | None
    stderr: bytes = b""
    timed_out: bool = False
    output_overflow: bool = False
    error: str = ""


class OutputCompressor:
    """Lazy policy holder shared by a parent agent and inherited by children."""

    def __init__(self, config: Any, artifact_root: Path | None = None):
        self.enabled = bool(getattr(config, "rtk_enabled", False))
        self.configured_binary = str(getattr(config, "rtk_binary", "") or "")
        self.api_key = str(getattr(config, "api_key", "") or "")
        self.artifact_root = artifact_root
        self.filter_timeout = FILTER_TIMEOUT_SECONDS
        self.last_stats: dict[str, Any] | None = None

    def plan(self, command: str) -> CompressionPlan | None:
        # Every shell attempt starts a fresh measurement window.
        self.last_stats = None
        if not self.enabled:
            return None
        format_name = classify_shell_command(command)
        if format_name is None:
            return None
        binary = resolve_rtk_binary(self.configured_binary)
        return CompressionPlan(format_name, binary) if binary else None

    def redact(self, data: bytes) -> bytes:
        return redact_bytes(data, (self.api_key,))

    def save_raw(self, stdout: bytes, stderr: bytes, exit_code: int | None,
                 timed_out: bool) -> dict[str, Any] | None:
        return save_raw_artifact(stdout, stderr, exit_code, timed_out,
                                 (self.api_key,), self.artifact_root)


def _secret_name(name: str) -> bool:
    upper = name.upper()
    return (upper in {"API_KEY", "API_TOKEN", "SECRET"} or
            upper.endswith(("_API_KEY", "_API_TOKEN", "_SECRET")))


def secret_values(extra: tuple[str, ...] | list[str] = ()) -> tuple[str, ...]:
    """Return non-empty values matching token, API-key and secret conventions."""
    values = {value for name, value in os.environ.items()
              if _secret_name(name) and value}
    values.update(value for value in extra if value)
    return tuple(sorted(values, key=lambda value: (-len(value), value)))


def redact_bytes(data: bytes, extra: tuple[str, ...] | list[str] = ()) -> bytes:
    """Redact known keys after complete capture, including split chunks."""
    for key in secret_values(extra):
        encoded = key.encode("utf-8")
        if encoded:
            data = data.replace(encoded, b"[REDACTED]")
    return data


def stable_raw_digest(stdout: bytes, stderr: bytes, exit_code: int | None,
                      timed_out: bool) -> str:
    payload = (str(exit_code).encode("ascii") + b"\0" +
               (b"1" if timed_out else b"0") + b"\0" +
               hashlib.sha256(stdout).digest() + hashlib.sha256(stderr).digest())
    return hashlib.sha256(payload).hexdigest()


def _is_path(candidate: str) -> bool:
    return os.sep in candidate or (os.altsep is not None and os.altsep in candidate)


def _resolve_candidate(candidate: str) -> str:
    candidate = os.path.expanduser(candidate)
    if _is_path(candidate):
        try:
            path = Path(candidate)
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        except OSError:
            pass
        return ""
    return shutil.which(candidate) or ""


def resolve_rtk_binary(configured: str = "") -> str:
    """Resolve configured/env/PATH RTK lazily, without a startup scan.

    A non-empty configuration is authoritative: when it is missing or not
    executable we do not silently substitute an environment or PATH binary.
    Environment lookup is likewise tried before PATH only when configuration
    is empty.
    """
    if configured:
        return _resolve_candidate(configured)
    environment = os.environ.get("TERMUX_AGENT_RTK", "")
    if environment:
        return _resolve_candidate(environment)
    return shutil.which("rtk") or ""


def _lex_shell_tokens(command: str) -> list[str] | None:
    if not isinstance(command, str) or not command.strip() or any(c in command for c in "\r\n\0"):
        return None
    # These forms can execute a second command or change the command's argv
    # after parsing.  Keep the classifier deliberately narrower than a shell.
    if "`" in command or "$" in command:
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    if any(any(operator in token for operator in ";&|<>") for token in tokens):
        return None
    try:
        return shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None


def _option_name(token: str) -> str:
    """Return an option's name, dropping a long-option value."""
    if token.startswith("--"):
        return token.split("=", 1)[0]
    return token


def _has_any(tokens: list[str], names: set[str]) -> bool:
    for token in tokens:
        value = _option_name(token)
        if value in names:
            return True
    return False


def _has_short_cluster(tokens: list[str], names: set[str]) -> bool:
    letters = {name.lstrip("-") for name in names if len(name.lstrip("-")) == 1}
    return any(token.startswith("-") and not token.startswith("--") and
               any(letter in token[1:] for letter in letters) for token in tokens)


def _has_machine_prefix(tokens: list[str], prefixes: tuple[str, ...]) -> bool:
    return any(token.startswith(prefix) for token in tokens for prefix in prefixes)


def _has_option(tokens: list[str], long_names: set[str], short_letters: set[str],
                prefixes: tuple[str, ...] = ()) -> bool:
    """Detect exact, ``--name=value``, and clustered short options."""
    for token in tokens:
        name = _option_name(token)
        if name in long_names:
            return True
        if any(name.startswith(prefix) for prefix in prefixes):
            return True
        if token.startswith("-") and not token.startswith("--") and len(token) > 1:
            if any(letter in token[1:] for letter in short_letters):
                return True
    return False


def classify_shell_command(command: str) -> str | None:
    """Return an explicit filter only for conservative, simple commands."""
    tokens = _lex_shell_tokens(command)
    if not tokens:
        return None
    executable = Path(tokens[0]).name
    args = tokens[1:]
    if executable == "git":
        if not args:
            return None
        subcommand = args[0]
        flags = args[1:]
        if subcommand == "status":
            if _has_option(flags,
                           {"--short", "--porcelain", "--json", "--format", "--pretty",
                            "--null", "--branch"},
                           {"s", "z", "b"},
                           ("--porcelain", "--format", "--pretty", "--branch")):
                return None
            return "git-status"
        if subcommand == "log":
            if _has_option(flags,
                           {"--format", "--pretty", "--raw", "--numstat", "--shortstat",
                            "--name-only", "--name-status", "--stat", "--patch", "--json",
                            "--null"},
                           {"p", "z"},
                           ("--format", "--pretty", "--raw", "--numstat", "--shortstat",
                            "--name", "--stat", "--patch", "--json")):
                return None
            allowed = {"--oneline", "--decorate", "--graph", "--all", "--no-merges", "--merges"}
            index = 0
            while index < len(flags):
                flag = flags[index]
                if flag in allowed:
                    index += 1
                    continue
                if flag in ("-n", "--max-count") and index + 1 < len(flags):
                    index += 2
                    continue
                if flag.startswith("--max-count="):
                    index += 1
                    continue
                return None
            return "git-log"
        return None
    if executable in {"rg", "grep"}:
        machine = {"--json", "-J", "--null", "-0", "-z", "--files",
                   "--files-with-matches", "--files-without-match", "--count",
                   "--count-matches", "-c", "-l", "-L", "-o", "--only-matching",
                   "-q", "--quiet", "--replace", "--format"}
        if (not args or _has_option(args,
                                    {"--json", "--null", "--files", "--files-with-matches",
                                     "--files-without-match", "--count", "--count-matches",
                                     "--only-matching", "--quiet", "--replace", "--format"},
                                    {"J", "0", "z", "c", "l", "L", "o", "q"},
                                    ("--json", "--format", "--files", "--count", "--replace"))):
            return None
        return "rg" if executable == "rg" else "grep"
    pytest_machine = ("--json", "--junit", "--report", "--result-log", "--log-",
                      "--tap", "--xml", "--html", "--cov-report")
    pytest_banned = {"--junitxml", "--json", "--report-log", "--result-log", "--log-file"}
    if executable == "pytest":
        if _has_option(args, pytest_banned, set(), pytest_machine):
            return None
        return "pytest"
    if executable in {"python", "python3"} and args[:2] == ["-m", "pytest"]:
        pytest_args = args[2:]
        if _has_option(pytest_args, pytest_banned, set(), pytest_machine):
            return None
        return "pytest"
    return None


def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _bounded_process(command: list[str], payload: bytes, env: dict[str, str],
                     timeout: float) -> FilterResult:
    """Run a fixed filter argv with bounded output and process-group cleanup."""
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, start_new_session=True)
    except (OSError, ValueError) as exc:
        return FilterResult(None, None, error=f"cannot start RTK: {exc}")

    out = bytearray()
    err = bytearray()
    overflow = False
    deadline = time.monotonic() + timeout
    write_error: list[BaseException] = []

    def feed_stdin() -> None:
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            write_error.append(exc)
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=feed_stdin, name="rtk-stdin", daemon=True)
    writer.start()
    selector = selectors.DefaultSelector()
    timed_out = False
    code: int | None = None

    try:
        for name, pipe in (("out", proc.stdout), ("err", proc.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(min(0.1, remaining)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                target = out if key.data == "out" else err
                room = FILTER_OUTPUT_BYTES - len(target)
                if room > 0:
                    target.extend(chunk[:room])
                if len(chunk) > room:
                    overflow = True

        if not timed_out:
            try:
                code = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True

        if timed_out:
            _kill_group(proc.pid, signal.SIGTERM)
            time.sleep(0.05)

        # The leader already had its remaining deadline after pipe EOF. These
        # signals clean descendants after a normal wait and never race a valid
        # close-pipes-then-exit leader.
        _kill_group(proc.pid, signal.SIGTERM)
        time.sleep(0.01)
        _kill_group(proc.pid, signal.SIGKILL)
        if code is None:
            try:
                code = proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                code = proc.returncode
    except BaseException:
        _kill_group(proc.pid, signal.SIGKILL)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        selector.close()
        for pipe in (proc.stdout, proc.stderr):
            if pipe and not pipe.closed:
                pipe.close()
        try:
            proc.stdin.close()
        except OSError:
            pass
        writer.join(timeout=1)

    error = ""
    if timed_out:
        error = "RTK filter timed out"
    elif overflow:
        error = "RTK filter output exceeded the bounded limit"
    elif write_error and not isinstance(write_error[0], BrokenPipeError):
        error = f"RTK filter stdin failed: {write_error[0]}"
    return FilterResult(bytes(out), code, bytes(err), timed_out, overflow, error)


def run_rtk_filter(binary: str, format_name: str, payload: bytes,
                   timeout: float = FILTER_TIMEOUT_SECONDS) -> FilterResult:
    if not payload or b"\0" in payload:
        return FilterResult(None, None, error="NUL or empty output is not filterable")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        return FilterResult(None, None, error="output is not UTF-8 text")
    env = os.environ.copy()
    env["RTK_TELEMETRY_DISABLED"] = "1"
    env.update({"TERM": "dumb", "CI": "1"})
    result = _bounded_process([binary, "pipe", "--filter", format_name], payload, env,
                              max(0.05, float(timeout)))
    return FilterResult(
        redact_bytes(result.output) if result.output is not None else None,
        result.returncode,
        redact_bytes(result.stderr),
        result.timed_out,
        result.output_overflow,
        redact_bytes(result.error.encode("utf-8")).decode("utf-8", "replace") if result.error else "",
    )


_DIR_FLAGS = os.O_RDONLY
if hasattr(os, "O_DIRECTORY"):
    _DIR_FLAGS |= os.O_DIRECTORY
if hasattr(os, "O_NOFOLLOW"):
    _DIR_FLAGS |= os.O_NOFOLLOW


def _open_child_dir(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise OSError("artifact path component is not a directory")
    return fd


def _open_private_child_dir(parent_fd: int, name: str) -> int:
    fd = _open_child_dir(parent_fd, name)
    try:
        os.fchmod(fd, 0o700)
    except OSError:
        os.close(fd)
        raise
    return fd


def _close_opened(opened: list[int], keep: int | None = None) -> None:
    for fd in reversed(opened):
        if fd != keep:
            try:
                os.close(fd)
            except OSError:
                pass


def _has_symlink_component(path: Path, trusted: Path) -> bool:
    """Check components at or below ``trusted`` without following them."""
    try:
        relative = path.relative_to(trusted)
    except ValueError:
        relative = path
        trusted = Path(path.anchor or "/")
    current = trusted
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _open_artifact_dir(root: Path | None = None) -> tuple[Path, int] | None:
    """Open/create storage below a trusted root, refusing symlinked children.

    HOME is allowed to be an OS alias (for example macOS ``/var`` to
    ``/private/var``): it is resolved once as the trusted anchor.  Components
    below that anchor are opened with ``O_NOFOLLOW`` and are never chmod'd via
    a pathname.  Explicit roots are treated as their own trusted anchor while
    still rejecting a symlinked root or ``output-artifacts`` child.
    """
    opened: list[int] = []
    try:
        if root is None:
            home_value = os.environ.get("HOME") or str(Path.home())
            home = Path(os.path.realpath(os.path.expanduser(home_value)))
            if not home.is_absolute():
                return None
            current = os.open(home, _DIR_FLAGS)
            opened.append(current)
            display = home
            for name in (".local", "share", "termux-agent", "output-artifacts"):
                if name in ("termux-agent", "output-artifacts"):
                    current = _open_private_child_dir(current, name)
                else:
                    current = _open_child_dir(current, name)
                opened.append(current)
                display = display / name
            _close_opened(opened, current)
            return display, current

        base = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
        home_value = os.environ.get("HOME") or str(Path.home())
        home_arg = Path(os.path.abspath(os.path.expanduser(home_value)))
        trusted = home_arg if _has_symlink_component(base.parent, home_arg) is False and \
            (base.parent == home_arg or home_arg in base.parent.parents) else Path(base.anchor or "/")
        if _has_symlink_component(base.parent, trusted):
            return None
        parent = Path(os.path.realpath(base.parent))
        current = os.open(parent, _DIR_FLAGS)
        opened.append(current)
        current = _open_child_dir(current, base.name)
        opened.append(current)
        current = _open_private_child_dir(current, "output-artifacts")
        opened.append(current)
        _close_opened(opened, current)
        return base / "output-artifacts", current
    except (OSError, ValueError, TypeError):
        _close_opened(opened)
        return None


def _safe_artifact_dir(root: Path | None = None) -> Path | None:
    opened = _open_artifact_dir(root)
    if opened is None:
        return None
    path, fd = opened
    os.close(fd)
    return path


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while publishing raw artifact")
        view = view[written:]


def _read_all(fd: int, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = os.read(fd, size - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _own_artifacts(dir_fd: int) -> list[tuple[float, int, str]]:
    rows = []
    try:
        names = os.listdir(dir_fd)
    except OSError:
        return rows
    for name in names:
        if not ARTIFACT_NAME.fullmatch(name):
            continue
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            rows.append((info.st_mtime, info.st_size, name))
    return sorted(rows)


def _remove_stale_temps(dir_fd: int) -> None:
    try:
        names = os.listdir(dir_fd)
    except OSError:
        return
    for name in names:
        if not ARTIFACT_TEMP.fullmatch(name):
            continue
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                os.unlink(name, dir_fd=dir_fd)
        except OSError:
            pass


def _owned_temporaries(dir_fd: int) -> list[tuple[float, int, str]]:
    rows = []
    try:
        names = os.listdir(dir_fd)
    except OSError:
        return rows
    for name in names:
        if not ARTIFACT_TEMP.fullmatch(name):
            continue
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            rows.append((info.st_mtime, info.st_size, name))
    return sorted(rows)


def _archive_payload(stdout: bytes, stderr: bytes, exit_code: int | None,
                     timed_out: bool, extra_keys: tuple[str, ...] | list[str]) -> bytes:
    safe_out = redact_bytes(stdout, extra_keys)
    safe_err = redact_bytes(stderr, extra_keys)
    payload: dict[str, Any] = {
        "version": 2,
        "kind": "termux-agent-shell-raw",
        "created_unix": time.time(),
        "exit_code": exit_code,
        "timed_out": bool(timed_out),
        "stdout_sha256": hashlib.sha256(safe_out).hexdigest(),
        "stderr_sha256": hashlib.sha256(safe_err).hexdigest(),
    }
    for name, value in (("stdout", safe_out), ("stderr", safe_err)):
        try:
            payload[name + "_encoding"] = "utf-8"
            payload[name + "_text"] = value.decode("utf-8")
        except UnicodeDecodeError:
            payload[name + "_encoding"] = "base64"
            payload[name + "_base64"] = base64.b64encode(value).decode("ascii")
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def save_raw_artifact(stdout: bytes, stderr: bytes, exit_code: int | None,
                      timed_out: bool, extra_keys: tuple[str, ...] | list[str] = (),
                      root: Path | None = None) -> dict[str, Any] | None:
    """Persist a redacted recoverable JSON archive under bounded quotas."""
    opened = _open_artifact_dir(root)
    if opened is None:
        return None
    directory, dir_fd = opened
    temp_name = ""
    lock_fd = -1
    _ARCHIVE_THREAD_LOCK.acquire()
    try:
        lock_flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        lock_fd = os.open(".archive.lock", lock_flags, 0o600, dir_fd=dir_fd)
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # The lock is held across cleanup, quota accounting, publication and
        # directory sync, so parallel children cannot race these decisions.
        _remove_stale_temps(dir_fd)
        data = _archive_payload(stdout, stderr, exit_code, timed_out, extra_keys)
        if len(data) > ARTIFACT_GLOBAL_QUOTA:
            return None

        leftover_temps = _owned_temporaries(dir_fd)
        existing = _own_artifacts(dir_fd)
        total = sum(size for _, size, _ in existing) + sum(size for _, size, _ in leftover_temps)
        while existing and (len(existing) >= ARTIFACT_RETENTION or
                            total + len(data) > ARTIFACT_GLOBAL_QUOTA):
            _, size, old = existing.pop(0)
            try:
                os.unlink(old, dir_fd=dir_fd)
                total -= size
            except OSError:
                break
        if total + len(data) > ARTIFACT_GLOBAL_QUOTA or len(existing) >= ARTIFACT_RETENTION:
            return None

        artifact_id = os.urandom(8).hex()
        name = f"shell-raw-{artifact_id}.json"
        temp_name = f".shell-raw-{artifact_id}.{os.urandom(4).hex()}.tmp"
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temp_name, flags, 0o600, dir_fd=dir_fd)
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, data)
            os.lseek(fd, 0, os.SEEK_SET)
            if _read_all(fd, len(data)) != data:
                raise OSError("raw artifact read-back did not match written data")
            json.loads(data.decode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        for _ in range(16):
            try:
                os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                break
            artifact_id = os.urandom(8).hex()
            name = f"shell-raw-{artifact_id}.json"
        else:
            return None
        os.replace(temp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        temp_name = ""
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        safe_out = redact_bytes(stdout, extra_keys)
        safe_err = redact_bytes(stderr, extra_keys)
        return {"id": artifact_id, "path": str(directory / name), "bytes": len(data),
                "stdout_bytes": len(safe_out), "stderr_bytes": len(safe_err)}
    except OSError:
        return None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name, dir_fd=dir_fd)
            except OSError:
                pass
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)
        os.close(dir_fd)
        _ARCHIVE_THREAD_LOCK.release()


def serialized_result_bytes(result: Any) -> bytes:
    return json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def meaningful_reduction(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Require a material final serialized reduction, including metadata."""
    before_size = len(serialized_result_bytes(before))
    after_size = len(serialized_result_bytes(after))
    saved = before_size - after_size
    return saved >= MIN_COMPRESSION_BYTES and saved >= before_size * MIN_COMPRESSION_RATIO


def finalize_result_metrics(before: dict[str, Any], after: dict[str, Any],
                            applied: bool) -> dict[str, Any]:
    """Make serialized before/after measurements self-consistent."""
    metadata = after["output_compression"]
    before_bytes = len(serialized_result_bytes(before))
    before_chars = len(serialized_result_bytes(before).decode("utf-8"))
    for _ in range(8):
        final = serialized_result_bytes(after)
        final_bytes = len(final)
        final_chars = len(final.decode("utf-8"))
        values = {
            "before_bytes": before_bytes,
            "after_bytes": final_bytes,
            "before_chars": before_chars,
            "after_chars": final_chars,
            "bytes_saved": max(0, before_bytes - final_bytes) if applied else 0,
            "chars_saved": max(0, before_chars - final_chars) if applied else 0,
        }
        if all(metadata.get(key) == value for key, value in values.items()):
            break
        metadata.update(values)
    return after


_VOLATILE_RESULT_KEYS = {
    "filter_reason", "filter_error", "raw_artifact", "artifact", "path", "paths",
    "ref", "refs", "reference", "references", "read", "timing", "duration",
    "elapsed", "timestamp", "created_unix", "created_at", "updated_at", "started_at",
    "finished_at", "completed_at",
}


def _volatile_result_key(key: str) -> bool:
    lower = key.lower()
    return (lower in _VOLATILE_RESULT_KEYS or
            lower.endswith(("_path", "_paths", "_ref", "_refs", "_reference",
                            "_references", "_timing", "_duration", "_timestamp")))


def stable_result_payload(result: Any) -> Any:
    """Normalize only output-compression metadata for result fingerprints.

    Result content, exit status, timeout state and the raw digest remain as
    evidence.  Artifact paths/ids and measurements that vary with serialization
    are omitted only within the compression metadata subtree; all other result
    data is retained unchanged.
    """
    if isinstance(result, dict):
        return {
            key: ({k: value[k] for k in (
                "status", "applied", "omitted", "filter", "raw_available",
                "raw_capture_complete", "filter_attempted", "raw_digest",
            ) if k in value} if isinstance(value, dict) else value)
            if key == "output_compression" else value
            for key, value in result.items()
        }
    if isinstance(result, list):
        return [stable_result_payload(item) for item in result]
    return result
