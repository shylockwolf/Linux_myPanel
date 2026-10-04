import os
import pathlib
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from PySide6.QtWidgets import QApplication
from volume_service import VolumeService, PREFIX
from localization import set_language

APP = QApplication.instance() or QApplication([])


class Value:
    def __init__(self, value): self.value = value
    def unpack(self): return self.value


class Proxy:
    def __init__(self, **properties): self.properties = properties
    def get_cached_property(self, key):
        return Value(self.properties[key]) if key in self.properties else None


class Object:
    def __init__(self, path, **interfaces): self.path, self.interfaces = path, interfaces
    def get_object_path(self): return self.path
    def get_interface(self, name): return self.interfaces.get(name.removeprefix(PREFIX))


class Manager:
    def __init__(self, objects): self.objects = objects
    def get_objects(self): return self.objects


def drive(name="drive1", sibling="", removable=False, power=True, eject=False):
    return Object("/drives/" + name, Drive=Proxy(Model=name, ConnectionBus="usb", Removable=removable, CanPowerOff=power, Ejectable=eject, SiblingId=sibling))


def mount(name="a", drive_path="/drives/drive1", path="/media/demo/a", **extra):
    return Object("/blocks/" + name, Block=Proxy(Drive=drive_path, HintSystem=False, IdLabel=name, **extra), Filesystem=Proxy(MountPoints=[(path + "\0").encode()]))


class VolumeTests(unittest.TestCase):
    def setUp(self):
        set_language("en")
        self.service = VolumeService(auto_connect=False)
        self.service.manager = Manager([drive(), mount()])
        self.calls = []

    def simulate(self, fail=None, after_unmount=None):
        def call(proxy, method, done):
            self.calls.append(method)
            if method == fail:
                done(False, "simulated error")
                return
            if method == "Unmount":
                proxy.properties["MountPoints"] = []
                if after_unmount:
                    after_unmount()
            done(True, "")
        self.service._call = call

    def remove(self):
        key = self.service.groups()[0].key
        results = []
        self.service.remove(key, lambda text: None, results.append)
        return results[0]

    def test_usb_non_removable_media_is_listed(self):
        self.assertFalse(self.service.manager.objects[0].interfaces["Drive"].properties["Removable"])
        self.assertEqual(len(self.service.groups()), 1)

    def test_system_mount_never_offered_for_removal(self):
        self.service.manager.objects.append(mount("root", path="/"))
        self.assertEqual(self.service.groups(), [])

    def test_internal_non_removable_disk_is_not_listed(self):
        self.service.manager.objects[0].interfaces["Drive"].properties["ConnectionBus"] = "ata"
        self.assertEqual(self.service.groups(), [])

    def test_multiple_partitions_and_sibling_drives_grouped(self):
        self.service.manager = Manager([drive("d1", "physical"), drive("d2", "physical"), mount("a", "/drives/d1"), mount("b", "/drives/d2")])
        self.assertEqual(len(self.service.groups()), 1)
        self.simulate()
        self.assertEqual(self.remove().status, "removed")
        self.assertEqual(self.calls, ["Unmount", "Unmount", "PowerOff"])

    def test_poweroff_failure_is_not_success(self):
        self.simulate(fail="PowerOff")
        result = self.remove()
        self.assertEqual(result.status, "failed")
        self.assertIn("safe removal failed", result.detail)

    def test_unmount_failure_does_not_attempt_poweroff(self):
        self.simulate(fail="Unmount")
        self.assertEqual(self.remove().status, "failed")
        self.assertEqual(self.calls, ["Unmount"])

    def test_unsupported_poweroff_reports_unmounted_only(self):
        self.service.manager.objects[0].interfaces["Drive"].properties["CanPowerOff"] = False
        self.simulate()
        result = self.remove()
        self.assertEqual(result.status, "unmounted")
        self.assertEqual(self.calls, ["Unmount"])

    def test_new_mount_prevents_poweroff(self):
        self.simulate(after_unmount=lambda: self.service.manager.objects.append(mount("new")))
        self.assertEqual(self.remove().status, "failed")
        self.assertEqual(self.calls, ["Unmount"])

    def test_new_system_mount_hidden_from_ui_also_prevents_poweroff(self):
        self.simulate(after_unmount=lambda: self.service.manager.objects.append(mount("new-system", path="/")))
        self.assertEqual(self.remove().status, "failed")
        self.assertEqual(self.calls, ["Unmount"])
        self.assertEqual(self.service.groups(), [])

    def test_new_hint_system_mount_also_prevents_poweroff(self):
        added = mount("new-system")
        added.interfaces["Block"].properties["HintSystem"] = True
        self.simulate(after_unmount=lambda: self.service.manager.objects.append(added))
        self.assertEqual(self.remove().status, "failed")
        self.assertEqual(self.calls, ["Unmount"])

    def test_removed_device_button_does_not_execute_any_operation(self):
        key = self.service.groups()[0].key
        self.service.manager.objects.clear()
        self.simulate()
        results = []
        self.service.remove(key, lambda text: None, results.append)
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(self.calls, [])

    def test_unlocked_encrypted_volume_is_associated_with_its_drive(self):
        backing = Object("/blocks/encrypted", Block=Proxy(Drive="/drives/drive1"))
        unlocked = mount("unlocked", "/", CryptoBackingDevice="/blocks/encrypted")
        self.service.manager = Manager([drive(), backing, unlocked])
        self.assertEqual(len(self.service.groups()), 1)
        self.assertEqual(self.service.groups()[0].label, "unlocked")


if __name__ == "__main__":
    unittest.main()
