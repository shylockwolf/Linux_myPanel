"""Qt views for slots, configuration and desktop devices."""

import copy
import pathlib
import time

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFontMetrics, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSizePolicy, QStyle, QVBoxLayout, QWidget,
)
from config_store import (
    COLUMNS, SLOT_CAPACITY, DEFAULT_ITEM_COUNT, MAX_ITEM_COUNT,
    ConfigStore, ConfigError, default_config, import_config,
)
from desktop_services import DesktopServices, DesktopLauncher, AsyncTasks, list_installed_apps
from localization import _
from metadata import APP_NAME, APP_VERSION, APP_AUTHOR, APP_DATE
from volume_service import VolumeService

SLOT_MIN_WIDTH = 250
SLOT_MIN_HEIGHT = 48
GRID_H_SPACING = 10
GRID_V_SPACING = 6
GRID_MARGIN = 10
GRID_SIDE_MARGIN = 20
WINDOW_CHROME_HEIGHT = 160

class TargetChooserDialog(QDialog):
    """选择目标：文件 / 文件夹 / 已安装应用（PRD §4.2）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("选择目标"))
        self.chosen: str | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(_("请选择要放入槽位的内容：")))

        btn_file = QPushButton(_("选择文件…"))
        btn_dir = QPushButton(_("选择文件夹…"))
        btn_app = QPushButton(_("从已安装应用中选择…"))
        btn_file.clicked.connect(self._pick_file)
        btn_dir.clicked.connect(self._pick_dir)
        btn_app.clicked.connect(self._pick_app)
        for b in (btn_file, btn_dir, btn_app):
            b.setMinimumHeight(32)
            layout.addWidget(b)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            _("选择文件"),
            str(pathlib.Path.home()),
            _("所有文件 (*)"),
        )
        if path:
            self.chosen = path
            self.accept()

    def _pick_dir(self):
        path = QFileDialog.getExistingDirectory(
            self, _("选择文件夹"), str(pathlib.Path.home())
        )
        if path:
            self.chosen = path
            self.accept()

    def _pick_app(self):
        dlg = AppChooserDialog(self)
        if dlg.exec() == QDialog.Accepted and dlg.chosen:
            self.chosen = dlg.chosen
            self.accept()


class AppChooserDialog(QDialog):
    """从已安装应用列表选择（PRD §4.2）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("选择应用"))
        self.resize(520, 460)
        self.chosen: str | None = None
        self._apps = []
        self._tasks = AsyncTasks(self)

        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText(_("搜索应用…"))
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._accept())
        layout.addWidget(self.list)

        self.status = QLabel(_("正在读取应用列表…"))
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate()
        self.search.setFocus()
        self._tasks.run(list_installed_apps, self._loaded)

    def _loaded(self, apps, error):
        if error:
            self.status.setText(_("无法读取应用列表：%s") % str(error))
            return
        self._apps = apps
        self.status.setText(_("共 %d 个应用") % len(apps))
        self._populate(self.search.text())

    def _populate(self, keyword: str = ""):
        self.list.clear()
        kw = keyword.strip().lower()
        for name, path in self._apps:
            if kw and kw not in name.lower():
                continue
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, path)
            item.setToolTip(path)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _filter(self, text: str):
        self._populate(text)

    def _accept(self):
        item = self.list.currentItem()
        if item is None:
            return
        self.chosen = item.data(Qt.UserRole)
        self.accept()


