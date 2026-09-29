"""Regression tests for named API profiles and switching behavior."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from termux_agent.config import Config
from termux_agent.api_profiles import ProfileStore


CONFIG = '''default_api = "local"

[apis.local]
base_url = "http://127.0.0.1:8080/v1"
model = "local-model"
api_key_env = "LOCAL_PROFILE_KEY"

[apis.cloud]
base_url = "https://cloud.example/v1"
model = "cloud-model"
api_key_env = "CLOUD_PROFILE_KEY"
'''


class ApiProfileConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(CONFIG, encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_loads_builtin_and_file_profiles_without_sharing_credentials(self):
        store = ProfileStore(self.config_path)
        store.add("cloud-alt", "https://alt.example/v1", "alt-model",
                  api_key_env="ALT_PROFILE_KEY", kind="cloud")

        env = {
            "LOCAL_PROFILE_KEY": "local-secret",
            "CLOUD_PROFILE_KEY": "cloud-secret",
            "ALT_PROFILE_KEY": "alt-secret",
        }
        with patch.dict(os.environ, env, clear=True):
            local = Config.load(self.config_path, "local")
            cloud = Config.load(self.config_path, "cloud")
            alt = Config.load(self.config_path, "cloud-alt")

        self.assertEqual((local.base_url, local.model, local.api_key),
                         ("http://127.0.0.1:8080/v1", "local-model", "local-secret"))
        self.assertEqual((cloud.base_url, cloud.model, cloud.api_key),
                         ("https://cloud.example/v1", "cloud-model", "cloud-secret"))
        self.assertEqual((alt.base_url, alt.model, alt.api_key),
                         ("https://alt.example/v1", "alt-model", "alt-secret"))

        # A profile without its own key source must never inherit another key.
        store.add("public", "https://public.example/v1", "public-model")
        with patch.dict(os.environ, env, clear=True):
            public = Config.load(self.config_path, "public")
        self.assertEqual(public.api_key, "")

    def test_profiles_enumerates_inline_and_file_profiles(self):
        store = ProfileStore(self.config_path)
        store.add("local-fast", "http://127.0.0.1:8081/v1", "fast-model")

        names = Config.profiles(self.config_path)
        self.assertIn("local", names)
        self.assertIn("cloud", names)
        self.assertIn("local-fast", names)
        self.assertEqual(len(names), len(set(names)))

    def test_add_persists_settings_without_persisting_secret_material(self):
        store = ProfileStore(self.config_path)
        store.add("gateway", "https://gateway.example/v1", "reasoning-model",
                  api_key_env="GATEWAY_TOKEN", kind="cloud", stream=False,
                  timeout=45, user_agent="termux-agent-test/1")

        profile_file = self.root / "profiles" / "gateway.toml"
        self.assertTrue(profile_file.is_file())
        serialized = profile_file.read_text(encoding="utf-8")
        self.assertIn("GATEWAY_TOKEN", serialized)
        self.assertNotIn("secret-value", serialized)
        with patch.dict(os.environ, {"GATEWAY_TOKEN": "secret-value"}, clear=True):
            config = Config.load(self.config_path, "gateway")
        self.assertEqual(config.model, "reasoning-model")
        self.assertFalse(config.stream)
        self.assertEqual(config.timeout, 45)
        self.assertEqual(config.user_agent, "termux-agent-test/1")
        self.assertEqual(config.api_key, "secret-value")

    def test_duplicate_names_are_rejected_without_overwriting_either_source(self):
        store = ProfileStore(self.config_path)
        with self.assertRaises((ValueError, FileExistsError)):
            store.add("cloud", "https://attacker.example/v1", "replacement")
        self.assertEqual(Config.load(self.config_path, "cloud").model, "cloud-model")

        store.add("custom", "https://custom.example/v1", "original")
        with self.assertRaises((ValueError, FileExistsError)):
            store.add("custom", "https://changed.example/v1", "replacement")
        self.assertEqual(Config.load(self.config_path, "custom").model, "original")

    def test_profile_names_are_safe_single_path_components(self):
        store = ProfileStore(self.config_path)
        for name in ("", ".", "..", "../escape", "a/b", "a\\b", "has space", "--option"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    store.add(name, "https://safe.example/v1", "model")
        self.assertFalse((self.root.parent / "escape.toml").exists())

    def test_remove_and_default_protect_inline_profiles_and_persist_custom_default(self):
        store = ProfileStore(self.config_path)
        store.add("temporary", "https://temporary.example/v1", "temp-model")
        store.set_default("temporary")
        self.assertEqual(store.get_default(), "temporary")

        # Reopening the store models a later CLI invocation.
        reopened = ProfileStore(self.config_path)
        self.assertEqual(reopened.get_default(), "temporary")
        with self.assertRaises(ValueError):
            reopened.remove("local")
        with self.assertRaises(ValueError):
            reopened.remove("cloud")

        reopened.remove("temporary")
        self.assertNotIn("temporary", [row["name"] for row in reopened.list()])
        self.assertNotIn("temporary", Config.profiles(self.config_path))
        self.assertTrue((self.root / "profiles" / "temporary.toml").exists() is False)

    def test_legacy_top_level_configuration_remains_loadable(self):
        legacy = self.root / "legacy.toml"
        legacy.write_text('''base_url = "http://127.0.0.1:8085/v1"
model = "legacy-model"
api_key_env = "LEGACY_KEY"
''', encoding="utf-8")
        with patch.dict(os.environ, {"LEGACY_KEY": "legacy-secret"}, clear=True):
            config = Config.load(legacy)
        self.assertEqual(config.model, "legacy-model")
        self.assertEqual(config.api_key, "legacy-secret")
        self.assertIn("default", Config.profiles(legacy))


class ApiProfileCliTests(unittest.TestCase):
    def test_default_selection_and_next_cycle_switch_agents_cleanly(self):
        from termux_agent import cli

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "config.toml"
            config_path.write_text(CONFIG, encoding="utf-8")
            ProfileStore(config_path).add("local-fast", "http://127.0.0.1:8081/v1", "fast-model")
            agents = []

            class FakeAgent:
                def __init__(self, config, **kwargs):
                    self.config = config
                    self.prompts = []
                    self.client = type("Client", (), {"close": lambda self: None})()
                    agents.append(self)

                def run(self, text):
                    self.prompts.append(text)
                    return "ok"

            original_cwd = Path.cwd()
            try:
                inputs = ["/api default cloud", "/api next", "after-next", "/exit"]
                with patch.object(cli, "Agent", FakeAgent), \
                     patch.object(cli.sys, "argv", ["ta", "--no-session", "--config", str(config_path), "--directory", td]), \
                     patch("builtins.input", side_effect=inputs), patch("builtins.print"):
                    cli.main()
            finally:
                os.chdir(original_cwd)

            self.assertEqual(ProfileStore(config_path).get_default(), "cloud")
            self.assertEqual(agents[-1].config.api_profile, "local")
            self.assertEqual(agents[-1].prompts, ["after-next"])
            self.assertTrue(all(agent.config.api_profile != "default" for agent in agents))


    def test_cli_add_multiple_profiles_and_numbered_switching(self):
        from termux_agent import cli

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "config.toml"
            config_path.write_text(CONFIG, encoding="utf-8")
            agents = []

            class FakeAgent:
                def __init__(self, config, **kwargs):
                    self.config = config
                    self.prompts = []
                    self.client = type("Client", (), {"close": lambda self: None})()
                    agents.append(self)

                def run(self, text):
                    self.prompts.append(text)
                    return "ok"

            original_cwd = Path.cwd()
            try:
                inputs = [
                    "/api add local-fast http://127.0.0.1:8081/v1 fast-model FAST_KEY local",
                    "/api add cloud-alt https://alt.example/v1 alt-model ALT_KEY cloud --no-stream",
                    "/api 2",  # Local profiles sort before cloud profiles.
                    "after-local-fast", "/api cloud-alt", "after-cloud-alt", "/exit",
                ]
                with patch.object(cli, "Agent", FakeAgent), \
                     patch.object(cli.sys, "argv", ["ta", "--no-session", "--config", str(config_path), "--directory", td]), \
                     patch("builtins.input", side_effect=inputs), patch("builtins.print"):
                    cli.main()
            finally:
                os.chdir(original_cwd)

            self.assertEqual(agents[-2].config.api_profile, "local-fast")
            self.assertEqual(agents[-2].prompts, ["after-local-fast"])
            self.assertEqual(agents[-1].config.api_profile, "cloud-alt")
            self.assertEqual(agents[-1].prompts, ["after-cloud-alt"])
            self.assertFalse(Config.load(config_path, "cloud-alt").stream)
            self.assertEqual(Config.load(config_path, "cloud-alt").api_key, "")


if __name__ == "__main__":
    unittest.main()
