"""Small, opt-in retry policy for known upstream thinking-echo errors."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import re
import signal
import threading


_THINKING_ECHO = re.compile(
    r"^The (?P<quote>[\"'`]?)"
    r"(?:reasoning_content|reasoning content|content\[\]\.thinking)(?P=quote) "
    r"in the thinking mode must be passed back to the API\.?"
    r"(?:\s+\((?:request_id|trace_id)[:=]\s*[A-Za-z0-9._-]+"
    r"(?:[;,]\s*(?:request_id|trace_id)[:=]\s*[A-Za-z0-9._-]+)*\))?"
    r"(?:\s+\[trace_id=[A-Za-z0-9._-]+\])?$", re.IGNORECASE,
)


def thinking_echo_error(value) -> bool:
    if not isinstance(value, dict):
        return False
    error = value.get("error")
    if not isinstance(error, dict) or error.get("type") != "invalid_request_error":
        return False
    if error.get("code") not in (None, "invalid_request_error"):
        return False
    message = error.get("message")
    return isinstance(message, str) and bool(
        _THINKING_ECHO.fullmatch(message.strip()))


class RetryDeadlineError(RuntimeError):
    pass


@contextmanager
def request_deadline(seconds: float | None):
    """Interrupt pre-response I/O on the native Unix CLI; preserve other alarms."""
    if seconds is None:
        yield lambda: None
        return
    if (not hasattr(signal, "setitimer")
            or threading.current_thread() is not threading.main_thread()):
        raise RetryDeadlineError("LLM retry deadline requires the main thread of a Unix client")
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    if previous_timer[0] > 0:
        raise RetryDeadlineError("LLM retry deadline cannot replace an existing process alarm")
    previous_handler = signal.getsignal(signal.SIGALRM)
    restored = False

    def expired(signum, frame):
        raise RetryDeadlineError("LLM retry deadline exceeded")

    def cancel():
        nonlocal restored
        if restored:
            return
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        restored = True

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield cancel
    finally:
        cancel()


def retry_after(value: str | None, now: float) -> float | None:
    if not isinstance(value, str) or len(value) > 256:
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"[0-9]+", value):
            delay = float(value)
        else:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            delay = max(0.0, when.timestamp() - now)
        return delay if math.isfinite(delay) else None
    except (ValueError, TypeError, OverflowError, AttributeError):
        return None


def backoff(attempt: int, random_value: float) -> float:
    cap = min(2.0 ** min(attempt, 3), 8.0)
    return cap * (0.5 + 0.5 * max(0.0, min(random_value, 1.0)))
