import ast
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer, QCoreApplication, QEvent
from desktop_services import DesktopServices, DesktopLauncher, AsyncTasks, desktop_info, localized_name
from localization import _, set_language, system_language

APP = QApplication.instance() or QApplication([])


def wait_until(condition, timeout=2):
    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.002)
    if not condition():
        raise AssertionError("Timed out waiting for asynchronous callback")


class DesktopTests(unittest.TestCase):
    def setUp(self):
        set_language("en")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = pathlib.Path(self.temp.name)

    def entry(self, name="selected.desktop", **extra):
        path = self.folder / name
        fields = {"Type": "Application", "Name": "English name", "Name[zh_CN]": "中文名称", "Exec": "/bin/true", **extra}
        path.write_text("[Desktop Entry]\n" + "\n".join(f"{key}={value}" for key, value in fields.items()) + "\n")
        return path

    def test_names_follow_user_language(self):
        info = desktop_info(str(self.entry()))
        self.assertEqual(localized_name(info), "English name")
        set_language("zh_CN")
        self.assertEqual(localized_name(info), "中文名称")

    def test_prepare_uses_exact_selected_file_not_filename_fallback(self):
        path = self.entry("selected with spaces.desktop")
        info, uri = DesktopServices.prepare_target(str(path))
        self.assertEqual(info.get_filename(), str(path))
        self.assertIsNone(uri)

    def test_missing_path_and_mac_app_are_rejected(self):
        for path in (self.folder / "missing", self.folder / "Example.app"):
            if path.suffix == ".app":
                path.mkdir()
            with self.subTest(path=path), self.assertRaises(ValueError):
                DesktopServices.prepare_target(str(path))

    def test_tryexec_missing_is_not_launchable(self):
        path = self.entry(TryExec="/does/not/exist")
        with self.assertRaises(ValueError):
            DesktopServices.prepare_target(str(path))

    def test_absolute_icon_is_resolved_and_file_metadata_has_icon(self):
        icon = self.folder / "icon.svg"
        icon.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
        path = self.entry(Icon=str(icon))
        name, exists, specification = DesktopServices.describe(str(path))
        self.assertTrue(exists)
        self.assertEqual(specification, ("file", str(icon)))
        self.assertEqual(name, "English name")

    def test_file_uris_escape_spaces_unicode_and_shell_characters(self):
        path = self.folder / "中文 name;$(noop).txt"
        path.write_text("test")
        info, uri = DesktopServices.prepare_target(str(path))
        self.assertIsNone(info)
        self.assertEqual(uri, path.as_uri())

    def test_timeout_keeps_event_loop_responsive_and_ignores_late_result(self):
        tasks, results, ticks = AsyncTasks(), [], []
        tasks.run(lambda: (time.sleep(0.1), "late")[1], lambda result, error: results.append((result, error)), timeout_ms=10)
        QTimer.singleShot(5, lambda: ticks.append(True))
        wait_until(lambda: bool(results))
        self.assertTrue(ticks)
        self.assertIsInstance(results[0][1], TimeoutError)
        wait_until(lambda: not tasks._tasks)
        self.assertEqual(len(results), 1)

    def test_launch_failure_is_reported_without_retrying_a_different_entry(self):
        launcher = DesktopLauncher()
        outcomes = []
        class Info:
            def launch_uris_async(self, uris, context, cancellable, callback, data):
                self.arguments = uris
                callback(self, object(), data)
            def launch_uris_finish(self, result):
                return False
            def get_filename(self):
                return "/demo/selected.desktop"
        info = Info()
        launcher._launch((info, None), outcomes.append)
        self.assertEqual(info.arguments, [])
        self.assertIsInstance(outcomes[0], ValueError)

    def test_destroyed_receiver_does_not_receive_a_late_worker_callback(self):
        tasks, results = AsyncTasks(), []
        tasks.run(lambda: (time.sleep(0.05), "late")[1], lambda *args: results.append(args))
        pending = list(tasks._tasks)
        tasks.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        wait_until(lambda: all(future.done() for future in pending))
        APP.processEvents()
        self.assertFalse(results)
        replacement = AsyncTasks()
        replacement.run(lambda: "new", lambda result, error: results.append(result))
        wait_until(lambda: bool(results))
        self.assertEqual(results, ["new"])

    def test_english_catalog_covers_all_application_messages(self):
        for path in (ROOT / "src").glob("*.py"):
            if path.name.startswith("._"):
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "Notice", "ConfigError") and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    message = node.args[0].value
                    with self.subTest(file=path.name, message=message):
                        self.assertNotEqual(_(message), message)

    def test_c_locale_uses_english(self):
        with patch.dict(os.environ, {"LC_ALL": "C.UTF-8"}):
            self.assertEqual(system_language(), "en")


if __name__ == "__main__":
    unittest.main()
