import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

from gitd.routers import explorer


class ExplorerSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "app_explorer"
        self.root.mkdir()
        self.previous_root = explorer._EXPLORER_DIR
        explorer._EXPLORER_DIR = self.root

    def tearDown(self):
        explorer._EXPLORER_DIR = self.previous_root
        explorer._active_proc = {}
        self.temp.cleanup()

    def assert_invalid(self, value, kind="run", **kwargs):
        with self.assertRaises(HTTPException) as raised:
            explorer._resolve_explorer_path(value, kind=kind, **kwargs)
        self.assertEqual(raised.exception.status_code, 400)

    def test_explorer_identifiers_reject_traversal_absolute_separators_and_dotdot(self):
        invalid = ("", ".", "..", "../frontend", "../../frontend", "/tmp/frontend", r"C:\frontend", "foo/bar", "foo\\bar", "bad\x00name")
        for value in invalid:
            with self.subTest(value=value):
                self.assert_invalid(value)
                self.assert_invalid(value, kind="package")
                self.assert_invalid(value, kind="state")

    def test_explorer_resolver_rejects_symlink_escape(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = self.root / "com.example.escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable on this host")
        self.assert_invalid("com.example.escape")
        self.assertTrue(outside.exists())

    def test_valid_explorer_routes_read_serve_and_delete_only_own_run(self):
        run = self.root / "com.example.valid"
        screenshots = run / "screenshots"
        screenshots.mkdir(parents=True)
        state_id = "0123456789ab"
        (run / "state_graph.json").write_text(json.dumps({"package": "com.example.valid", "states": {state_id: {}}}))
        screenshot = screenshots / f"{state_id}.png"
        screenshot.write_bytes(b"png")
        outside = Path(self.temp.name) / "outside-sentinel.txt"
        outside.write_text("keep")

        self.assertEqual(explorer.explorer_run_detail("com.example.valid")["package"], "com.example.valid")
        served = explorer.explorer_screenshot("com.example.valid", state_id)
        self.assertEqual(Path(served.path), screenshot)
        self.assertEqual(explorer.explorer_delete("com.example.valid"), {"ok": True})
        self.assertFalse(run.exists())
        self.assertEqual(outside.read_text(), "keep")

    def test_start_valid_package_uses_contained_output_and_rejects_escape_before_spawn(self):
        fake_proc = Mock(pid=1234)
        previous_proc = explorer._active_proc
        try:
            with patch.object(explorer._subprocess, "Popen", return_value=fake_proc) as popen:
                with patch("builtins.open", return_value=Mock()):
                    result = explorer.explorer_start({"package": "com.example.valid", "device": "emulator-5554"})
            self.assertEqual(result["output_dir"], str(self.root / "com.example.valid"))
            popen.assert_called_once()

            with patch.object(explorer._subprocess, "Popen") as popen:
                with self.assertRaises(HTTPException) as raised:
                    explorer.explorer_start({"package": "../../frontend", "device": "emulator-5554"})
            self.assertEqual(raised.exception.status_code, 400)
            popen.assert_not_called()
        finally:
            explorer._active_proc = previous_proc


if __name__ == "__main__":
    unittest.main()
