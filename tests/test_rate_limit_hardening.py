"""Rate-limit hints, bounded waits and accepted-response non-replay."""
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from termux_agent.agent import Agent
from termux_agent.client import Client, ClientError, RateLimitError
from termux_agent.config import Config
from termux_agent.retry import duration_seconds, rate_limit_error, server_retry_delay


def ok():
    return httpx.Response(200,json={'choices':[{'message':{'content':'done'}}]})


class RateHardeningTests(unittest.TestCase):
    def test_temporary_codes_allow_generic_gateway_types_but_never_override_quota_or_blocking(self):
        for value in ({'error':{'code':'rate_limit_exceeded','type':'new_api_error'}},
                      {'error':{'code':'RateLimitReached','type':'api_error'}},
                      {'code':'tokens_rate_limit_exceeded'}, {'error':{'code':429}}):
            self.assertTrue(rate_limit_error(value),value)
        for value in ({'error':{'type':'insufficient_quota','code':'rate_limit_exceeded'}},
                      {'error':{'type':'rate_limit_error','code':'billing_hard_limit_reached'}},
                      {'error':{'type':'rate_limit_error','code':'content-blocked'}},
                      {'error':{'type':'api_error','message':'rate limit'}},None):
            self.assertFalse(rate_limit_error(value),value)

    def test_reset_duration_parser_is_finite_strict_and_bounded(self):
        for raw,seconds in (('1m30s',90),('250ms',.25),('1h2m3.5s',3723.5),('0',0)):
            self.assertAlmostEqual(duration_seconds(raw),seconds)
        for raw in ('-1s','NaN','Inf','1e3s','1m trailing','10','s','1s'*100,None):
            self.assertIsNone(duration_seconds(raw),raw)

    def test_explicit_hints_override_bucket_estimates_and_limit_axis_is_respected(self):
        headers={'Retry-After':'2','retry-after-ms':'3500','x-ratelimit-reset-tokens':'1m',
                 'x-ratelimit-remaining-tokens':'0','x-ratelimit-reset-requests':'2h',
                 'x-ratelimit-remaining-requests':'5'}
        self.assertEqual(server_retry_delay(headers,0,rate_limited=True),3.5)
        self.assertEqual(server_retry_delay(headers,0,rate_limited=False),3.5)
        del headers['Retry-After'];del headers['retry-after-ms']
        self.assertEqual(server_retry_delay(headers,0,rate_limited=True),60)
        self.assertEqual(server_retry_delay(headers,0,rate_limited=True,axis='requests'),7200)
        headers['x-ratelimit-remaining-tokens']='500'
        self.assertEqual(server_retry_delay(headers,0,rate_limited=True,axis='tokens'),60)

    def test_long_server_wait_recovers_same_wire_with_configured_total_deadline(self):
        now=[0.0];sent=[];sleeps=[]
        def response(request):
            sent.append(request.content)
            return (httpx.Response(429,json={'error':{'code':'rate_limit_exceeded','type':'new_api_error'}},
                                   headers={'x-ratelimit-reset-tokens':'1m30s'}) if len(sent)==1 else ok())
        def sleep(delay):sleeps.append(delay);now[0]+=delay
        with httpx.Client(transport=httpx.MockTransport(response)) as http:
            cfg=Config(stream=False,timeout=10,retry_wait_budget=180,retry_total_timeout=200)
            client=Client(cfg,http_client=http,sleeper=sleep,clock=lambda:now[0],randomizer=lambda:0)
            self.assertEqual(client.chat([{'role':'user','content':'task'}],[])['content'],'done')
        self.assertEqual(sleeps,[90])
        self.assertEqual(sent,[sent[0],sent[0]])

    def test_long_hint_beyond_budget_is_deferred_without_an_early_request(self):
        seen=[]
        def response(request):
            seen.append(request)
            return httpx.Response(429,json={'error':{'code':'rate_limit_exceeded'}},headers={'Retry-After':'120'})
        with httpx.Client(transport=httpx.MockTransport(response)) as http:
            client=Client(Config(stream=False,retry_wait_budget=60),http_client=http,
                          sleeper=lambda _:self.fail('should not sleep'),randomizer=lambda:0)
            with self.assertRaises(RateLimitError) as caught:client.chat([],[])
        self.assertEqual(len(seen),1)
        self.assertEqual(caught.exception.retry_after_seconds,120)

    def test_missing_headers_use_slower_rate_backoff_covering_minute_window(self):
        sent=[];sleeps=[];now=[0.0]
        def response(request):
            sent.append(request)
            return httpx.Response(429,json={'error':{'type':'rate_limit_error'}})
        def sleep(delay):sleeps.append(delay);now[0]+=delay
        with httpx.Client(transport=httpx.MockTransport(response)) as http:
            client=Client(Config(stream=False,retry_max_attempts=6,retry_wait_budget=180,retry_total_timeout=300),
                          http_client=http,sleeper=sleep,clock=lambda:now[0],randomizer=lambda:0)
            with self.assertRaises(RateLimitError):client.chat([],[])
        self.assertEqual(len(sent),6)
        self.assertEqual(sleeps,[2.5,5,10,20,30])
        self.assertGreater(sum(sleeps),60)

    def test_server_minimum_gets_additional_jitter_never_a_shorter_delay(self):
        seen=[];sleeps=[]
        def response(request):
            seen.append(request)
            return httpx.Response(429,json={'error':{'type':'rate_limit_error'}},headers={'Retry-After':'10'}) if len(seen)==1 else ok()
        with httpx.Client(transport=httpx.MockTransport(response)) as http:
            client=Client(Config(stream=False),http_client=http,sleeper=sleeps.append,randomizer=lambda:1)
            client.chat([],[])
        self.assertGreater(sleeps[0],10)

    def test_quota_and_html_never_retry_even_with_valid_server_hint(self):
        for response in (httpx.Response(429,json={'error':{'type':'insufficient_quota'}},headers={'Retry-After':'1'}),
                         httpx.Response(429,text='<html>rate limit</html>',headers={'Retry-After':'1','content-type':'text/html'})):
            seen=[]
            def respond(request):seen.append(request);return response
            with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                client=Client(Config(stream=False),http_client=http,sleeper=lambda _:self.fail('retry'))
                with self.assertRaises(ClientError):client.chat([],[])
            self.assertEqual(len(seen),1)

    def test_accepted_json_or_sse_rate_error_stops_without_retry_in_both_protocols(self):
        for wire in ('responses','chat_completions'):
            for stream in (False,True):
                seen=[]
                error={'code':'rate_limit_exceeded','message':'provider busy'}
                def respond(request):
                    seen.append(request)
                    value=({'type':'error',**error} if wire=='responses' else {'error':error})
                    body='data: '+json.dumps(value)+'\n\n'
                    if stream and wire=='responses':
                        # An accepted stream that already emitted output must never
                        # be replayed; only an output-free empty stream may retry.
                        body=('data: '+json.dumps({'type':'response.output_text.delta','delta':'partial'})+'\n\n')+body
                    if stream:return httpx.Response(200,text=body,headers={'content-type':'text/event-stream'})
                    return httpx.Response(200,json={'error':error})
                with httpx.Client(transport=httpx.MockTransport(respond)) as http:
                    client=Client(Config(wire_api=wire,stream=stream),http_client=http,
                                  sleeper=lambda _:self.fail('accepted response replayed'))
                    a=Agent(client.config,client=client)
                    with patch('termux_agent.agent.execute',side_effect=AssertionError('tool executed')):
                        a.run('task')
                    self.assertEqual(a.last_run['stop_reason'],'rate_limit')
                self.assertEqual(len(seen),1)

    def test_silent_eof_never_executes_even_syntactically_complete_tool_arguments(self):
        call={'index':0,'id':'call','type':'function','function':{'name':'write_file','arguments':'{"path":"x","content":"x"}'}}
        data='data: '+json.dumps({'choices':[{'delta':{'tool_calls':[call]}}]})+'\n\n'
        seen=[]
        def response(request):seen.append(request);return httpx.Response(200,text=data,headers={'content-type':'text/event-stream'})
        with httpx.Client(transport=httpx.MockTransport(response)) as http:
            client=Client(Config(approval_policy='never'),http_client=http)
            a=Agent(client.config,client=client)
            with patch('termux_agent.agent.execute',side_effect=AssertionError('incomplete tool ran')):a.run('task')
            self.assertEqual(a.last_run['status'],'incomplete')
            self.assertEqual(a.last_run['stop_reason'],'stream_interrupted')
            self.assertIn('_incomplete_tool_calls',a.incomplete_draft)
            self.assertEqual([m['role'] for m in a.messages],['system','user'])
        self.assertEqual(len(seen),1)

    def test_total_deadline_is_profile_configurable_and_validated(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'config.toml'
            p.write_text('retry_total_timeout=200\n[apis.cloud]\nbase_url="https://example.test/v1"\nmodel="x"\nretry_total_timeout=480\n')
            self.assertEqual(Config.load(p,'default').retry_total_timeout,200)
            self.assertEqual(Config.load(p,'cloud').retry_total_timeout,480)
        for value in (-1,3601,10**1000,True,float('nan'),float('inf')):
            with self.assertRaises(ValueError):Config(retry_total_timeout=value).validate()

    def test_resume_waits_only_remaining_cooldown_without_restarting_backoff(self):
        now=[0.];sleeps=[]
        def sleep(delay):sleeps.append(delay);now[0]+=delay
        with httpx.Client(transport=httpx.MockTransport(lambda _:ok())) as http:
            client=Client(Config(stream=False,timeout=1),http_client=http,sleeper=sleep,
                          clock=lambda:now[0],randomizer=lambda:0)
            client.set_retry_hooks(initial_cooldown=lambda:.1)
            self.assertEqual(client.chat([],[])['content'],'done')
        self.assertEqual(sleeps,[.1])
