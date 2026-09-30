import tempfile
import os
import io
import unittest
from pathlib import Path

from termux_agent.agent import Agent, SYSTEM
from termux_agent.config import Config
from termux_agent.sessions import snapshot, restore
from termux_agent.skills import SkillRegistry, MAX_SKILL_CHARS


class NullClient:
    def close(self):
        pass


class SkillTests(unittest.TestCase):
    def test_zero_load_and_manual_session_lifecycle(self):
        agent = Agent(Config(), client=NullClient())
        self.assertEqual(agent.get_active_skills(), {})
        self.assertEqual(agent.system, SYSTEM)
        agent.load_skill('demo', 'Do the demo.')
        self.assertIn('<!-- SKILL_START: demo -->', agent.system)
        agent.unload_skill('demo')
        self.assertNotIn('Do the demo.', agent.system)
        agent.load_skill('one', 'One instructions')
        agent.load_skill('two', 'Two instructions')
        agent.clear_skills()
        self.assertEqual(agent.system, SYSTEM)

    def test_frontmatter_tolerance_scope_and_precedence(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); hermes=root/'hermes'; local=root/'project'/'skills'; cfg=root/'config'
            for folder in (hermes,local,cfg): folder.mkdir(parents=True)
            (hermes/'hello').mkdir(); (hermes/'hello'/'SKILL.md').write_text('---\nname: hello\ndescription: "say \\"hi\\""\n---\nHermes body')
            (local/'hello').mkdir(parents=True); (local/'hello'/'SKILL.md').write_text('---\nname: hello\ndescription: project\n---\nLocal body')
            (cfg/'fallback').mkdir(); (cfg/'fallback'/'SKILL.md').write_text('---\nname: [malformed\n---\nBody')
            registry=SkillRegistry([hermes,local,cfg])
            self.assertEqual([x.name for x in registry.list_skills()], ['hello','fallback'])
            skill,body=registry.load('hello')
            self.assertIn('Hermes body',body)
            self.assertEqual(skill.description,'say "hi"')
            self.assertEqual(registry.load('fallback')[1],'---\nname: [malformed\n---\nBody')
            with self.assertRaises(ValueError): registry.find('../hello')

    def test_max_size_and_session_roundtrip_preserve_activation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'skill').mkdir(); file=root/'skill'/'SKILL.md'
            file.write_text('---\nname: skill\n---\n'+('x'*MAX_SKILL_CHARS))
            with self.assertRaises(ValueError): SkillRegistry([root]).load('skill')
            config_file=root/'config.toml';config_file.write_text('base_url="https://example.com/v1"\nmodel="m"\n')
            agent=Agent(Config.load(config_file),client=NullClient())
            agent.load_skill('manual','remember')
            scope={'directory':td,'config_path':str(config_file),'instructions':'','session_file':None,'disabled':False,'instruction_path':None}
            payload=snapshot(agent,scope)
            _,restored,_=restore(payload,lambda cfg,text: Agent(cfg,client=NullClient(),instructions=text))
            self.assertEqual(restored.get_active_skills(),{'manual':'remember'})
            restored.unload_skill('manual')
            self.assertEqual(restored.system,agent.base_system)

    def test_cli_manual_skill_commands_inject_and_strip_without_model_call(self):
        from unittest.mock import patch
        from termux_agent import cli
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'skills'/'demo';root.mkdir(parents=True)
            (root/'SKILL.md').write_text('---\nname: demo\ndescription: test skill\n---\nSKILL_SENTINEL')
            registry=SkillRegistry([Path(td)/'skills'])
            rendered=""
            original=Path.cwd()
            prompts=['/skill list','/skill info demo','/skill load demo','/debug-context',
                     '/skill unload demo','/debug-context','/exit']
            try:
                with patch.object(cli.sys,'argv',['ta','--no-session','--no-agent','--directory',td]), \
                     patch.object(cli,'Config') as config_class, patch.object(cli,'SkillRegistry',return_value=registry), \
                     patch('builtins.input',side_effect=prompts), patch('sys.stdout',new_callable=io.StringIO) as output:
                    config_class.load.return_value=Config(stream=False)
                    cli.main()
                    rendered=output.getvalue()
            finally:
                os.chdir(original)
            self.assertIn('test skill',rendered)
            self.assertIn('SKILL_SENTINEL',rendered)
            self.assertIn('Loaded demo',rendered)
            self.assertIn('Unloaded demo',rendered)
            self.assertNotIn('active skills',rendered.lower())
