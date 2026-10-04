import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from PySide6.QtWidgets import QApplication, QMessageBox, QDialog
from PySide6.QtCore import QTimer
from config_store import ConfigStore, default_config
from volume_service import VolumeService, RemovalResult, VolumeGroup
from localization import set_language
from window import MyPanelWindow, SlotWidget, VolumeBar

APP = QApplication.instance() or QApplication([])


def wait_until(condition):
    end = time.monotonic() + 2
    while not condition() and time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.002)
    if not condition():
        raise AssertionError("Timed out")


class EmptyManager:
    def get_objects(self): return []


class WindowTests(unittest.TestCase):
    def setUp(self):
        set_language("en")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / "myPanel.json"
        self.store = ConfigStore(self.path)
        self.store.load()
        self.cfg = default_config()
        self.cfg["lastOpenedFiles"][0] = "/demo/first"
        self.cfg["lastOpenedFiles"][1] = "/demo/second-column"
        self.cfg["lastOpenedFiles"][196] = "/demo/last-row"
        self.cfg["lastOpenedFiles"][199] = "/demo/last-column"
        self.store.save(self.cfg)
        service = VolumeService(auto_connect=False)
        service.manager = EmptyManager()
        self.window = MyPanelWindow(self.store, self.store.load(), service)
        self.addCleanup(self.window.close)

    def test_layout_count_and_restart_preserve_all_hidden_cells(self):
        self.window.large_toggle.setChecked(True)
        self.window._change_count(41)
        self.assertEqual(len(self.window._slot_widgets), 200)
        self.window._change_count(-49)
        self.assertEqual(len(self.window._slot_widgets), 4)
        self.window.large_toggle.setChecked(False)
        self.assertEqual(list(self.window._slot_widgets), [0])
        result = ConfigStore(self.path).load()
        self.assertEqual(result.config["lastOpenedFiles"], self.cfg["lastOpenedFiles"])
        self.assertEqual(result.config["itemCount"], 1)
        self.window._change_count(49)
        self.window.large_toggle.setChecked(True)
        self.assertEqual(self.window._slot_widgets[199].target_path, "/demo/last-column")

    def test_slot_bounds_disable_increment_and_decrement(self):
        self.window._change_count(-8)
        self.assertFalse(self.window.btn_dec.isEnabled())
        self.window._change_count(49)
        self.assertFalse(self.window.btn_inc.isEnabled())
        self.window._change_count(1)
        self.assertEqual(self.window.item_count, 50)

    def test_switching_to_single_clears_obsolete_grid_stretch(self):
        self.window.large_toggle.setChecked(True)
        self.window.large_toggle.setChecked(False)
        self.assertEqual(self.window.slots_layout.columnStretch(1), 0)
        self.assertEqual(self.window.slots_layout.columnStretch(3), 0)

    def test_cancelled_import_does_not_modify_file_or_panel(self):
        source = pathlib.Path(self.temp.name) / "import.json"
        source.write_text(json.dumps(["/demo/import"]))
        before = self.path.read_bytes()
        with patch("window.QFileDialog.getOpenFileName", return_value=(str(source), "")), patch("window.QMessageBox.question", return_value=QMessageBox.No):
            self.window._on_import()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.window.config, self.cfg)

    def test_accepted_import_backs_up_current_and_preserves_source(self):
        source = pathlib.Path(self.temp.name) / "import.json"
        source.write_text(json.dumps(["/demo/import"]))
        source_before = source.read_bytes()
        before = self.path.read_bytes()
        with patch("window.QFileDialog.getOpenFileName", return_value=(str(source), "")), patch("window.QMessageBox.question", return_value=QMessageBox.Yes), patch("window.QMessageBox.information"):
            self.window._on_import()
        self.assertEqual(self.window._slot_widgets[0].target_path, "/demo/import")
        self.assertEqual(source.read_bytes(), source_before)
        backups = list(self.path.parent.glob("myPanel.json.backup-*"))
        self.assertEqual(backups[0].read_bytes(), before)

    def test_relocation_cancel_preserves_target(self):
        class Cancelled:
            chosen = None
            def exec(self): return QDialog.Rejected
        with patch("window.TargetChooserDialog", return_value=Cancelled()):
            self.window._on_add(0)
        self.assertEqual(self.window._slot_widgets[0].target_path, "/demo/first")

    def test_metadata_failure_does_not_delete_invalid_target(self):
        slot = self.window._slot_widgets[0]
        slot._metadata_loaded(slot.target_path, ("first", False, ("theme", ["text-x-generic"])), None)
        self.assertIn("Relocate", slot.name_label.toolTip())
        self.assertEqual(slot.target_path, "/demo/first")
        self.assertTrue(slot.repair_btn.isEnabled())

    def test_late_metadata_cannot_overwrite_a_changed_slot(self):
        slot = self.window._slot_widgets[0]
        slot.set_target("/demo/new")
        slot._metadata_loaded("/demo/first", ("old name", True, ("theme", [])), None)
        self.assertEqual(slot.target_path, "/demo/new")
        self.assertEqual(slot.name_label.full_text(), "new")
        slot.set_target("/demo/first")

    def test_eject_all_continues_after_failure_and_distinguishes_unmount(self):
        service = VolumeService(auto_connect=False)
        service.manager = EmptyManager()
        groups = [VolumeGroup(str(i), f"Device {i}", [], [object()], []) for i in range(3)]
        service.groups = lambda: groups
        statuses = ["failed", "removed", "unmounted"]
        calls = []
        def remove(key, progress, done):
            calls.append(key)
            done(RemovalResult(statuses[int(key)], "simulated"))
        service.remove = remove
        bar = VolumeBar(service)
        bar.eject_all()
        wait_until(lambda: not bar._busy)
        self.assertEqual(calls, ["0", "1", "2"])
        self.assertIn("safely removed 1, unmounted only 1, failed 1", bar.status.text())
        self.assertIn("Device 2", bar.status.text())
        bar.deleteLater()

    def test_unchanged_close_does_not_rewrite_configuration(self):
        before = self.path.stat().st_mtime_ns
        self.window.close()
        self.assertEqual(self.path.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
