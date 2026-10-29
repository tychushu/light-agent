"""Regression tests for the bounded per-tool output character cap."""

import tempfile
import unittest
from pathlib import Path

from termux_agent.config import Config
from termux_agent.tools import execute


class OutputLimitConfigTests(unittest.TestCase):
    def load_config(self, assignment="") -> Config:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                'default_api = "local"\n'
                f"{assignment}"
                '[apis.local]\n'
                'base_url = "http://127.0.0.1:1/v1"\n'
                'model = "local-test"\n',
                encoding="utf-8",
            )
            return Config.load(path)

    def test_toml_accepts_25000_with_inline_local_profile(self):
        config = self.load_config("max_output_chars = 25000\n")
        self.assertEqual(config.api_profile, "local")
        self.assertEqual(config.max_output_chars, 25000)

    def test_default_and_inclusive_integer_boundaries(self):
        self.assertEqual(self.load_config().max_output_chars, 20000)
        for value in (128, 25000, 1_000_000):
            with self.subTest(value=value):
                self.assertEqual(
                    self.load_config(f"max_output_chars = {value}\n").max_output_chars,
                    value,
                )

    def test_rejects_bool_non_integer_and_out_of_range_values(self):
        for assignment in (
            "max_output_chars = true\n",
            "max_output_chars = false\n",
            'max_output_chars = "25000"\n',
            "max_output_chars = 25000.0\n",
            "max_output_chars = 127\n",
            "max_output_chars = 1000001\n",
        ):
            with self.subTest(assignment=assignment), self.assertRaises(ValueError):
                self.load_config(assignment)


class OutputLimitToolTests(unittest.TestCase):
    def test_read_file_uses_25000_character_output_cap(self):
        config = Config(max_output_chars=25000)
        config.validate()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            short_path = root / "short.txt"
            short = "a" * 21_000
            short_path.write_text(short, encoding="utf-8")
            short_result = execute(
                "read_file",
                {"path": str(short_path), "offset": 0, "limit": len(short)},
                config.max_output_chars,
            )
            self.assertFalse(short_result["truncated"])
            self.assertEqual(short_result["content"], short)

            long_path = root / "long.txt"
            long = "b" * 25_001
            long_path.write_text(long, encoding="utf-8")
            long_result = execute(
                "read_file",
                {"path": str(long_path), "offset": 0, "limit": len(long)},
                config.max_output_chars,
            )
            self.assertTrue(long_result["truncated"])
            self.assertIn("[TRUNCATED]", long_result["content"])
            self.assertLessEqual(len(long_result["content"]), config.max_output_chars)


if __name__ == "__main__":
    unittest.main()
