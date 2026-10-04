"""UDisks2 object notifications and cancellable device-level safe removal."""

from dataclasses import dataclass

from gi.repository import Gio, GLib
from PySide6.QtCore import QObject, QTimer, Signal
from localization import _

PREFIX = "org.freedesktop.UDisks2."


def property_value(proxy, key, default=None):
    value = proxy.get_cached_property(key)
    return value.unpack() if value is not None else default


def mount_paths(proxy):
    paths = property_value(proxy, "MountPoints", [])
    return [bytes(path).rstrip(b"\x00").decode("utf-8", "replace") for path in paths if path]


@dataclass
class VolumeGroup:
    key: str
    label: str
    drives: list
    mounts: list
    paths: list[str]


@dataclass(frozen=True)
class RemovalResult:
    status: str  # removed, unmounted, failed
    detail: str = ""


class VolumeService(QObject):
    changed = Signal()
    error_changed = Signal(str)

    def __init__(self, parent=None, *, auto_connect=True):
        super().__init__(parent)
        self.manager = None
        self.error = ""
        self._operations = []
        self._connecting = False
        if auto_connect:
            QTimer.singleShot(0, self.connect_service)

    def connect_service(self):
        if self.manager or self._connecting:
            return
        self._connecting = True
        Gio.DBusObjectManagerClient.new_for_bus(
            Gio.BusType.SYSTEM, Gio.DBusObjectManagerClientFlags.NONE,
            "org.freedesktop.UDisks2", "/org/freedesktop/UDisks2",
            None, None, None, self._connected, None,
        )

    def _connected(self, _source, result, _data):
        self._connecting = False
        try:
            self.manager = Gio.DBusObjectManagerClient.new_for_bus_finish(result)
            for name in ("object-added", "object-removed", "interface-proxy-properties-changed"):
                self.manager.connect(name, lambda *_args: self.changed.emit())
            self.error = ""
        except GLib.Error as error:
            self.error = _("无法查询外接设备：%s") % error.message
        self.error_changed.emit(self.error)
        self.changed.emit()

    def refresh(self):
        if not self.manager:
            self.connect_service()
        self.changed.emit()

    def groups(self, *, include_protected=False):
        if not self.manager:
            return []
        drives, groups, unsafe = {}, {}, set()
        objects = self.manager.get_objects()
        blocks = {obj.get_object_path(): obj.get_interface(PREFIX + "Block") for obj in objects}
        def drive_key(block):
            visited = set()
            while block is not None:
                path = property_value(block, "Drive", "")
                if path in drives:
                    return drives[path]
                backing = property_value(block, "CryptoBackingDevice", "")
                if backing in visited or backing not in blocks:
                    return None
                visited.add(backing)
                block = blocks[backing]
            return None
        for obj in objects:
            drive = obj.get_interface(PREFIX + "Drive")
            if drive is None:
                continue
            external = property_value(drive, "ConnectionBus", "") in ("usb", "firewire", "sdio")
            if not (external or property_value(drive, "Removable", False) or property_value(drive, "Ejectable", False)):
                continue
            key = property_value(drive, "SiblingId", "") or obj.get_object_path()
            drives[obj.get_object_path()] = key
            group = groups.setdefault(key, VolumeGroup(key, property_value(drive, "Model", "") or _("外接设备"), [], [], []))
            group.drives.append(drive)
        for obj in objects:
            block = obj.get_interface(PREFIX + "Block")
            filesystem = obj.get_interface(PREFIX + "Filesystem")
            if block is None or filesystem is None:
                continue
            key = drive_key(block)
            if key is None:
                continue
            paths = mount_paths(filesystem)
            if not paths:
                continue
            if property_value(block, "HintSystem", False) or any(p in ("/", "/home", "/boot", "/boot/efi") for p in paths):
                unsafe.add(key)
            group = groups[key]
            group.mounts.append(filesystem)
            group.paths.extend(paths)
            label = property_value(block, "IdLabel", "")
            if label and len(group.mounts) == 1:
                group.label = label
        return [group for key, group in groups.items()
                if group.mounts and (include_protected or key not in unsafe)]

    def _call(self, proxy, method, done):
        """An explicit timeout cancels an operation, never reports a late success."""
        cancellable = Gio.Cancellable()
        timer = QTimer(self)
        timer.setSingleShot(True)
        state = {"done": False}
        self._operations.append(cancellable)

        def finish(ok, message):
            if state["done"]:
                return
            state["done"] = True
            timer.stop()
            timer.deleteLater()
            self._operations.remove(cancellable)
            done(ok, message)

        def expired():
            cancellable.cancel()
            finish(False, _("操作超时，设备状态未知；请刷新后检查。"))

        def complete(source, result, _data):
            try:
                source.call_finish(result)
                finish(True, "")
            except GLib.Error as error:
                finish(False, error.message)

        timer.timeout.connect(expired)
        timer.start(35000)
        try:
            proxy.call(method, GLib.Variant("(a{sv})", ({},)),
                       Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION, 30000,
                       cancellable, complete, None)
        except (GLib.Error, TypeError) as error:
            finish(False, str(error))

    def remove(self, key, progress, done):
        # Resolve the key again: a cached button must not target a recycled /dev name.
        group = next((item for item in self.groups() if item.key == key), None)
        if group is None:
            done(RemovalResult("failed", _("设备已移除或挂载状态已改变，请刷新。")))
            return
        mounts = list(group.mounts)

        def step(index=0):
            if index < len(mounts):
                progress(_("正在卸载 %d/%d：%s") % (index + 1, len(mounts), group.label))
                def unmounted(ok, error):
                    if not ok:
                        done(RemovalResult("failed", _("卸载失败：%s") % error))
                    else:
                        step(index + 1)
                self._call(mounts[index], "Unmount", unmounted)
                return
            # Never power off a sibling with a newly mounted filesystem.
            if any(item.key == key for item in self.groups(include_protected=True)):
                done(RemovalResult("failed", _("设备仍有挂载卷，未执行断电；请刷新后重试。")))
                return
            drive = next((p for p in group.drives if property_value(p, "CanPowerOff", False)), None)
            method = "PowerOff"
            if drive is None:
                drive = next((p for p in group.drives if property_value(p, "Ejectable", False)), None)
                method = "Eject"
            if drive is None:
                done(RemovalResult("unmounted", _("已卸载；设备不支持安全弹出或断电。")))
                return
            progress(_("正在安全弹出：%s") % group.label)
            def removed(ok, error):
                done(RemovalResult("removed" if ok else "failed", "" if ok else _("已卸载，但安全弹出失败：%s") % error))
            self._call(drive, method, removed)
        step()
