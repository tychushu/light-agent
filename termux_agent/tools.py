"""The four small tools exposed to the model."""
from __future__ import annotations

import os
import shutil
import signal
import selectors
import subprocess
import tempfile
import math
import time
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


TOOLS = [
    {"type": "function", "function": {"name": "shell", "description": "Run a non-interactive Termux shell command.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "number"}}, "required": ["command"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "read_file", "description": "Read a bounded text window by line number or byte offset.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1}}, "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write UTF-8 atomically; verified SHA-256 confirms the write without returning text.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "edit_file", "description": "Replace exact text only at the expected count; return verified SHA-256, not text.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "expected_replacements": {"type": "integer", "minimum": 1}}, "required": ["path", "old_text", "new_text"], "additionalProperties": False}}},
]

WEB_FETCH_TOOL = {"type": "function", "function": {"name": "web_fetch", "description": "Fetch a web page through the Lightpanda headless browser (JavaScript executed) and return Markdown, HTML or a semantic text tree.", "parameters": {"type": "object", "properties": {"url": {"type": "string"}, "format": {"type": "string", "enum": ["markdown", "html", "tree"]}, "selector": {"type": "string"}, "wait_ms": {"type": "integer", "minimum": 0, "maximum": 60000}, "strip": {"type": "string", "enum": ["none", "clutter", "shell"]}}, "required": ["url"], "additionalProperties": False}}}

WEB_FETCH_FORMATS = {"markdown": "markdown", "html": "html", "tree": "semantic_tree_text"}
WEB_FETCH_STRIP = {"none": (), "clutter": ("clutter",), "shell": ("shell",)}
WEB_FETCH_DEFAULT_WAIT_MS = 5_000
WEB_FETCH_MAX_WAIT_MS = 60_000
WEB_FETCH_MAX_SECONDS = 120.0


def web_fetch_available(binary: str | None = None) -> bool:
    """True when the browser binary can be resolved; used to hide the tool."""
    return bool(_web_fetch_binary(binary))


def _web_fetch_binary(binary: str | None = None) -> str:
    candidate = binary or os.environ.get("TERMUX_AGENT_LIGHTPANDA", "lightpanda")
    if not candidate:
        return ""
    candidate = os.path.expanduser(candidate)
    if os.sep in candidate:
        return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else ""
    return shutil.which(candidate) or ""


