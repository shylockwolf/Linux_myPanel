"""Desktop application metadata and asynchronous file/application launching."""

from __future__ import annotations

import pathlib
import time
from concurrent.futures import Future

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib
try:
    gi.require_version("GioUnix", "2.0")
    from gi.repository import GioUnix
    DesktopAppInfo = GioUnix.DesktopAppInfo
except (ValueError, ImportError):
    DesktopAppInfo = Gio.DesktopAppInfo

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QStyle

from localization import _, language_names


class Task(QRunnable):
    def __init__(self, work, future):
        super().__init__()
        self.work = work
        self.future = future

    def run(self):
        if not self.future.set_running_or_notify_cancel():
            return
        try:
            result = self.work()
        except Exception as exc:
            self.future.set_exception(exc)
        else:
            self.future.set_result(result)


class AsyncTasks(QObject):
    """Poll primitive futures on the UI thread; workers own no signal QObjects."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks = {}
        self._timer = QTimer(self)
        self._timer.setInterval(5)
        self._timer.timeout.connect(self._poll)

    def run(self, work, callback, timeout_ms=15000):
        future = Future()
        self._tasks[future] = (callback, time.monotonic() + timeout_ms / 1000)
        self._timer.start()
        QThreadPool.globalInstance().start(Task(work, future))

    def _poll(self):
        for future, (callback, deadline) in list(self._tasks.items()):
            if future.done():
                del self._tasks[future]
                try:
                    result, error = future.result(), None
                except Exception as exc:
                    result, error = None, exc
                if callback:
                    callback(result, error)
            elif callback and time.monotonic() >= deadline:
                self._tasks[future] = (None, deadline)
                future.cancel()
                callback(None, TimeoutError(_("操作超时，请检查目标是否可访问。")))
        if not self._tasks:
            self._timer.stop()


class GlibPump(QObject):
    """GVfs/UDisks callbacks run on the same main thread as Qt."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.dispatch)
        self.timer.start(25)

    def dispatch(self):
        context = GLib.MainContext.default()
        for _i in range(20):
            if not context.pending():
                break
            context.iteration(False)


def desktop_info(path):
    if not path.endswith(".desktop"):
        return None
    try:
        return DesktopAppInfo.new_from_filename(path)
    except (GLib.Error, TypeError):
        return None


def localized_name(info):
    # DesktopAppInfo can discard translations outside the process locale.
    # Retain the original keys and explicitly apply the user's application locale.
    keyfile = GLib.KeyFile()
    try:
        keyfile.load_from_file(info.get_filename(), GLib.KeyFileFlags.KEEP_TRANSLATIONS)
        for language in language_names():
            value = keyfile.get_locale_string("Desktop Entry", "Name", language)
            if value:
                return value
    except GLib.Error:
        pass
    return info.get_name()


def list_installed_apps():
    """GIO honors desktop IDs, XDG precedence, Hidden, TryExec and show-in rules."""
    apps = []
    for info in Gio.AppInfo.get_all():
        if not isinstance(info, DesktopAppInfo) or not info.should_show():
            continue
        path = info.get_filename()
        if path:
            apps.append((localized_name(info), path))
    return sorted(apps, key=lambda item: item[0].casefold())


class DesktopServices:
    @staticmethod
    def describe(path):
        """Worker-only path/metadata IO; return a primitive icon description."""
        name = pathlib.Path(path).name or path
        exists, icon = False, None
        try:
            metadata = Gio.File.new_for_path(path).query_info("standard::icon", Gio.FileQueryInfoFlags.NONE, None)
            exists, icon = True, metadata.get_icon()
            info = desktop_info(path)
            if info:
                name = localized_name(info)
                icon = info.get_icon() or icon
        except GLib.Error:
            pass
        if isinstance(icon, Gio.FileIcon):
            specification = ("file", icon.get_file().get_path())
        elif isinstance(icon, Gio.ThemedIcon):
            specification = ("theme", icon.get_names())
        else:
            specification = ("theme", ["text-x-generic"])
        return name, exists, specification

    @staticmethod
    def metadata_icon(specification, style):
        kind, value = specification
        icon = QIcon(value) if kind == "file" and value else QIcon()
        if kind == "theme":
            for name in value:
                icon = QIcon.fromTheme(name)
                if not icon.isNull():
                    break
        return icon if not icon.isNull() else style.standardIcon(QStyle.SP_FileIcon)

    @staticmethod
    def prepare_target(path):
        """Worker-only validation; the UI then requests cancellable GIO launch."""
        if not path:
            raise ValueError(_("该槽位尚未设置目标。"))
        target = pathlib.Path(path)
        if not target.is_absolute() or not target.exists():
            raise ValueError(_("目标不存在或不可访问：\n%s") % path)
        if target.suffix == ".app":
            raise ValueError(_("macOS 应用不能在 Linux 启动，请重新定位为 Linux 应用。"))
        if target.suffix == ".desktop":
            info = desktop_info(path)
            if info is None:
                raise ValueError(_("无效或不可用的应用启动项：\n%s") % path)
            return info, None
        else:
            return None, target.as_uri()


class DesktopLauncher(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.tasks = AsyncTasks(self)
        self._pending = []

    def open(self, path, done):
        self.tasks.run(lambda: DesktopServices.prepare_target(path),
                       lambda prepared, error: done(error) if error else self._launch(prepared, done))

    def _launch(self, prepared, done):
        info, uri = prepared
        cancellable = Gio.Cancellable()
        self._pending.append(cancellable)
        timer = QTimer(self)
        timer.setSingleShot(True)
        state = {"done": False}

        def finish(error=None):
            if state["done"]:
                return
            state["done"] = True
            timer.stop()
            timer.deleteLater()
            self._pending.remove(cancellable)
            done(error)

        def expired():
            cancellable.cancel()
            finish(TimeoutError(_("操作超时，请检查目标是否可访问。")))

        def completed(source, result, _data):
            try:
                ok = info.launch_uris_finish(result) if info is not None else Gio.AppInfo.launch_default_for_uri_finish(result)
                finish(None if ok else ValueError(_("启动应用失败：%s") % (uri or info.get_filename())))
            except GLib.Error as error:
                finish(error)

        timer.timeout.connect(expired)
        timer.start(15000)
        try:
            if info is not None:
                info.launch_uris_async([], None, cancellable, completed, None)
            else:
                Gio.AppInfo.launch_default_for_uri_async(uri, None, cancellable, completed, None)
        except (GLib.Error, TypeError) as error:
            finish(error)
