import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from config_store import ConfigStore, ConfigError, default_config, migrate_config, import_config, MAX_CONFIG_BYTES
from localization import set_language


class MigrationTests(unittest.TestCase):
    def setUp(self):
        set_language("en")

    def test_old_nine_array_maps_to_first_column(self):
        source = [f"/demo/{i}" for i in range(9)]
        cfg, notices = migrate_config(source)
        self.assertEqual([cfg["lastOpenedFiles"][i * 4] for i in range(9)], source)
        self.assertEqual(sum(bool(p) for p in cfg["lastOpenedFiles"]), 9)
        self.assertFalse(notices)

    def test_object_single_column_and_metadata_are_mapped_together(self):
        source = [f"/demo/{i}" for i in range(9)]
        cfg, _ = migrate_config({"lastOpenedFiles": source, "itemCount": 9, "lastModifiedTimes": list(range(9))})
        self.assertEqual([cfg["lastOpenedFiles"][i * 4] for i in range(9)], source)
        self.assertEqual([cfg["lastModifiedTimes"][i * 4] for i in range(9)], list(range(9)))

    def test_missing_count_uses_original_length(self):
        source = [f"/demo/{i}" for i in range(36)]
        cfg, _ = migrate_config({"lastOpenedFiles": source})
        self.assertEqual(cfg["itemCount"], 9)
        self.assertFalse(cfg["isLargePanel"])
        self.assertEqual(cfg["lastOpenedFiles"][:36], source)

    def test_compact_four_columns_preserve_row_order(self):
        source = [f"/demo/{i}" for i in range(36)]
        cfg, _ = migrate_config({"lastOpenedFiles": source, "itemCount": 9, "isLargePanel": True})
        self.assertEqual(cfg["lastOpenedFiles"][:36], source)
        self.assertTrue(cfg["isLargePanel"])

    def test_top_level_compact_array_defaults_to_single_panel(self):
        source = [f"/demo/{i}" for i in range(36)]
        cfg, _ = migrate_config(source)
        self.assertEqual(cfg["itemCount"], 9)
        self.assertFalse(cfg["isLargePanel"])
        self.assertEqual(cfg["lastOpenedFiles"][:36], source)

    def test_ambiguous_51_keeps_every_entry_with_warning(self):
        source = [f"/demo/{i}" for i in range(51)]
        cfg, notices = migrate_config(source)
        self.assertEqual(cfg["lastOpenedFiles"][:51], source)
        self.assertEqual(cfg["itemCount"], 9)
        self.assertIn("All 51 entries", notices[0].text())

    def test_fixed_200_slots_round_trip_preserves_hidden_cells(self):
        cfg = default_config()
        cfg["lastOpenedFiles"][199] = "/demo/hidden"
        cfg["lastModifiedTimes"][199] = 123
        result, _ = migrate_config(cfg)
        self.assertEqual(result, cfg)

    def test_overflow_rejected_for_both_formats(self):
        paths = [f"/demo/{i}" for i in range(201)]
        for raw in (paths, {"lastOpenedFiles": paths}):
            with self.subTest(raw_type=type(raw)), self.assertRaises(ConfigError):
                migrate_config(raw)

    def test_invalid_slot_types_and_relative_paths_rejected(self):
        # None is coerced to "" (legacy default), not rejected
        for value in (True, "relative/file", "/nul\x00path"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                migrate_config({"lastOpenedFiles": [value]})

    def test_legacy_null_slots_are_coerced_with_notice(self):
        # 旧版本会把空槽位序列化为 null；不能整个拒绝配置，得归一化并提示
        # 单列布局：n == count 时 target = i*4，所以路径落到 [0,4,8,12,16]
        raw = {"lastOpenedFiles": [None, "/usr/bin/x", None, "", "/etc/y"], "itemCount": 5}
        cfg, notices = migrate_config(raw)
        self.assertEqual(cfg["lastOpenedFiles"][:20:4], ["", "/usr/bin/x", "", "", "/etc/y"])
        self.assertTrue(any("Coerced" in n.text() for n in notices))
        # 真实目标不应该丢
        self.assertIn("/usr/bin/x", cfg["lastOpenedFiles"])
        self.assertIn("/etc/y", cfg["lastOpenedFiles"])

    def test_invalid_metadata_is_optional_and_non_fatal(self):
        cfg, notices = migrate_config({"lastOpenedFiles": ["/demo/a"], "itemCount": 1, "lastModifiedTimes": [float("nan")]})
        self.assertEqual(cfg["lastModifiedTimes"][0], 0)
        self.assertTrue(notices)

    def test_unsupported_version_and_incomplete_v2_are_rejected(self):
        for cfg in ({"schemaVersion": 99}, {"schemaVersion": 2, "itemCount": 9, "lastOpenedFiles": []}):
            with self.subTest(config=cfg), self.assertRaises(ConfigError):
                migrate_config(cfg)

    def test_language_alias_and_mac_path_preserved(self):
        cfg, _ = migrate_config({"lastOpenedFiles": ["/Applications/Example.app"], "itemCount": 1, "preferences": {"language": "zh-Hans"}})
        self.assertEqual(cfg["preferences"]["language"], "zh_CN")
        self.assertEqual(cfg["lastOpenedFiles"][0], "/Applications/Example.app")


class StorageTests(unittest.TestCase):
    def setUp(self):
        set_language("en")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / "config" / "myPanel.json"
        self.store = ConfigStore(self.path)

    def write(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text)

    def test_new_save_creates_directory_private_atomic_file(self):
        result = self.store.load()
        self.store.save(result.config)
        self.assertEqual(json.loads(self.path.read_text()), result.config)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.path.parent.glob(".myPanel-*")))

    def test_corrupt_original_unchanged_and_backups_unique(self):
        self.write("{broken")
        first = self.store.load()
        second = ConfigStore(self.path).load()
        self.assertTrue(first.protected)
        self.assertEqual(self.path.read_text(), "{broken")
        self.assertNotEqual(first.backup, second.backup)
        self.assertEqual(first.backup.read_text(), "{broken")
        with self.assertRaises(ConfigError):
            self.store.save(default_config())

    def test_failed_backup_blocks_automatic_and_explicit_overwrite(self):
        self.write("{broken")
        with patch.object(self.store, "_backup", side_effect=PermissionError("denied")):
            self.assertTrue(self.store.load().protected)
            with self.assertRaises(ConfigError):
                self.store.save(default_config())
            with self.assertRaises(ConfigError):
                self.store.save(default_config(), recover=True)
        self.assertEqual(self.path.read_text(), "{broken")

    def test_explicit_recovery_requires_and_keeps_backup(self):
        self.write("{broken")
        self.store.load()
        backup = self.store.save(default_config(), recover=True)
        self.assertEqual(backup.read_text(), "{broken")
        self.assertFalse(self.store.protected)
        self.assertEqual(json.loads(self.path.read_text())["schemaVersion"], 2)

    def test_external_edits_are_not_overwritten(self):
        self.store.load()
        self.store.save(default_config())
        second = ConfigStore(self.path)
        second.load()
        changed = default_config()
        changed["lastOpenedFiles"][0] = "/demo/new"
        self.store.save(changed)
        with self.assertRaises(ConfigError):
            second.save(default_config())
        self.assertEqual(json.loads(self.path.read_text())["lastOpenedFiles"][0], "/demo/new")

    def test_reload_valid_configuration_clears_protection(self):
        self.write("{broken")
        self.assertTrue(self.store.load().protected)
        self.write(json.dumps(default_config()))
        self.assertFalse(self.store.load().protected)
        self.store.save(default_config())

    def test_failed_atomic_replace_preserves_previous_file(self):
        self.store.load()
        self.store.save(default_config())
        old = self.path.read_bytes()
        with patch("config_store.os.replace", side_effect=PermissionError("denied")):
            with self.assertRaises(ConfigError):
                self.store.save(default_config())
        self.assertEqual(self.path.read_bytes(), old)
        self.assertFalse(list(self.path.parent.glob(".myPanel-*")))

    def test_import_never_changes_source_and_backup_keeps_current(self):
        self.store.load()
        self.store.save(default_config())
        source = pathlib.Path(self.temp.name) / "source.json"
        source.write_text(json.dumps(["/demo/imported"]))
        before = source.read_bytes()
        cfg, _ = import_config(source)
        backup = self.store.save(cfg, backup=True)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(json.loads(backup.read_text())["lastOpenedFiles"][0], "")
        self.assertEqual(json.loads(self.path.read_text())["lastOpenedFiles"][0], "/demo/imported")

    def test_invalid_root_future_schema_and_nonstandard_json_protect_original(self):
        for value in ("true", '{"schemaVersion":99}', '{"lastModifiedTimes":[NaN]}'):
            with self.subTest(value=value):
                self.write(value)
                store = ConfigStore(self.path)
                self.assertTrue(store.load().protected)
                with self.assertRaises(ConfigError):
                    store.save(default_config())
                self.assertEqual(self.path.read_text(), value)

    def test_oversized_import_is_rejected_without_changing_source(self):
        self.write(" " * (MAX_CONFIG_BYTES + 1))
        with self.assertRaises(ConfigError):
            import_config(self.path)
        self.assertEqual(self.path.stat().st_size, MAX_CONFIG_BYTES + 1)


if __name__ == "__main__":
    unittest.main()