def _web_fetch_process(command, env, timeout, output_limit):
    """Drain both pipes with bounded RAM; always reap the browser process group."""
    selector = selectors.DefaultSelector()
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, start_new_session=True)
    except (OSError, ValueError) as exc:
        selector.close()
        raise ValueError(f"cannot start lightpanda: {exc}") from None
    out, err = bytearray(), bytearray()
    deadline = time.monotonic() + timeout

    def signal_group(sig):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            if proc.poll() is None:
                try:
                    proc.send_signal(sig)
                except ProcessLookupError:
                    pass

    try:
        for label, pipe in (("out", proc.stdout), ("err", proc.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, label)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            for key, _ in selector.select(min(0.1, remaining)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                elif key.data == "out":
                    if len(out) + len(chunk) > output_limit:
                        raise ValueError(f"web_fetch browser output exceeds {output_limit} byte limit")
                    out.extend(chunk)
                else:
                    err.extend(chunk[-4096:])
                    del err[:-4096]
        proc.wait(timeout=max(0, deadline - time.monotonic()))
        return bytes(out), bytes(err), proc.returncode
    except subprocess.TimeoutExpired:
        raise ValueError(f"web_fetch timed out after {timeout:g}s") from None
    finally:
        # Always KILL remaining descendants, even if the group leader exited
        # after TERM or an interrupt. No second unbounded communicate() call.
        signal_group(signal.SIGTERM)
        try:
            proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        finally:
            signal_group(signal.SIGKILL)
            try:
                proc.wait(timeout=1)
            finally:
                selector.close()
                proc.stdout.close()
                proc.stderr.close()


def _web_fetch(args: dict, cap: int, binary: str | None = None,
               timeout: float | None = None) -> dict:
    """Load one http(s) URL in Lightpanda and return bounded page content."""
    if set(args) - {"url", "format", "selector", "wait_ms", "strip"}:
        raise ValueError("web_fetch accepts only url, format, selector, wait_ms and strip")
    url = args.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    url = url.strip()
    parsed = urlsplit(url)
    if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or
            any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)):
        raise ValueError("url must start with http:// or https://")
    parsed.port  # Reject malformed ports before launching the browser.
    fmt = args.get("format", "markdown")
    if not isinstance(fmt, str) or fmt not in WEB_FETCH_FORMATS:
        raise ValueError(f"format must be one of {sorted(WEB_FETCH_FORMATS)}")
    strip = args.get("strip", "none")
    if not isinstance(strip, str) or strip not in WEB_FETCH_STRIP:
        raise ValueError(f"strip must be one of {sorted(WEB_FETCH_STRIP)}")
    selector = args.get("selector")
    if selector is not None and (not isinstance(selector, str) or not selector.strip()):
        raise ValueError("selector must be a non-empty string when provided")
    wait_ms = args.get("wait_ms", WEB_FETCH_DEFAULT_WAIT_MS)
    if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or not 0 <= wait_ms <= WEB_FETCH_MAX_WAIT_MS:
        raise ValueError(f"wait_ms must be an integer between 0 and {WEB_FETCH_MAX_WAIT_MS}")
    exe = _web_fetch_binary(binary)
    if not exe:
        raise ValueError("lightpanda binary not found; set TERMUX_AGENT_LIGHTPANDA")
    if timeout is None:
        timeout = min(WEB_FETCH_MAX_SECONDS, max(15.0, wait_ms / 1000 + 20.0))
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("web_fetch timeout must be positive and finite")
    timeout = min(timeout, WEB_FETCH_MAX_SECONDS)

    command = [exe, "fetch", "--json", "--log-level", "error",
               "--dump", WEB_FETCH_FORMATS[fmt],
               "--wait-ms", str(wait_ms)]
    if fmt in ("markdown", "html"):
        # The flag is rejected for semantic_tree dumps; those are clipped below.
        # Over-read one UTF-8 character beyond the character cap so browser-side
        # byte clipping cannot silently turn a truncated page into truncated=false.
        command += ["--dump-max-bytes", str(cap * 4 + 4)]
    for mode in WEB_FETCH_STRIP[strip]:
        command += ["--strip-mode", mode]
    if selector is not None:
        command += ["--dump-selector", selector.strip()]
    command.append(url)

    env = os.environ.copy()
    # Upstream telemetry is on by default; never send it from this agent.
    env["LIGHTPANDA_DISABLE_TELEMETRY"] = "true"
    env.update({"TERM": "dumb", "CI": "1"})
    output_limit = min(8 * 1024 * 1024, max(65536, cap * 32 + 65536))
    out, err, returncode = _web_fetch_process(command, env, timeout, output_limit)
    text = out.decode("utf-8", "replace")
    stderr = err.decode("utf-8", "replace").strip()
    payload = None
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except ValueError:
            payload = None
    if isinstance(payload, dict) and isinstance(payload.get("results"), list) and payload["results"]:
        payload = payload["results"][0]
    failure = payload.get("error") if isinstance(payload, dict) else None
    if returncode != 0:
        detail = failure[-400:] if isinstance(failure, str) and failure else (stderr[-400:] or f"exit {returncode}")
        raise ValueError(f"lightpanda failed: {detail}")
    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, str):
        content = text
    truncated = len(content) > cap
    if truncated:
        content = _clip(content, cap)
    status = payload.get("http_status") if isinstance(payload, dict) else None
    if status is None and isinstance(payload, dict):
        status = payload.get("status")
    return {"url": payload.get("url") if isinstance(payload, dict) and isinstance(payload.get("url"), str) else url,
            "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
            "format": fmt, "content": content, "chars": len(content), "truncated": truncated}

MAX_FILE_BYTES = 10 * 1024 * 1024
DELEGATE_TOOL = {
    "type": "function", "function": {
        "name": "delegate",
        "description": "Delegate a self-contained subtask to an independent bounded worker. Include all necessary facts. Runs synchronously with the same API, directory and approvals; no nested delegation. Returns status, summary and usage.",
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "minLength": 1, "maxLength": 16000},
            "max_steps": {"type": "integer", "minimum": 1, "maximum": 12}},
            "required": ["task"], "additionalProperties": False}}}

MAX_EDIT_BYTES = 2 * 1024 * 1024


def _retain(sample: list, chunk: bytes, cap: int) -> None:
    """Keep a bounded head and tail while draining the complete pipe."""
    payload_cap = max(0, cap - len("\n[TRUNCATED]\n"))
    head_cap = payload_cap // 2
    tail_cap = payload_cap - head_cap
    head, tail, total = sample
    total += len(chunk)
    if len(head) < head_cap:
        head.extend(chunk[:head_cap - len(head)])
    if tail_cap:
        tail.extend(chunk)
        if len(tail) > tail_cap:
            del tail[:-tail_cap]
    sample[2] = total


