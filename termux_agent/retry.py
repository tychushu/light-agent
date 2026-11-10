"""Bounded transient retries and opt-in thinking-echo recovery."""
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


class RetryDeadlineSetupError(RetryDeadlineError):
    """A permanent runtime refusal before any HTTP attempt, not an elapsed deadline."""


def rate_limit_error(value) -> bool:
    """Only explicitly temporary rate limits; never infer from error prose."""
    if not isinstance(value, dict):
        return False
    error = value.get('error') if isinstance(value.get('error'), dict) else value
    normalize = lambda item: str(item or '').strip().lower().replace('-', '_')
    kind, code = normalize(error.get('type')), normalize(error.get('code'))
    denied = {'insufficient_quota', 'quota_exceeded', 'billing_hard_limit_reached',
              'insufficient_balance', 'authentication_error', 'invalid_api_key',
              'permission_denied', 'content_blocked', 'account_deactivated'}
    if kind in denied or code in denied:
        return False
    codes = {'rate_limit_exceeded', 'rate_limit_error', 'requests_rate_limit_exceeded',
             'tokens_rate_limit_exceeded', 'ratelimitreached', 'too_many_requests', '429'}
    return code in codes or (kind in (codes | {'requests', 'tokens'}) and not code)


def duration_seconds(value):
    """Bounded duration syntax used by token/request reset headers, never prose."""
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    value = value.strip().lower()
    if value == '0':
        return 0.0
    total, end = 0.0, 0
    units = {'h': 3600, 'm': 60, 's': 1, 'ms': .001}
    for match in re.finditer(r'(\d+(?:\.\d+)?)(ms|[hms])', value):
        if match.start() != end:
            return None
        total += float(match[1]) * units[match[2]]
        end = match.end()
    return total if end == len(value) and end and math.isfinite(total) else None


def rate_limit_axis(value):
    error = value.get('error', value) if isinstance(value, dict) else {}
    if not isinstance(error, dict):
        return None
    fields = [str(error.get(name, '')).lower() for name in ('code', 'type')]
    for axis in ('requests', 'tokens'):
        if any(field == axis or field.startswith(axis + '_') for field in fields):
            return axis
    return None


def server_retry_delay(headers, now, *, rate_limited=False, axis=None):
    """Explicit Retry-After takes priority over bucket-full reset estimates."""
    if isinstance(headers, str) or headers is None:
        return retry_after(headers, now)
    headers = {str(key).lower(): value for key, value in headers.items()}
    values = [retry_after(headers.get('retry-after'), now)]
    milliseconds = headers.get('retry-after-ms')
    if isinstance(milliseconds, str) and len(milliseconds) <= 128 and re.fullmatch(r'\d+(?:\.\d+)?', milliseconds):
        delay = float(milliseconds) / 1000
        if math.isfinite(delay):
            values.append(delay)
    explicit = [value for value in values if value is not None]
    if explicit:
        return max(explicit)
    if rate_limited:
        for bucket in (axis,) if axis in ('requests', 'tokens') else ('requests', 'tokens'):
            remaining = headers.get('x-ratelimit-remaining-' + bucket)
            if bucket == 'requests' and remaining is not None and axis is None:
                try:
                    if float(remaining) > 0:
                        continue
                except (ValueError, TypeError):
                    pass
            values.append(duration_seconds(headers.get('x-ratelimit-reset-' + bucket)))
    return max((v for v in values if v is not None), default=None)


class _DeadlineScope:
    """One armed SIGALRM period.

    Each period independently captures and later restores exactly the signal
    state it replaced, so an alarm installed by a callback after ``accepted()``
    released the previous scope is preserved rather than overwritten/cleared.
    ``_closed`` is set before any signal operation that can raise the timeout,
    so a signal delivered synchronously while arming still finds live cleanup.
    """

    def __init__(self, expired):
        # Detect external ownership BEFORE mutating signal state: refuse to
        # replace an alarm we do not own (a permanent setup refusal, not an
        # elapsed deadline).
        self._timer = signal.getitimer(signal.ITIMER_REAL)
        if self._timer[0] > 0:
            raise RetryDeadlineSetupError(
                "LLM retry deadline cannot replace an existing process alarm")
        self._handler = signal.getsignal(signal.SIGALRM)
        self._expired = expired
        self._closed = False

    def install(self, seconds):
        # Cleanup state (registered scope, _closed False) is already in place.
        signal.signal(signal.SIGALRM, self._expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)

    def close(self):
        if self._closed:
            return
        self._closed = True
        # Preserve an alarm/handler a callback installed after we armed: only
        # tear down signal state we still own.
        if signal.getsignal(signal.SIGALRM) is not self._expired:
            return
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, self._handler)
        signal.setitimer(signal.ITIMER_REAL, *self._timer)


class _DeadlineGuard:
    """Callable guard: ``()`` releases the alarm, ``rearm`` opens a fresh scope."""

    def __init__(self, expired):
        self._expired = expired
        self._scope = None

    def __call__(self):
        self._release()

    def rearm(self, seconds):
        self._release()
        self._arm(seconds)

    def start(self, seconds):
        self._arm(seconds)

    def _arm(self, seconds):
        if self._expired is None:  # request_deadline(None): no alarms at all
            return
        scope = _DeadlineScope(self._expired)  # may refuse before any mutation
        self._scope = scope                     # cleanup ready BEFORE install
        scope.install(seconds)                  # may raise via synchronous signal

    def _release(self):
        scope, self._scope = self._scope, None
        if scope is not None:
            scope.close()

    def close(self):
        self._release()


@contextmanager
def request_deadline(seconds: float | None):
    """Interrupt pre-response I/O on the native Unix CLI; preserve other alarms.

    Calling the yielded guard cancels the one-shot alarm once a response body is
    accepted. A same-request retry can ``rearm`` it; each arm is an independent
    scope that captures/restores its own signal state and refuses to overwrite
    an alarm owned by someone else.
    """
    if seconds is None:
        yield _DeadlineGuard(None)
        return
    if (not hasattr(signal, "setitimer")
            or threading.current_thread() is not threading.main_thread()):
        raise RetryDeadlineSetupError("LLM retry deadline requires the main thread of a Unix client")

    def expired(signum, frame):
        raise RetryDeadlineError("LLM retry deadline exceeded")

    guard = _DeadlineGuard(expired)
    try:
        guard.start(seconds)
        yield guard
    finally:
        guard.close()


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


def backoff(attempt: int, random_value: float, *, rate_limited=False) -> float:
    cap = min(5 * 2.0 ** min(attempt, 4), 60.0) if rate_limited else min(2.0 ** min(attempt, 3), 8.0)
    return cap * (0.5 + 0.5 * max(0.0, min(random_value, 1.0)))
