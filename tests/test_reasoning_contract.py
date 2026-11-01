"""Reasoning replay is required state, not an unconditional token-saving target."""
import copy
import json
import unittest

import httpx

from termux_agent.client import Client
from termux_agent.config import Config


def thought(index, size=8414):
    return {'type':'reasoning','id':f'rs_{index}','summary':[],
            'encrypted_content':str(index)*size}


def call(index):
    return {'type':'function_call','id':f'fc_{index}','call_id':f'call_{index}',
            'name':'read_file','arguments':json.dumps({'path':f'file-{index}.txt'})}


def no_server_ids(items):
    return [{k:v for k,v in item.items() if k!='id'} for item in items]


class ReasoningContractTests(unittest.TestCase):
    def test_active_chain_preserves_every_reasoning_item_and_call_id_without_mutation(self):
        first=[thought(i) for i in range(4)]+[call(0)]
        second=[thought(4),call(1)]
        messages=[{'role':'system','content':'unchanged'}, {'role':'user','content':'inspect both'},
                  {'role':'assistant','content':'','reasoning_content':'display-only text',
                   '_responses_output':first},
                  {'role':'tool','tool_call_id':'call_0','content':'first result'},
                  {'role':'assistant','content':'','_responses_output':second},
                  {'role':'tool','tool_call_id':'call_1','content':'second result'}]
        original=copy.deepcopy(messages)
        with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200))) as http:
            client=Client(Config(wire_api='responses'),http_client=http)
            _,body=client._prepare_responses_request(messages,[])
        self.assertEqual(body['input'][2:7],no_server_ids(first))
        self.assertEqual(body['input'][8:10],no_server_ids(second))
        self.assertEqual([i['call_id'] for i in body['input'] if i['type']=='function_call'],['call_0','call_1'])
        self.assertEqual(len([i for i in body['input'] if i['type']=='reasoning']),5)
        self.assertNotIn('display-only text',json.dumps(body))
        self.assertFalse(body['store'])
        self.assertIn('reasoning.encrypted_content',body['include'])
        self.assertEqual(messages,original)

    def test_sse_item_events_and_terminal_output_do_not_duplicate_reasoning(self):
        output=[thought(i) for i in range(4)]+[call(0)]
        events=[]
        for index,item in enumerate(output):
            for kind in ('response.output_item.added','response.output_item.done'):
                events.append({'type':kind,'output_index':index,'item':item})
        events.append({'type':'response.completed','response':{'status':'completed','output':output}})
        stream=''.join('data: '+json.dumps(event)+'\n\n' for event in events)
        with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,text=stream,
                         headers={'content-type':'text/event-stream'}))) as http:
            client=Client(Config(wire_api='responses'),http_client=http)
            answer=client.chat([{'role':'user','content':'inspect'}],[])
            self.assertEqual(answer['_responses_output'],output)
            _,body=client._prepare_responses_request([
                {'role':'user','content':'inspect'},answer,
                {'role':'tool','tool_call_id':'call_0','content':'result'}],[])
        self.assertEqual(len([item for item in body['input'] if item.get('type')=='reasoning']),4)
        self.assertEqual(sum(len(item['encrypted_content']) for item in body['input']
                             if item.get('type')=='reasoning'),33656)

    def test_legitimate_consecutive_user_messages_do_not_need_destructive_normalization(self):
        messages=[{'role':'system','content':'rules'}, {'role':'user','content':'original goal'},
                  {'role':'user','content':'additional constraint; preserve exactly'}]
        before=copy.deepcopy(messages)
        with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200))) as http:
            _,body=Client(Config(wire_api='responses'),http_client=http)._prepare_responses_request(messages,[])
        self.assertEqual([i['content'][0]['text'] for i in body['input'][1:]],
                         ['original goal','additional constraint; preserve exactly'])
        self.assertEqual(messages,before)