def _clip(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    marker = "\n[TRUNCATED]\n"
    keep = max(0, cap - len(marker))
    head = keep // 2
    return text[:head] + marker + (text[-(keep-head):] if keep > head else "")


def _atomic_write(path: Path, data: bytes) -> None:
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"file content exceeds {MAX_FILE_BYTES} byte limit")
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o7777 if path.exists() else None
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _shell(args: dict, cap: int) -> dict:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    timeout = float(args.get("timeout", 30))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive")
    timeout = min(timeout, 3600)
    cwd = args.get("cwd")
    if cwd is not None and not Path(cwd).is_dir():
        raise NotADirectoryError(cwd)
    prefix = os.environ.get("PREFIX", "")
    bash = os.path.join(prefix, "bin/bash") if prefix and os.path.isfile(os.path.join(prefix, "bin/bash")) else shutil.which("bash") or "/bin/bash"
    env = os.environ.copy()
    env.update({"TERM": "dumb", "GIT_TERMINAL_PROMPT": "0", "CI": "1"})
    proc = subprocess.Popen([bash, "--noprofile", "--norc", "-c", command], cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    samples = {"out": [bytearray(), bytearray(), 0], "err": [bytearray(), bytearray(), 0]}
    budgets = {"out": cap // 2, "err": cap - cap // 2}
    selector = selectors.DefaultSelector()
    for name, pipe in (("out", proc.stdout), ("err", proc.stderr)):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, name)
    deadline = time.monotonic() + timeout
    timed_out = False
    def kill_group(sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            # The group can disappear between TERM and KILL; still reap the leader.
            if proc.poll() is None:
                try:
                    proc.send_signal(sig)
                except ProcessLookupError:
                    pass
    try:
        # Poll both pipes nonblocking. A setsid child holding a pipe open cannot hang us.
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
                if chunk:
                    _retain(samples[key.data], chunk, budgets[key.data])
                else:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
        if not timed_out:
            try:
                proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
        if timed_out:
            kill_group(signal.SIGTERM)
            time.sleep(0.1)
        # Kill descendants too, even when the leader already exited after TERM.
        kill_group(signal.SIGKILL)
        try:
            code = proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            code = proc.returncode
    except BaseException:
        kill_group(signal.SIGKILL)
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
    def render(sample: list, budget: int) -> str:
        head, tail, total = sample
        retained = len(head) + len(tail)
        overlap = max(0, retained - total)
        raw = head + tail[overlap:]
        if total <= budget - len("\n[TRUNCATED]\n"):
            return raw.decode("utf-8", "replace")
        marker = "\n[TRUNCATED]\n"
        return head.decode("utf-8", "replace") + marker + tail.decode("utf-8", "replace")
    out = render(samples["out"], budgets["out"])
    err = render(samples["err"], budgets["err"])
    if timed_out:
        err = (err + "\n[command timed out and process group was terminated]").strip()
    # Final character cap includes both streams and timeout text.
    combined = out + ("\n" if out and err else "") + err
    clipped = _clip(combined, cap)
    if clipped != combined:
        # Keep fields useful while guaranteeing their concatenated size is bounded.
        out, err = clipped, ""
    return {"exit_code": code, "stdout": out, "stderr": err}


def _read_file(args: dict, cap: int) -> dict:
    path = Path(args["path"])
    if ("start_line" in args or "end_line" in args or
            ("offset" not in args and "limit" not in args)):
        return _read_lines(path, args, cap)
    offset = int(args.get("offset", 0))
    requested = args.get("limit")
    limit = int(requested) if requested is not None else None
    if offset < 0 or (limit is not None and limit < 1):
        raise ValueError("offset must be non-negative and limit positive")
    with path.open("rb") as f:
        file_size = os.fstat(f.fileno()).st_size
        selected_end = min(file_size, offset + limit) if limit is not None else file_size
        selected_size = max(0, selected_end - offset)
        truncated = selected_size > cap
        if not truncated:
            f.seek(offset)
            raw = f.read(selected_size)
            value = raw.decode("utf-8", "replace")
            bytes_read = len(raw)
        else:
            head_size = max(1, (cap - len("\n[TRUNCATED]\n")) // 2)
            tail_size = cap - len("\n[TRUNCATED]\n") - head_size
            f.seek(offset)
            head = f.read(head_size)
            f.seek(selected_end - tail_size)
            tail = f.read(tail_size)
            value = head.decode("utf-8", "replace") + "\n[TRUNCATED]\n" + tail.decode("utf-8", "replace")
            bytes_read = len(head) + len(tail)
    next_offset = selected_end if not truncated else offset + len(head)
    return {"content": value, "offset": offset, "bytes_read": bytes_read,
            "truncated": truncated, "next_offset": next_offset}


def _read_lines(path: Path, args: dict, cap: int) -> dict:
    if "offset" in args or "limit" in args:
        raise ValueError("Use line numbers or byte offset/limit, not both")
    start = int(args.get("start_line", 1))
    end = int(args.get("end_line", start + 49))
    if start < 1 or end < start or end - start >= 5000:
        raise ValueError("line range must be ordered and no wider than 5000 lines")
    out, used, line_no, last_line, truncated = [], 0, 0, 0, False
    with path.open("r", encoding="utf-8", errors="replace") as f:
        while line_no < start - 1:
            chunk = f.readline(8192)
            if not chunk:
                break
            if chunk.endswith("\n"):
                line_no += 1
        while line_no < end:
            chunk = f.readline(cap + 1)
            if not chunk:
                break
            line_no += 1
            too_long = len(chunk) > cap
            prefix = f"{line_no}: "
            if too_long:
                marker = " [LINE TRUNCATED]"
                available = max(0, cap - used - len(prefix) - len(marker) - 1)
                head_size = available // 2
                tail_size = available - head_size
                head = chunk[:head_size]
                tail = chunk[-tail_size:] if tail_size else ""
                rest = chunk
                while rest and not rest.endswith("\n"):
                    rest = f.readline(8192)
                    tail = (tail + rest)[-tail_size:] if tail_size else ""
                chunk = head.rstrip("\n") + marker + tail.rstrip("\r\n") + "\n"
                truncated = True
            row = prefix + chunk
            if len(row) > cap - used:
                truncated = True
                break
            out.append(row)
            used += len(row)
            last_line = line_no
    value = "".join(out)
    if truncated and len(value) + len("[TRUNCATED]") <= cap:
        value += "[TRUNCATED]"
    return {"content": value, "start_line": start, "end_line": last_line or None,
            "truncated": truncated, "next_line": last_line + 1 if last_line else start}


def _write_file(args: dict) -> dict:
    content = args["content"]
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    data = content.encode("utf-8")
    path = Path(args["path"])
    _atomic_write(path, data)
    return {**_write_verification(path, data), "chars_written": len(content)}


def _write_verification(path: Path, data: bytes) -> dict:
    result = {"path": str(path), "bytes_written": len(data), "verified": False}
    expected = hashlib.sha256(data).hexdigest()
    try:
        with path.open("rb") as stream:
            if os.fstat(stream.fileno()).st_size != len(data):
                result["error"] = "File size changed before hash verification"
                return result
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        result["error"] = f"File was written but hash verification failed: {type(exc).__name__}"
        return result
    result["sha256"] = actual
    result["verified"] = actual == expected
    if actual != expected:
        result["error"] = "File changed before hash verification; written content is not confirmed"
    return result


def _edit_file(args: dict) -> dict:
    path = Path(args["path"])
    old, new = args["old_text"], args["new_text"]
    expected = args.get("expected_replacements", 1)
    if not isinstance(old, str) or not isinstance(new, str):
        raise ValueError("old_text and new_text must be strings")
    if not old:
        raise ValueError("old_text must not be empty")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        raise ValueError("expected_replacements must be a positive integer")
    with path.open("rb") as f:
        data = f.read(MAX_EDIT_BYTES + 1)
    if len(data) > MAX_EDIT_BYTES:
        raise ValueError(f"edit limited to files <= {MAX_EDIT_BYTES} bytes")
    source = data.decode("utf-8")
    count = source.count(old)
    if count != expected:
        raise ValueError(f"expected {expected} exact match(es), found {count}; file unchanged")
    result = source.replace(old, new)
    data = result.encode("utf-8")
    _atomic_write(path, data)
    return {**_write_verification(path, data), "replacements": count,
            "chars_written": len(result)}


def execute(name: str, args: dict, max_output_chars: int = 20000, *, lightpanda_binary: str | None = None) -> dict:
    """Run a named tool and turn failures into compact model-readable results."""
    try:
        if not isinstance(args, dict):
            raise ValueError("tool arguments must be an object")
        cap = int(max_output_chars)
        if cap < len("\n[TRUNCATED]\n") + 8:
            raise ValueError("max_output_chars is too small for truncation marker")
        if name == "shell":
            return _shell(args, cap)
        if name == "read_file":
            return _read_file(args, cap)
        if name == "write_file":
            return _write_file(args)
        if name == "edit_file":
            return _edit_file(args)
        if name == "web_fetch":
            return _web_fetch(args, cap, binary=lightpanda_binary)
        raise ValueError(f"unknown tool: {name}")
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
