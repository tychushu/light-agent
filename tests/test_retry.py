"""Behavioral tests for bounded completion-request retries."""
from __future__ import annotations

import json
import math
import signal
import tempfile
import time
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import httpx
from unittest.mock import patch

from termux_agent.agent import Agent
from termux_agent.client import Client, ClientError
from termux_agent.config import Config


THINKING_MESSAGE = (
    "The `content[].thinking` in the thinking mode must be passed back to the API."
)


def error_response(request, status=400, *, message=THINKING_MESSAGE,
                   error_type="invalid_request_error", code=None,
                   headers=None):
    return httpx.Response(
        status,
        json={"error": {"type": error_type, "code": code, "message": message}},
        headers=headers or {},
        request=request,
    )


def success_response(request):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        request=request,
    )


class RetryBehaviorTests(unittest.TestCase):
    def client(self, transport, *, enabled=True, **kwargs):
        config = Config(stream=False, retry_thinking_400=enabled)
        http = httpx.Client(transport=transport)
        self.addCleanup(http.close)
        return Client(config, http_client=http, **kwargs)

    def test_retry_is_opt_in_and_matches_structured_thinking_echo(self):
        attempts = []

        def respond(request):
            attempts.append(request)
            if len(attempts) == 1:
                return error_response(
                    request,
                    message=(THINKING_MESSAGE + " (request_id: req_abc123)"),
                    code="invalid_request_error",
                )
            return success_response(request)

        client = self.client(httpx.MockTransport(respond), sleeper=lambda _: None)
        events = []
        client.on_retry = events.append

        result = client.chat([], [])

        self.assertEqual(result["content"], "ok")
        self.assertEqual(client.last_attempts, 2)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["attempt"], 2)
        self.assertEqual(events[0]["max_attempts"], 3)
        self.assertEqual(events[0]["status"], 400)
        self.assertEqual(events[0]["cause"], "thinking-echo")

        disabled_attempts = []

        def disabled_respond(request):
            disabled_attempts.append(request)
            return error_response(request)

        disabled = self.client(httpx.MockTransport(disabled_respond), enabled=False,
                               sleeper=lambda _: None)
        with self.assertRaises(ClientError):
            disabled.chat([], [])
        self.assertEqual(len(disabled_attempts), 1)

    def test_default_off_preserves_legacy_status_backoff(self):
        attempts, sleeps = [], []

        def respond(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(503, request=request)
            if len(attempts) == 2:
                return httpx.Response(504, request=request)
            return success_response(request)

        config = Config(stream=False)
        self.assertFalse(config.retry_thinking_400)
        client = self.client(httpx.MockTransport(respond), enabled=False,
                             sleeper=sleeps.append)
        self.assertEqual(client.chat([], [])["content"], "ok")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [0.25, 0.5])

    def test_default_off_does_not_retry_the_thinking_400(self):
        attempts = []

        def respond(request):
            attempts.append(request)
            return error_response(request)

        http = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(http.close)
        client = Client(Config(stream=False), http_client=http,
                        sleeper=lambda _: self.fail("unexpected retry"))
        with self.assertRaises(ClientError):
            client.chat([], [])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(client.last_attempts, 1)

    def test_http_500_recovers_with_or_without_thinking_retry_in_both_stream_modes(self):
        for enabled in (False, True):
            for stream in (False, True):
                with self.subTest(enabled=enabled, stream=stream):
                    attempts, sleeps, events, chunks = [], [], [], []

                    def respond(request):
                        attempts.append(request.content)
                        if len(attempts) == 1:
                            return error_response(request, 500, error_type="api_error",
                                                  message="Service temporarily unavailable")
                        if stream:
                            return httpx.Response(
                                200, request=request,
                                headers={"content-type": "text/event-stream"},
                                content=(b'data: {"choices":[{"delta":{"content":"ok"},'
                                         b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'),
                            )
                        return success_response(request)

                    client = self.client(httpx.MockTransport(respond), enabled=enabled,
                                         sleeper=sleeps.append, randomizer=lambda: 0.0)
                    client.config.stream = stream
                    client.on_retry = events.append
                    client.on_stream = lambda kind, text: chunks.append((kind, text))

                    self.assertEqual(client.chat([{"role": "user", "content": "test"}], [])
                                     ["content"], "ok")
                    self.assertEqual(client.last_attempts, 2)
                    self.assertEqual(len(attempts), 2)
                    self.assertEqual(attempts[0], attempts[1])
                    self.assertEqual(sleeps, [0.5 if enabled else 0.25])
                    self.assertEqual([event["status"] for event in events],
                                     [500] if enabled else [])
                    self.assertEqual(chunks, [("content", "ok")] if stream else [])

    def test_persistent_http_500_stops_after_three_attempts(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                attempts, sleeps = [], []

                def respond(request):
                    attempts.append(request)
                    return error_response(request, 500, error_type="api_error",
                                          message="Service temporarily unavailable private body")

                client = self.client(httpx.MockTransport(respond), enabled=enabled,
                                     sleeper=sleeps.append, randomizer=lambda: 0.0)
                with self.assertRaises(ClientError) as raised:
                    client.chat([], [])
                self.assertEqual(client.last_attempts, 3)
                self.assertEqual(len(attempts), 3)
                self.assertEqual(sleeps, [0.5, 1.0] if enabled else [0.25, 0.5])
                self.assertIn("HTTP 500", str(raised.exception))
                self.assertIn("type=api_error", str(raised.exception))
                self.assertNotIn("private body", str(raised.exception))

    def test_http_500_thinking_400_and_gateway_errors_share_three_attempts(self):
        attempts, events = [], []

        def respond(request):
            attempts.append(request.content)
            if len(attempts) == 1:
                return error_response(request, 500, error_type="api_error",
                                      message="Service temporarily unavailable")
            if len(attempts) == 2:
                return error_response(request)
            return httpx.Response(503, request=request)

        client = self.client(httpx.MockTransport(respond), sleeper=lambda _: None)
        client.on_retry = events.append
        with self.assertRaisesRegex(ClientError, "HTTP 503"):
            client.chat([], [])
        self.assertEqual(client.last_attempts, 3)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(attempts, [attempts[0]] * 3)
        self.assertEqual([event["status"] for event in events], [500, 400])

    def test_http_500_after_tool_result_does_not_execute_tool_again(self):
        attempts = []

        def respond(request):
            attempts.append(request.content)
            if len(attempts) == 1:
                return httpx.Response(200, request=request, json={"choices": [{"message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "call_once", "type": "function",
                                    "function": {"name": "read_file",
                                                 "arguments": '{"path":"/tmp/test"}'}}],
                }}]})
            if len(attempts) < 4:
                return error_response(request, 500, error_type="api_error",
                                      message="Service temporarily unavailable")
            return success_response(request)

        config = Config(stream=False, retry_thinking_400=True,
                        approval_policy="never", max_steps=2)
        http = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(http.close)
        client = Client(config, http_client=http, sleeper=lambda _: None)
        agent = Agent(config, client=client, base_system="test")
        with patch("termux_agent.agent.execute", return_value={"text": "file result"}) as execute:
            self.assertEqual(agent.run("read the file"), "ok")

        execute.assert_called_once_with("read_file", {"path": "/tmp/test"},
                                        config.max_output_chars)
        self.assertEqual(len(attempts), 4)
        self.assertEqual(client.last_attempts, 3)
        self.assertEqual(attempts[1:], [attempts[1]] * 3)
        history = json.loads(attempts[-1])["messages"]
        tool_results = [message for message in history if message.get("role") == "tool"]
        self.assertEqual(len(tool_results), 1)
        self.assertEqual(tool_results[0]["tool_call_id"], "call_once")
        self.assertIn("file result", tool_results[0]["content"])

    def test_nonmatching_400s_and_other_statuses_are_not_retried(self):
        cases = [
            (400, "The content field is invalid.", "invalid_request_error", None),
            (400, THINKING_MESSAGE, "bad_request", None),
            (400, THINKING_MESSAGE, "invalid_request_error", "content_blocked"),
            (401, THINKING_MESSAGE, "invalid_request_error", None),
            (402, THINKING_MESSAGE, "invalid_request_error", None),
            (403, THINKING_MESSAGE, "invalid_request_error", None),
            (405, THINKING_MESSAGE, "invalid_request_error", None),
            (429, THINKING_MESSAGE, "invalid_request_error", None),
            (501, THINKING_MESSAGE, "invalid_request_error", None),
            (505, THINKING_MESSAGE, "invalid_request_error", None),
        ]
        for status, message, error_type, code in cases:
            with self.subTest(status=status, message=message, code=code):
                attempts = []

                def respond(request):
                    attempts.append(request)
                    return error_response(request, status, message=message,
                                          error_type=error_type, code=code)

                client = self.client(httpx.MockTransport(respond),
                                     sleeper=lambda _: self.fail("unexpected retry"))
                with self.assertRaises(ClientError):
                    client.chat([], [])
                self.assertEqual(len(attempts), 1)
                self.assertEqual(client.last_attempts, 1)

    def test_connect_and_gateway_failures_share_three_attempt_limit(self):
        attempts = []

        def respond(request):
            attempts.append(request)
            if len(attempts) == 1:
                raise httpx.ConnectError("offline", request=request)
            if len(attempts) == 2:
                return httpx.Response(503, request=request)
            return error_response(request)

        client = self.client(httpx.MockTransport(respond), sleeper=lambda _: None)
        with self.assertRaises(ClientError):
            client.chat([], [])
        self.assertEqual(len(attempts), 3)
        self.assertEqual(client.last_attempts, 3)

    def test_retry_after_wait_over_budget_fails_without_early_request(self):
        for status in (400, 500):
            with self.subTest(status=status):
                attempts, sleeps = [], []

                def respond(request):
                    attempts.append(request)
                    return error_response(request, status, headers={"Retry-After": "21"})

                config = Config(stream=False, retry_thinking_400=True,
                                retry_wait_budget=20.0)
                http = httpx.Client(transport=httpx.MockTransport(respond))
                self.addCleanup(http.close)
                client = Client(config, http_client=http, sleeper=sleeps.append,
                                clock=lambda: 100.0, wall_clock=lambda: 1_700_000_000.0,
                                randomizer=lambda: 0.0)

                with self.assertRaises(ClientError):
                    client.chat([], [])
                self.assertEqual(len(attempts), 1)
                self.assertEqual(sleeps, [])
                self.assertEqual(client.last_attempts, 1)

    def test_cumulative_wait_budget_and_deadline_stop_before_next_request(self):
        attempts, sleeps = [], []

        def respond(request):
            attempts.append(request)
            if len(attempts) == 1:
                return error_response(request)
            return error_response(request, headers={"Retry-After": "1"})

        now = [0.0]

        def sleep(delay):
            sleeps.append(delay)
            now[0] += delay

        config = Config(stream=False, retry_thinking_400=True,
                        retry_wait_budget=1.4)
        http = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(http.close)
        client = Client(config, http_client=http, sleeper=sleep,
                        clock=lambda: now[0], wall_clock=lambda: 0.0,
                        randomizer=lambda: 0.0)
        with self.assertRaises(ClientError):
            client.chat([], [])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [0.5])
        self.assertEqual(client.last_attempts, 2)

        # A wait that would consume the remaining overall deadline is rejected
        # before sleeping or sending another request.
        deadline_attempts, deadline_sleeps = [], []

        def deadline_respond(request):
            deadline_attempts.append(request)
            return httpx.Response(503, request=request)

        deadline_clock = [0.0]

        def deadline_sleep(delay):
            deadline_sleeps.append(delay)
            deadline_clock[0] += delay

        short = Config(stream=False, retry_thinking_400=True, timeout=0.4)
        deadline_http = httpx.Client(transport=httpx.MockTransport(deadline_respond))
        self.addCleanup(deadline_http.close)
        deadline_client = Client(short, http_client=deadline_http,
                                 sleeper=deadline_sleep,
                                 clock=lambda: deadline_clock[0],
                                 randomizer=lambda: 0.0)
        with self.assertRaises(ClientError):
            deadline_client.chat([], [])
        self.assertEqual(len(deadline_attempts), 1)
        self.assertEqual(deadline_sleeps, [])

    def test_keyboard_interrupt_during_backoff_propagates_without_retry(self):
        attempts = []

        def respond(request):
            attempts.append(request)
            return error_response(request)

        client = self.client(httpx.MockTransport(respond),
                             sleeper=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        with self.assertRaises(KeyboardInterrupt):
            client.chat([], [])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(client.last_attempts, 1)

    def test_retry_after_http_date_is_honored_within_budget(self):
        attempts, sleeps = [], []
        wall_now = datetime(2026, 10, 10, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        retry_at = datetime(2026, 10, 10, 0, 0, 3, tzinfo=timezone.utc)

        def respond(request):
            attempts.append(request)
            if len(attempts) == 1:
                return error_response(request, headers={
                    "Retry-After": format_datetime(retry_at, usegmt=True)
                })
            return success_response(request)

        config = Config(stream=False, retry_thinking_400=True,
                        retry_wait_budget=20.0)
        http = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(http.close)
        client = Client(config, http_client=http, sleeper=sleeps.append,
                        clock=lambda: 50.0, wall_clock=lambda: wall_now,
                        randomizer=lambda: 0.0)

        self.assertEqual(client.chat([], [])["content"], "ok")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [3.0])

    def test_invalid_retry_after_values_use_local_bounded_jitter(self):
        for value in ("-1", "NaN", "Inf", "1.5"):
            with self.subTest(retry_after=value):
                attempts, sleeps = [], []

                def respond(request):
                    attempts.append(request)
                    if len(attempts) == 1:
                        return error_response(request, headers={"Retry-After": value})
                    return success_response(request)

                config = Config(stream=False, retry_thinking_400=True)
                http = httpx.Client(transport=httpx.MockTransport(respond))
                self.addCleanup(http.close)
                client = Client(config, http_client=http, sleeper=sleeps.append,
                                randomizer=lambda: 0.0)
                self.assertEqual(client.chat([], [])["content"], "ok")
                self.assertEqual(len(attempts), 2)
                self.assertEqual(sleeps, [0.5])

    def test_stream_retry_preserves_wire_history_and_emits_only_successful_stream(self):
        attempts, retry_events, stream_events, source_changes = [], [], [], []
        original_messages = [
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "prior answer",
             "reasoning_content": "prior private reasoning",
             "tool_calls": [{"id": "call_prior", "type": "function",
                             "function": {"name": "read_file",
                                          "arguments": '{"path":"/tmp/a"}'}}]},
            {"role": "tool", "tool_call_id": "call_prior",
             "content": '{"text":"prior result"}'},
        ]
        messages = json.loads(json.dumps(original_messages))

        def respond(request):
            attempts.append((request.content, json.loads(request.content)))
            if len(attempts) == 1:
                messages[0]["content"] = "caller mutation after request was built"
                source_changes.append(True)
                return error_response(request)
            body = (
                'data: {"choices":[{"delta":{"reasoning_content":"fresh reason"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"fresh answer"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":12,"completion_tokens":5,"total_tokens":17}}\n\n'
                'data: [DONE]\n\n'
            )
            return httpx.Response(200, content=body,
                                  headers={"content-type": "text/event-stream"},
                                  request=request)

        config = Config(stream=True, retry_thinking_400=True)
        http = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(http.close)
        client = Client(config, http_client=http, sleeper=lambda _: None,
                        randomizer=lambda: 0.0)
        client.on_retry = retry_events.append
        client.on_stream = lambda kind, text: stream_events.append((kind, text))

        result = client.chat(messages, [])

        self.assertEqual(result["reasoning_content"], "fresh reason")
        self.assertEqual(result["content"], "fresh answer")
        self.assertEqual(client.last_usage,
                         {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17})
        self.assertEqual(stream_events, [
            ("reasoning", "fresh reason"), ("content", "fresh answer")
        ])
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0][0], attempts[1][0])
        self.assertEqual(attempts[0][1]["messages"], original_messages)
        self.assertEqual(attempts[1][1]["messages"], original_messages)
        self.assertEqual(messages[0]["content"], "caller mutation after request was built")
        self.assertEqual(source_changes, [True])

    def test_html_invalid_oversized_and_wrong_sentence_400_are_not_retried(self):
        responses = [
            lambda request: httpx.Response(
                400, content=b"<html>gateway</html>",
                headers={"content-type": "text/html"}, request=request),
            lambda request: httpx.Response(
                400, content=b'{"error":{"type":"invalid_request_error",',
                headers={"content-type": "application/json"}, request=request),
            lambda request: httpx.Response(
                400, content=(b'{"error":{"type":"invalid_request_error","code":null,"message":"'
                              + THINKING_MESSAGE.encode() + b'"' + b" " * 4096),
                headers={"content-type": "application/json"}, request=request),
            lambda request: error_response(
                request, message="The content[].thinking field must be passed back."),
        ]
        for make_response in responses:
            with self.subTest(response=make_response):
                attempts = []

                def respond(request):
                    attempts.append(request)
                    return make_response(request)

                config = Config(stream=False, retry_thinking_400=True)
                http = httpx.Client(transport=httpx.MockTransport(respond))
                self.addCleanup(http.close)
                client = Client(config, http_client=http,
                                sleeper=lambda _: self.fail("unexpected retry"))
                with self.assertRaises(ClientError):
                    client.chat([], [])
                self.assertEqual(len(attempts), 1)
                self.assertEqual(client.last_attempts, 1)

    def test_profile_does_not_inherit_top_level_retry_opt_in_and_budget_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.toml"
            config_path.write_text(
                'retry_thinking_400 = true\n'
                'retry_wait_budget = 9.0\n'
                '\n[apis.remote]\n'
                'base_url = "https://api.example/v1"\n'
                'model = "test-model"\n',
                encoding="utf-8",
            )
            profile = Config.load(config_path, "remote")
            self.assertFalse(profile.retry_thinking_400)
            self.assertEqual(profile.retry_wait_budget, 9.0)

        for valid in (0, 60):
            config = Config(retry_wait_budget=valid)
            config.validate()
        for invalid in (-0.01, 60.01, True, math.nan, math.inf, -math.inf):
            with self.subTest(budget=invalid):
                with self.assertRaises(ValueError):
                    Config(retry_wait_budget=invalid).validate()

    @unittest.skipUnless(hasattr(signal, "setitimer") and hasattr(signal, "ITIMER_REAL"),
                         "Unix real-time interval timers are required")
    def test_pre_response_deadline_interrupts_transport_and_restores_alarm_state(self):
        before_handler = signal.getsignal(signal.SIGALRM)
        before_timer = signal.getitimer(signal.ITIMER_REAL)
        if before_timer[0] > 0:
            self.skipTest("an unrelated process alarm is already active")
        after_sleep = []
        attempts = []

        def respond(request):
            attempts.append(request)
            time.sleep(0.15)
            after_sleep.append(True)
            return success_response(request)

        config = Config(stream=False, retry_thinking_400=True, timeout=0.02)
        http = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(http.close)
        client = Client(config, http_client=http)

        with self.assertRaises(ClientError):
            client.chat([], [])

        self.assertEqual(len(attempts), 1)
        self.assertEqual(after_sleep, [])
        self.assertIs(signal.getsignal(signal.SIGALRM), before_handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    @unittest.skipUnless(hasattr(signal, "setitimer") and hasattr(signal, "ITIMER_REAL"),
                         "Unix real-time interval timers are required")
    def test_successful_sse_streaming_callback_runs_after_alarm_is_restored(self):
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        if previous_timer[0] > 0:
            self.skipTest("an unrelated process alarm is already active")
        body = (
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        )

        def respond(request):
            return httpx.Response(200, content=body,
                                  headers={"content-type": "text/event-stream"},
                                  request=request)

        http = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(http.close)
        client = Client(Config(stream=True, retry_thinking_400=True), http_client=http)
        callback_state = []

        def on_stream(kind, value):
            callback_state.append((kind, value, signal.getsignal(signal.SIGALRM),
                                   signal.getitimer(signal.ITIMER_REAL)))

        client.on_stream = on_stream
        result = client.chat([], [])
        self.assertEqual(result["content"], "ok")
        self.assertEqual(len(callback_state), 1)
        kind, value, handler, timer = callback_state[0]
        self.assertEqual((kind, value), ("content", "ok"))
        self.assertIs(handler, previous_handler)
        self.assertEqual(timer, (0.0, 0.0))

    @unittest.skipUnless(hasattr(signal, "setitimer") and hasattr(signal, "ITIMER_REAL"),
                         "Unix real-time interval timers are required")
    def test_existing_process_alarm_is_not_replaced_or_transport_started(self):
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        if previous_timer[0] > 0:
            self.skipTest("an unrelated process alarm is already active")
        attempts = []
        signal.setitimer(signal.ITIMER_REAL, 5.0)
        try:
            http = httpx.Client(transport=httpx.MockTransport(
                lambda request: (attempts.append(request) or success_response(request))))
            try:
                client = Client(Config(stream=False, retry_thinking_400=True),
                                http_client=http)
                with self.assertRaises(ClientError):
                    client.chat([], [])
            finally:
                http.close()
            remaining, interval = signal.getitimer(signal.ITIMER_REAL)
            self.assertGreater(remaining, 0)
            self.assertEqual(interval, 0.0)
            self.assertEqual(attempts, [])
            self.assertIs(signal.getsignal(signal.SIGALRM), previous_handler)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)

    def test_started_sse_stream_is_never_replayed_after_read_error(self):
        class InterruptedStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                raise httpx.ReadError("stream broke")

        for initial_500 in (False, True):
            with self.subTest(initial_500=initial_500):
                attempts = []

                def respond(request):
                    attempts.append(request)
                    if initial_500 and len(attempts) == 1:
                        return error_response(request, 500, error_type="api_error")
                    return httpx.Response(
                        200,
                        headers={"content-type": "text/event-stream"},
                        stream=InterruptedStream(),
                        request=request,
                    )

                config = Config(stream=True, retry_thinking_400=True)
                http = httpx.Client(transport=httpx.MockTransport(respond))
                self.addCleanup(http.close)
                client = Client(config, http_client=http, sleeper=lambda _: None)
                chunks = []
                client.on_stream = lambda kind, value: chunks.append((kind, value))

                with self.assertRaises(ClientError):
                    client.chat([], [])
                self.assertEqual(len(attempts), 2 if initial_500 else 1)
                self.assertEqual(client.last_attempts, len(attempts))
                self.assertEqual(chunks, [("content", "partial")])

    def test_tool_runs_once_only_after_complete_assistant_tool_call_response(self):
        attempts = []

        def respond(request):
            attempts.append(json.loads(request.content))
            if len(attempts) == 1:
                return error_response(request)
            if len(attempts) == 2:
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "call_once", "type": "function",
                                    "function": {"name": "read_file",
                                                 "arguments": json.dumps({"path": "/tmp/test"})}}],
                }}]}, request=request)
            return success_response(request)

        config = Config(stream=False, retry_thinking_400=True,
                        approval_policy="never", max_steps=4)
        http = httpx.Client(transport=httpx.MockTransport(respond))
        self.addCleanup(http.close)
        client = Client(config, http_client=http, sleeper=lambda _: None)
        agent = Agent(config, client=client, base_system="test")
        with patch("termux_agent.agent.execute", return_value={"text": "ok"}) as execute:
            self.assertEqual(agent.run("read the file"), "ok")

        self.assertEqual(len(attempts), 3)
        execute.assert_called_once_with("read_file", {"path": "/tmp/test"},
                                        config.max_output_chars)
        # The failed response never entered history; only the complete tool-call
        # assistant turn is present in the following request.
        history = attempts[2]["messages"]
        tool_turns = [message for message in history
                      if message.get("role") == "assistant" and message.get("tool_calls")]
        self.assertEqual(len(tool_turns), 1)
        self.assertEqual(tool_turns[0]["tool_calls"][0]["id"], "call_once")
        self.assertEqual(sum(message.get("role") == "tool" for message in history), 1)


if __name__ == "__main__":
    unittest.main()
