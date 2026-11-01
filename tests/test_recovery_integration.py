"""Independent checks at CLI/persistence boundaries, beyond reducer unit tests."""
import json
import re
import unittest
from unittest.mock import patch

import test_mimo_acceptance as fixtures
import test_session_selection as selection
from termux_agent.agent import Agent


class RecoveryIntegrationTests(fixtures.AcceptanceTestCase):
    def test_fresh_session_first_automatic_archive_uses_actual_persisted_id(self):
        source=self.root/'source.txt';source.write_text('source-body '*1200)
        config=self.root/'config.toml'
        config.write_text('model="test-model"\nbase_url="https://example.test/v1"\nstream=false\n'
                          'approval_policy="never"\ntool_history_keep_groups=2\n')
        replies=[{'tool_calls':[fixtures.call(f'fresh{i}','read_file',{'path':str(source)})]}
                 for i in range(3)]
        replies.extend([{'content':'first done'},{'content':'second done'}])
        output,requests=self.run_cli(['--config',str(config),'--directory',str(self.root),'--no-agent'],
                                    ['inspect sources','review the findings','/exit'],replies,store=self.root/'store')
        self.assertIn('second done',output)
        self.assertNotIn('memory-',output)
        self.assertEqual(len(requests.read_text().splitlines()),5)
        with fixtures.open_store(self.root/'store') as store:
            rows=[store.load(identifier)[1] for identifier in store.ids()]
            self.assertEqual(len(rows),2)
            archived=[p for p in rows if sum(m['role']=='tool' for m in p['messages'])==3]
            reduced=[p for p in rows if sum(m['role']=='tool' for m in p['messages'])==2]
            self.assertEqual(len(archived),1)
            self.assertEqual(len(reduced),1)
            archive_id=reduced[0]['branch']['archive_session_id']
            self.assertRegex(archive_id,r'^[0-9a-f]{12}$')
            self.assertIn(archive_id,store.ids())
            self.assertIn({'role':'user','content':'inspect sources'},reduced[0]['messages'])
            self.assertIn({'role':'user','content':'review the findings'},reduced[0]['messages'])


class UIInterruptionTests(unittest.TestCase):
    setUp=selection.SessionSelectionTests.setUp
    tearDown=selection.SessionSelectionTests.tearDown
    run_cli=selection.SessionSelectionTests.run_cli

    def test_continue_ctrl_c_returns_to_repl_and_reports_failed_cleanup_checkpoint(self):
        error=KeyboardInterrupt();error.add_note('Interruption checkpoint failed: OSError')
        with patch.object(Agent,'continue_task',side_effect=error):
            output,prompts,_,_=self.run_cli(['/continue','/help','/exit'])
        self.assertEqual(len(prompts),3)
        self.assertIn('中断检查点保存失败（OSError）',output)
        self.assertIn('/session delete',output)
