"""Console key input and failed real-API acceptance, using dummy secrets only."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    spec = importlib.util.spec_from_file_location("configure_for_test", SCRIPTS / "configure_and_smoke.py")
    configure = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(configure)
finally:
    sys.path.pop(0)


class ConfigureTests(unittest.TestCase):
    def read(self, text):
        stream = iter(text)
        output = io.StringIO()
        key = configure.read_masked_key(lambda: next(stream, ""), output.write)
        return key, output.getvalue()

    def test_paste_is_masked(self):
        key, output = self.read("sk-dummy-secret\r")
        self.assertEqual(key, "sk-dummy-secret")
        self.assertEqual(output, "*" * len(key) + "\n")
        self.assertNotIn(key, output)

    def test_backspace_and_extended_keys(self):
        key, output = self.read("\babc\b\x00Kd\r")
        self.assertEqual(key, "abd")
        self.assertNotIn("abc", output)

    def test_clear_before_retry(self):
        key, _ = self.read("wrong\x15correct\r")
        self.assertEqual(key, "correct")

    def test_cancel_and_eof(self):
        with self.assertRaises(KeyboardInterrupt):
            self.read("dummy\x03")
        with self.assertRaises(EOFError):
            self.read("dummy")

    def test_failed_acceptance_restores_environment_and_saves_no_secret(self):
        names = ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_THINKING")
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            with patch.dict(os.environ, {name: "prior-dummy" for name in names}):
                with patch.object(sys.stdin, "isatty", return_value=True), \
                     patch("builtins.input", side_effect=["", "", ""]), \
                     patch.object(configure, "prompt_key", return_value="sk-dummy-secret"), \
                     patch.object(configure, "smoke", side_effect=AssertionError("model_error")), \
                     patch.object(configure, "REPORT_PATH", report), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(configure.main(), 1)
                self.assertEqual({name: os.environ[name] for name in names},
                                 {name: "prior-dummy" for name in names})
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["planned_tasks"], 6)
            self.assertNotIn("sk-dummy-secret", report.read_text(encoding="utf-8") + output.getvalue())


if __name__ == "__main__":
    unittest.main()
