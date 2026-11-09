"""Independent checks at CLI/persistence boundaries, beyond reducer unit tests."""
import json
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
            [identifier] = store.ids()
            [archive] = store.archives(identifier)
            original = store.load_archive(archive['id'])['payload']['messages']
            reduced = store.load(identifier)[1]['messages']
            self.assertEqual(sum(m['role']=='tool' for m in original), 3)
            self.assertEqual(sum(m['role']=='tool' for m in reduced), 2)
            self.assertTrue(any(archive['id'] in m.get('content', '') for m in reduced))
            self.assertIn({'role':'user','content':'inspect sources'}, reduced)
            self.assertIn({'role':'user','content':'review the findings'}, reduced)


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
