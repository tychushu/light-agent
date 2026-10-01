"""Prompt files reload for new sessions; saved session bases remain stable."""
import copy
from importlib.resources import files
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from termux_agent.agent import Agent
from termux_agent.config import Config
from termux_agent.instructions import load_system_prompt, system_prompt_path
from termux_agent.sessions import SessionStore, new_id, restore, snapshot


class NullClient:
    def __init__(self, *args, **kwargs): pass
    def close(self): pass


def build_agent(config, instructions, base_system=None):
    return Agent(config, client=NullClient(), instructions=instructions, base_system=base_system)


class SystemPromptTests(unittest.TestCase):
    def test_first_use_copies_template_and_new_agents_reload_edits(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'HOME': td}):
            template = files('termux_agent').joinpath('system_prompt.txt').read_text().rstrip('\r\n')
            first = Agent(Config(), client=NullClient())
            self.assertEqual(first.system, template)
            path = system_prompt_path()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text('手工编辑的系统 Prompt\n')
            second = Agent(Config(), client=NullClient())
            self.assertEqual(second.system, '手工编辑的系统 Prompt')
            self.assertEqual(first.system, template)
            self.assertEqual(load_system_prompt(), second.system)

    def test_empty_or_oversized_prompt_has_no_hardcoded_fallback(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'HOME': td}):
            load_system_prompt()
            path = system_prompt_path()
            for value in ('\n', 'x' * 16001):
                path.write_text(value)
                with self.assertRaises(ValueError): Agent(Config(), client=NullClient())

    def test_old_session_and_skills_restore_saved_base_even_if_file_is_invalid(self):
        from termux_agent import cli
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'HOME': td}):
            root = Path(td)
            config_path = root/'config.toml'
            config_path.write_text('base_url="https://example.com/v1"\nmodel="m"\n')
            old = Agent(Config.load(config_path), client=NullClient(), instructions='scope instructions')
            old.load_skill('demo', 'skill instructions')
            scope = {'directory': td, 'config_path': str(config_path), 'instructions': 'scope instructions',
                     'session_file': None, 'disabled': False, 'instruction_path': None}
            payload = snapshot(old, scope)
            system_prompt_path().write_text('')
            for version in (2, 3):
                saved = copy.deepcopy(payload)
                saved['version'] = version
                if version == 2: saved.pop('base_system')
                _, resumed, _ = restore(saved, build_agent)
                self.assertEqual(resumed.system, old.system)
                resumed.clear_skills()
                self.assertEqual(resumed.system, old.base_system)
            store = SessionStore(); identifier = new_id()
            store.save(identifier, 0, payload); store.close()
            previous = Path.cwd()
            try:
                with patch.object(cli.sys, 'argv', ['ta', '--session', identifier]), \
                     patch('termux_agent.agent.Client', NullClient), \
                     patch('builtins.input', side_effect=['/exit']), \
                     patch('sys.stdout', new_callable=io.StringIO):
                    cli.main()
            finally:
                os.chdir(previous)