class ElidedLabel(QLabel):
    """单行标签：宽度固定，放不下时用省略号截断（完整文本由调用方放进 tooltip）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = ""

    def full_text(self) -> str:
        return self._full_text

    def set_full_text(self, text: str):
        self._full_text = text or ""
        self._refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self):
        if not self._full_text:
            self.setText("")
            return
        metrics = QFontMetrics(self.font())
        if metrics.horizontalAdvance(self._full_text) <= self.width():
            # 放得下就原样显示，避免 elidedText 在宽度刚好够时也插入省略号
            self.setText(self._full_text)
            return
        self.setText(metrics.elidedText(self._full_text, Qt.ElideMiddle, self.width()))


class SlotWidget(QWidget):
    """单个槽位（PRD §4.1 / §5.1）。不包含任何文件/卷管理逻辑。"""

    add_requested = Signal()
    open_requested = Signal()
    clear_requested = Signal()
    repair_requested = Signal()

    ICON_SIZE = 32

    def __init__(self, index: int, parent=None):
        super().__init__(parent)
        self.index = index
        self.target_path = ""
        self.setMinimumSize(SLOT_MIN_WIDTH, SLOT_MIN_HEIGHT)
        # 垂直方向固定高度：高度由内容决定，避免被网格拉伸后
        # 在槽位内部出现大片空白。
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        self.icon_label = QLabel()
        self.icon_label.setAlignment(Qt.AlignCenter)
        self.icon_label.setFixedSize(self.ICON_SIZE, self.ICON_SIZE)

        self.name_label = ElidedLabel()
        self.name_label.setMinimumWidth(90)
        self.name_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.name_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.open_btn = QPushButton()
        self.open_btn.setFixedSize(32, 32)
        self.open_btn.clicked.connect(self._on_open_clicked)
        self.repair_btn = QPushButton("↻")
        self.repair_btn.setFixedSize(24, 28)
        self.repair_btn.setToolTip(_("更换目标 / 重新定位"))
        self.repair_btn.setAccessibleName(_("更换目标 / 重新定位"))
        self.repair_btn.clicked.connect(self.repair_requested.emit)
        self.clear_btn = QPushButton("×")
        self.clear_btn.setFixedSize(22, 22)
        self.clear_btn.setToolTip(_("清除该槽位"))
        self.clear_btn.setAccessibleName(_("清除该槽位"))
        self.clear_btn.clicked.connect(self.clear_requested.emit)

        # 图标 / 名称 / 打开 / 清除 紧凑地靠左排列（名称紧贴图标，按钮紧跟名称）
        layout.addWidget(self.open_btn, 0)
        layout.addWidget(self.icon_label, 0, Qt.AlignVCenter)
        layout.addWidget(self.name_label, 1, Qt.AlignVCenter)
        layout.addWidget(self.repair_btn, 0)
        layout.addWidget(self.clear_btn, 0)

        self.set_target("")

    # -- 状态 --

    def set_target(self, path: str):
        self.target_path = path or ""

        if not self.target_path:
            self._full_name = _("添加")
            self.name_label.setToolTip(_("点击「+」选择文件、文件夹或应用"))
            self.open_btn.setText("+")
            self.open_btn.setToolTip(_("添加目标"))
            self.open_btn.setAccessibleName(_("添加目标"))
            self.open_btn.setIcon(QIcon())
            self.repair_btn.setEnabled(False)
            self.clear_btn.setEnabled(False)
        else:
            self._full_name = pathlib.Path(self.target_path).name or self.target_path
            tip = f"{self._full_name}\n{self.target_path}"
            self.name_label.setToolTip(tip)
            icon = QIcon.fromTheme("media-playback-start")
            self.open_btn.setIcon(icon)
            self.open_btn.setText("▶" if icon.isNull() else "")
            self.open_btn.setToolTip(_("打开：%s") % self.target_path)
            self.open_btn.setAccessibleName(_("打开：%s") % self._full_name)
            self.repair_btn.setEnabled(True)
            self.clear_btn.setEnabled(True)
            expected = self.target_path
            # AsyncTasks 挂在 MyPanelWindow 上（见 __init__），所以这里取
            # self.window().tasks。如果 window() 暂时为 None（极端情况，
            # 如 widget 尚未 reparent），先放过同步路径的兜底图标，
            # 让后续交互重新触发。
            tasks_owner = self.window()
            if tasks_owner is not None:
                tasks_owner.tasks.run(
                    lambda: DesktopServices.describe(expected),
                    lambda result, error: self._metadata_loaded(expected, result, error),
                )

        self.name_label.set_full_text(getattr(self, "_full_name", ""))
        self._refresh_icon()

    def _refresh_icon(self):
        style = self.style()
        if not self.target_path:
            self.icon_label.clear()
            self.icon_label.setText("+")
            return
        icon = style.standardIcon(QStyle.SP_FileIcon)
        self.icon_label.setText("")
        self.icon_label.setPixmap(icon.pixmap(self.ICON_SIZE, self.ICON_SIZE))

    def _metadata_loaded(self, expected, result, error):
        if self.target_path != expected:
            return
        if error:
            self.name_label.setToolTip(f"{expected}\n{error}")
            return
        name, exists, specification = result
        self._full_name = name
        self.name_label.set_full_text(name)
        tip = f"{name}\n{expected}"
        if not exists:
            tip += "\n" + _("⚠ 目标不存在或不可访问，请重新定位或清除")
        self.name_label.setToolTip(tip)
        self.open_btn.setAccessibleName(_("打开：%s") % name)
        icon = DesktopServices.metadata_icon(specification, self.style())
        self.icon_label.setPixmap(icon.pixmap(self.ICON_SIZE, self.ICON_SIZE))

    def _on_open_clicked(self):
        if self.target_path:
            self.open_requested.emit()
        else:
            self.add_requested.emit()


class VolumeBar(QWidget):
    """Progress and honest per-device results; one failure does not stop a batch."""
    def __init__(self, service=None, parent=None):
        super().__init__(parent)
        self.service = service if service is not None else VolumeService(self)
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 4, 16, 4)
        header = QHBoxLayout()
        header.addWidget(QLabel(_("外接卷")), 1)
        self.btn_refresh = QPushButton(_("刷新"))
        self.btn_refresh.clicked.connect(self.service.refresh)
        header.addWidget(self.btn_refresh)
        self.btn_eject_all = QPushButton(_("全部弹出"))
        self.btn_eject_all.clicked.connect(self.eject_all)
        header.addWidget(self.btn_eject_all)
        layout.addLayout(header)
        self.rows_box = QVBoxLayout()
        layout.addLayout(self.rows_box)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)
        self.service.changed.connect(lambda: self._refresh_timer.start(30))
        self.service.error_changed.connect(lambda _error: self._refresh_timer.start(30))
        self.refresh()

    def refresh(self):
        while self.rows_box.count():
            item = self.rows_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        groups = self.service.groups()
        self.btn_refresh.setEnabled(not self._busy)
        self.btn_eject_all.setEnabled(bool(groups) and not self._busy)
        if not groups:
            text = self.service.error or (_("正在查询外接设备…") if self.service.manager is None else _("无外接设备"))
            self.rows_box.addWidget(QLabel(text))
        for group in groups:
            row = QWidget()
            cells = QHBoxLayout(row)
            cells.setContentsMargins(0, 0, 0, 0)
            icon = QLabel()
            device_icon = QIcon.fromTheme("drive-removable-media", self.style().standardIcon(QStyle.SP_DriveHDIcon))
            icon.setPixmap(device_icon.pixmap(24, 24))
            cells.addWidget(icon)
            label = ElidedLabel()
            label.set_full_text(group.label)
            label.setMinimumWidth(80)
            label.setToolTip("\n".join(group.paths))
            cells.addWidget(label, 1)
            button = QPushButton(_("弹出"))
            button.setToolTip(_("卸载此设备的全部 %d 个卷并请求安全弹出") % len(group.mounts))
            button.setAccessibleName(_("弹出：%s") % group.label)
            button.setEnabled(not self._busy)
            button.clicked.connect(lambda _checked=False, key=group.key, name=group.label: self._remove([(key, name)]))
            cells.addWidget(button)
            self.rows_box.addWidget(row)

    def eject_all(self):
        self._remove([(group.key, group.label) for group in self.service.groups()])

    def _remove(self, targets):
        if self._busy or not targets:
            return
        self._busy = True
        self.refresh()
        results = []
        def step():
            if len(results) == len(targets):
                self._busy = False
                self.refresh()
                removed = sum(result.status == "removed" for _name, result in results)
                unmounted = sum(result.status == "unmounted" for _name, result in results)
                failed = sum(result.status == "failed" for _name, result in results)
                summary = _("完成：安全弹出 %d，仅卸载 %d，失败 %d") % (removed, unmounted, failed)
                details = []
                for name, result in results:
                    state = {"removed": _("已安全弹出"), "unmounted": _("仅卸载"), "failed": _("失败")}[result.status]
                    details.append(f"{name}：{state}" + (f" — {result.detail}" if result.detail else ""))
                self.status.setText(summary + "\n" + "\n".join(details))
                return
            key, name = targets[len(results)]
            prefix = _("设备 %d/%d：%s") % (len(results) + 1, len(targets), name)
            def progress(text):
                self.status.setText(prefix + "\n" + text)
            def finished(result):
                results.append((name, result))
                QTimer.singleShot(0, step)
            self.service.remove(key, progress, finished)
        step()


class SettingsDialog(QDialog):
    """设置：主题、语言、全局重置（PRD §4.6）。"""

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("设置"))
        self.config = config
        self.reset_requested = False
        self.import_requested = False
        self.recovery_requested = False

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(_("主题")))
        self.theme_combo = QComboBox()
        for value, label in (
            ("default", _("跟随系统")),
            ("light", _("浅色")),
            ("dark", _("深色")),
        ):
            self.theme_combo.addItem(label, value)
        current_theme = config.get("preferences", {}).get("theme", "default")
        idx = self.theme_combo.findData(current_theme)
        self.theme_combo.setCurrentIndex(idx if idx >= 0 else 0)
        layout.addWidget(self.theme_combo)

        layout.addWidget(QLabel(_("语言")))
        self.lang_combo = QComboBox()
        for value, label in (
            ("system", _("跟随系统")),
            ("zh_CN", "简体中文"),
            ("en", "English"),
        ):
            self.lang_combo.addItem(label, value)
        current_lang = config.get("preferences", {}).get("language", "system")
        idx = self.lang_combo.findData(current_lang)
        self.lang_combo.setCurrentIndex(idx if idx >= 0 else 0)
        layout.addWidget(self.lang_combo)

        hint = QLabel(_("语言变更在重启后对界面文本生效"))
        hint.setEnabled(False)
        layout.addWidget(hint)

        layout.addSpacing(8)
        btn_import = QPushButton(_("导入配置…"))
        btn_import.clicked.connect(self._on_import)
        layout.addWidget(btn_import)
        if parent and parent.store.protected:
            btn_recover = QPushButton(_("恢复并保存当前配置…"))
            btn_recover.clicked.connect(self._on_recovery)
            layout.addWidget(btn_recover)
        btn_reset = QPushButton(_("全局重置…"))
        btn_reset.clicked.connect(self._on_reset)
        layout.addWidget(btn_reset)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_import(self):
        self.import_requested = True
        self.accept()

    def _on_recovery(self):
        self.recovery_requested = True
        self.accept()

    def _on_reset(self):
        answer = QMessageBox.question(
            self,
            _("确认全局重置"),
            _(
                "这会清空全部槽位目标和应用偏好（主题、语言），且无法撤销。\n\n"
                "确定要继续吗？"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer == QMessageBox.Yes:
            self.reset_requested = True
            self.accept()

    def selected_theme(self) -> str:
        return self.theme_combo.currentData()

    def selected_language(self) -> str:
        return self.lang_combo.currentData()


class MyPanelWindow(QMainWindow):
    """主窗口。"""

    def __init__(self, store=None, result=None, volume_service=None):
        super().__init__()
        self.store = store if store is not None else ConfigStore()
        result = result if result is not None else self.store.load()
        self.config = copy.deepcopy(result.config)
        self._dirty = False
        self._save_error = None
        self._volume_service = volume_service
        self._fix_invariants()
        # 窗口级共享任务队列。所有槽位的异步元数据加载都走这里，
        # 避免 _relayout 销毁槽位时挂在 self 上的 QTimer 被一并释放。
        self.tasks = AsyncTasks(self)
        self._launcher = DesktopLauncher(self)
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(350, 450)
        self._slot_widgets = {}
        # 同一条 notice 在同一次启动中只弹一次：可能从 load / import / reset
        # 等多个入口连环触发。空集合（即所有 notice 都已被去重）则完全静默。
        self._shown_notices: set[str] = set()
        self._setup_ui()
        self._relayout()
        if result.notices:
            warning = "\n\n".join(notice.text() for notice in result.notices)
            QTimer.singleShot(0, lambda: QMessageBox.warning(self, _("配置提示"), warning))

    # ---- 配置 ----

    def _fix_invariants(self):
        """把 itemCount / isLargePanel 夹到合法范围（修掉越界值直接透传的问题）。"""
        ic = self.config.get("itemCount")
        if not isinstance(ic, int) or isinstance(ic, bool) or not (
            1 <= ic <= MAX_ITEM_COUNT
        ):
            ic = DEFAULT_ITEM_COUNT
        self.item_count = ic
        self.config["itemCount"] = ic
        self.is_large = bool(self.config.get("isLargePanel", False))
        self.config["isLargePanel"] = self.is_large

    def _last_opened(self):
        return list(self.config["lastOpenedFiles"])

    def _mtimes(self):
        return list(self.config["lastModifiedTimes"])

    def save(self):
        # Update visible cells only: hidden rows and other columns are preserved.
        self._dirty = True
        lof, mtimes = self._last_opened(), self._mtimes()
        now = int(time.time())
        for index, widget in self._slot_widgets.items():
            if lof[index] != widget.target_path:
                lof[index] = widget.target_path
                mtimes[index] = now if widget.target_path else 0
        # 写盘前兜底清理：内存里有人塞了 null 也不让它再写到磁盘里。
        # （migrate_config 已经会规整 null，但我们在写盘侧再补一道防御。）
        self.config["lastOpenedFiles"] = [p or "" for p in lof]
        self.config["lastModifiedTimes"] = mtimes
        self.config["itemCount"] = self.item_count
        self.config["isLargePanel"] = self.is_large
        try:
            self.store.save(self.config)
            self._dirty = False
            self._save_error = None
            self.statusBar().showMessage(_("已保存"), 2500)
            return True
        except ConfigError as error:
            self.statusBar().showMessage(str(error))
            if self._save_error != str(error):
                self._save_error = str(error)
                QMessageBox.warning(self, _("保存失败"), str(error))
            return False

    # ---- 界面构建 ----

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 8, 0, 8)
        root.setSpacing(6)

        root.addLayout(self._build_top_bar())

        self.volume_bar = VolumeBar(self._volume_service, self)
        root.addWidget(self.volume_bar)

        self.slots_host = QWidget()
        self.slots_layout = QGridLayout(self.slots_host)
        self.slots_layout.setHorizontalSpacing(GRID_H_SPACING)
        self.slots_layout.setVerticalSpacing(GRID_V_SPACING)
        self.slots_layout.setContentsMargins(
            GRID_SIDE_MARGIN, GRID_MARGIN, GRID_SIDE_MARGIN, GRID_MARGIN
        )

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidget(self.slots_host)
        root.addWidget(self.scroll, 1)

    def _build_top_bar(self):
        bar = QVBoxLayout()
        bar.setContentsMargins(16, 0, 16, 0)
        heading = QHBoxLayout()
        title = QLabel(APP_NAME)
        font = title.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 3)
        title.setFont(font)
        heading.addWidget(title, 1)
        meta = QLabel(f"{APP_AUTHOR}  ·  v{APP_VERSION}\n{APP_DATE}")
        meta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        meta.setToolTip(_("作者：%s") % APP_AUTHOR)
        heading.addWidget(meta)
        bar.addLayout(heading)
        controls = QHBoxLayout()
        self.btn_dec = QPushButton("−")
        self.btn_dec.setFixedSize(28, 28)
        self.btn_dec.setAccessibleName(_("减少每列槽位数"))
        self.btn_dec.setToolTip(_("减少每列槽位数"))
        self.btn_dec.clicked.connect(lambda: self._change_count(-1))
        self.count_label = QLabel(str(self.item_count))
        self.count_label.setAlignment(Qt.AlignCenter)
        self.count_label.setMinimumWidth(28)
        self.count_label.setAccessibleName(_("每列槽位数"))
        self.btn_inc = QPushButton("+")
        self.btn_inc.setFixedSize(28, 28)
        self.btn_inc.setAccessibleName(_("增加每列槽位数"))
        self.btn_inc.setToolTip(_("增加每列槽位数"))
        self.btn_inc.clicked.connect(lambda: self._change_count(+1))
        for widget in (self.btn_dec, self.count_label, self.btn_inc):
            controls.addWidget(widget)
        self.large_toggle = QCheckBox(_("大面板 (4列)"))
        self.large_toggle.setMinimumWidth(self.large_toggle.sizeHint().width())
        self.large_toggle.setChecked(self.is_large)
        self.large_toggle.toggled.connect(self._on_large_toggled)
        controls.addWidget(self.large_toggle)
        controls.addStretch()
        settings = QPushButton(_("设置"))
        settings.clicked.connect(self._open_settings)
        controls.addWidget(settings)
        bar.addLayout(controls)
        self._update_count_buttons()
        return bar

    # ---- 布局 ----

    def _change_count(self, delta: int):
        new = self.item_count + delta
        if not (1 <= new <= MAX_ITEM_COUNT):
            return
        self.item_count = new
        self.config["itemCount"] = new
        self.count_label.setText(str(new))
        self._update_count_buttons()
        self._relayout()
        self.save()

    def _update_count_buttons(self):
        self.btn_dec.setEnabled(self.item_count > 1)
        self.btn_inc.setEnabled(self.item_count < MAX_ITEM_COUNT)

    def _on_large_toggled(self, checked: bool):
        self.is_large = bool(checked)
        self.config["isLargePanel"] = self.is_large
        self._relayout()
        self.save()

    def _relayout(self):
        while self.slots_layout.count():
            item = self.slots_layout.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        self._slot_widgets.clear()

        lof = self._last_opened()
        rows = self.item_count
        cols = COLUMNS if self.is_large else 1

        for ui_idx in range(rows * cols):
            if self.is_large:
                row, col = divmod(ui_idx, COLUMNS)
                cfg_idx = row * COLUMNS + col
            else:
                row, col = ui_idx, 0
                cfg_idx = ui_idx * COLUMNS  # 单列 = 第一列

            slot = SlotWidget(ui_idx)
            slot.add_requested.connect(lambda i=cfg_idx: self._on_add(i))
            slot.open_requested.connect(lambda i=cfg_idx: self._on_open(i))
            slot.clear_requested.connect(lambda i=cfg_idx: self._on_clear(i))
            slot.repair_requested.connect(lambda i=cfg_idx: self._on_add(i))

            # 必须先放入布局，set_target 内部会用 self.window().tasks 派发
            # 异步图标加载；如果 widget 还没 reparent 到顶层窗口，window()
            # 会返回 None → AttributeError → 图标和名称都不会被刷新。
            self.slots_layout.addWidget(slot, row, col)
            self._slot_widgets[cfg_idx] = slot

            if 0 <= cfg_idx < SLOT_CAPACITY:
                slot.set_target(lof[cfg_idx])

        for c in range(COLUMNS):
            self.slots_layout.setColumnStretch(c, 1 if c < cols else 0)
        for r in range(MAX_ITEM_COUNT + 1):
            self.slots_layout.setRowStretch(r, 0)
        # 多余空间全部塞给最后一行（空行），避免被均分到各槽位行
        # 而在槽位之间撑出大片空白。
        self.slots_layout.setRowStretch(rows, 1)

        self._apply_window_size(rows, cols)

    def _apply_window_size(self, rows: int, cols: int):
        """按当前布局调整窗口尺寸（PRD §5.1）。"""
        if self.is_large:
            # 每列只占一个槽位所需的宽度：槽位内容紧贴列宽，
            # 列内不再留下大片空白（否则只调列间距肉眼看不出差别）。
            width = cols * (SLOT_MIN_WIDTH + GRID_H_SPACING) + 2 * GRID_SIDE_MARGIN
        else:
            width = 350

        # 用实际槽位高度（sizeHint）而不是最小高度，避免估算偏小
        sample = next(iter(self._slot_widgets.values()), None)
        row_h = sample.sizeHint().height() if sample is not None else SLOT_MIN_HEIGHT
        # 别忘了网格自身的上下留白
        slots_h = (
            rows * row_h
            + max(0, rows - 1) * GRID_V_SPACING
            + 2 * GRID_MARGIN
        )

        height = max(450, slots_h + WINDOW_CHROME_HEIGHT)
        if not self.isMaximized() and not self.isFullScreen():
            screen = self.screen() or QApplication.primaryScreen()
            max_width = screen.availableGeometry().width() - 40 if screen else width
            self.resize(min(width, max(350, max_width)), min(height, self._max_window_height()))

    def _max_window_height(self) -> int:
        """不超过屏幕可用高度，避免窗口超出显示器。"""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return 900
        available = screen.availableGeometry().height()
        return max(450, available - 80)

    # ---- 槽位动作 ----

    def _on_add(self, cfg_idx: int):
        dlg = TargetChooserDialog(self)
        if dlg.exec() != QDialog.Accepted or not dlg.chosen:
            return
        slot = self._slot_widgets.get(cfg_idx)
        if slot is None:
            return
        slot.set_target(dlg.chosen)
        self.save()  # PRD §4.2：选择后立即保存

    def _on_open(self, cfg_idx):
        slot = self._slot_widgets.get(cfg_idx)
        if slot is None:
            return
        path = slot.target_path
        slot.open_btn.setEnabled(False)
        def finished(error):
            current = self._slot_widgets.get(cfg_idx)
            if current and current.target_path == path:
                current.open_btn.setEnabled(True)
            if error:
                QMessageBox.warning(self, _("无法打开"), str(error))
        self._launcher.open(path, finished)

    def _on_clear(self, cfg_idx: int):
        slot = self._slot_widgets.get(cfg_idx)
        if slot is None:
            return
        slot.set_target("")
        self.save()

    # ---- 设置 ----

    def _replace_config(self, config, *, recover=False):
        try:
            backup = self.store.save(config, backup=True, recover=recover)
        except ConfigError as error:
            QMessageBox.warning(self, _("保存失败"), str(error))
            return False
        self.config = copy.deepcopy(config)
        # 写入前兜底清 null，避免历史 bug 写脏文件。
        self.config["lastOpenedFiles"] = [p or "" for p in self.config["lastOpenedFiles"]]
        self._dirty = False
        self._save_error = None
        self._fix_invariants()
        self.large_toggle.blockSignals(True)
        self.large_toggle.setChecked(self.is_large)
        self.large_toggle.blockSignals(False)
        self.count_label.setText(str(self.item_count))
        self._update_count_buttons()
        self._relayout()
        from main import apply_theme
        apply_theme(self.config)
        if backup:
            self.statusBar().showMessage(_("原配置已备份到：%s") % str(backup))
        return True

    def _on_import(self):
        path, _filter = QFileDialog.getOpenFileName(self, _("导入配置"), str(pathlib.Path.home()), _("JSON 配置 (*.json);;所有文件 (*)"))
        if not path:
            return
        try:
            config, notices = import_config(path)
        except ConfigError as error:
            QMessageBox.warning(self, _("导入失败"), str(error))
            return
        preview = _("将导入 %d 个目标，每列 %d 行。\n当前配置会先备份，随后替换；来源文件保持不变。") % (sum(bool(p) for p in config["lastOpenedFiles"]), config["itemCount"])
        if notices:
            preview += "\n\n" + "\n".join(notice.text() for notice in notices)
        answer = QMessageBox.question(self, _("确认导入"), preview, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes and self._replace_config(config, recover=self.store.protected):
            QMessageBox.information(self, _("导入完成"), _("配置已导入。语言设置将在重启后生效。"))

    def _on_recovery(self):
        answer = QMessageBox.question(self, _("确认恢复保存"),
            _("将先备份原文件，再用当前面板配置替换它。备份失败时不会覆盖。确定继续吗？"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self._replace_config(self.config, recover=True)

    def _open_settings(self):
        dlg = SettingsDialog(self.config, self)
        if dlg.exec() != QDialog.Accepted:
            return
        if dlg.import_requested:
            self._on_import()
            return
        if dlg.recovery_requested:
            self._on_recovery()
            return
        if dlg.reset_requested:
            self._replace_config(default_config(), recover=self.store.protected)
            return
        self.config["preferences"] = {"theme": dlg.selected_theme(), "language": dlg.selected_language()}
        self.save()
        from main import apply_theme
        apply_theme(self.config)
        self.statusBar().showMessage(_("语言变更在重启后对界面文本生效"))

    def closeEvent(self, event):
        if self.volume_bar._busy:
            QMessageBox.warning(self, _("操作进行中"), _("设备操作尚未完成，请等待结果后再关闭。"))
            event.ignore()
            return
        if self._dirty and not self.save():
            answer = QMessageBox.question(self, _("存在未保存的更改"),
                _("关闭会丢失本次未保存的更改，原配置保持不变。仍要关闭吗？"),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        super().closeEvent(event)
