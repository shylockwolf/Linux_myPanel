#!/usr/bin/env python3
"""myPanel — 轻量级 Linux 桌面「文件 / 应用」快捷面板。

分层（PRD §6）：
  - 配置层      : load_config / save_config / migrate_config / import_config
  - 桌面服务层  : DesktopServices（打开目标、图标解析、已安装应用、外接卷）
  - 界面层      : TargetChooserDialog / SlotWidget / VolumeBar / SettingsDialog / MyPanelWindow

数据模型（PRD §8）：lastOpenedFiles 固定 200 个位置，索引 = row * 4 + column。
单列模式只显示第 1 列（索引 0, 4, 8, ...）；四列模式显示全部。调整每列数量
只改变「可见行数」，被隐藏行的数据必须原样保留。
"""

import configparser
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Callable

from PySide6.QtCore import (
    Qt,
    QLibraryInfo,
    QLocale,
    QProcess,
    QTranslator,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices, QFontMetrics, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "myPanel"
APP_VERSION = "1.0"
APP_AUTHOR = "shylock"

COLUMNS = 4
SLOT_CAPACITY = 200  # 4 列 × 每列最多 50
DEFAULT_ITEM_COUNT = 9
MAX_ITEM_COUNT = 50

# 布局尺寸（PRD §5.1，单位 logical px）
SLOT_NAME_WIDTH = 62   # 名称固定区域宽度：超长在区域内截断，按钮位置不随之偏移
SLOT_MIN_WIDTH = 173   # 边距12 + 图标32 + 间距12 + 名称62 + 打开30 + 清除22 + 余量
SLOT_MIN_HEIGHT = 40   # 图标 / 名称 / 按钮全部同一行后的自然高度
GRID_H_SPACING = 0
GRID_V_SPACING = 6
GRID_MARGIN = 10  # 槽位网格上下留白
GRID_SIDE_MARGIN = 20  # 槽位网格左右留白
# 槽位网格之外的高度：顶部栏 + 外接卷区 + 窗口外边距（实测）
WINDOW_CHROME_HEIGHT = 132

CONFIG_DIR = pathlib.Path(
    os.environ.get("XDG_CONFIG_HOME", str(pathlib.Path.home() / ".config"))
) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "myPanel.json"


# ---------------------------------------------------------------------------
# 配置层
# ---------------------------------------------------------------------------

_LAST_LOAD_WARNING: str | None = None


def _default_config() -> dict:
    return {
        "lastOpenedFiles": [""] * SLOT_CAPACITY,
        "preferences": {"theme": "default", "language": "system"},
        "lastModifiedTimes": [0] * SLOT_CAPACITY,
        "itemCount": DEFAULT_ITEM_COUNT,
        "isLargePanel": False,
    }


def _infer_item_count(n: int) -> tuple[int, bool]:
    """按配置长度推断 itemCount / isLargePanel（PRD §4.7）。

    - 长度为 4 的倍数且每列数量在 1–50 → 四列大面板
    - 1–50 个条目 → 单列
    - 无法可靠推断 → 默认 9 / 单列
    """
    if n > 0 and n % COLUMNS == 0 and 1 <= n // COLUMNS <= MAX_ITEM_COUNT:
        return n // COLUMNS, True
    if 1 <= n <= MAX_ITEM_COUNT:
        return n, False
    return DEFAULT_ITEM_COUNT, False


def _normalize_slots(seq, capacity: int = SLOT_CAPACITY) -> list:
    """把任意序列规整为长度恰为 capacity 的字符串列表，不丢非空条目。"""
    out = [""] * capacity
    if isinstance(seq, list):
        for i, v in enumerate(seq[:capacity]):
            out[i] = v if isinstance(v, str) else ("" if v is None else str(v))
    return out


def _normalize_mtimes(seq, capacity: int = SLOT_CAPACITY) -> list:
    out = [0] * capacity
    if isinstance(seq, list):
        for i, v in enumerate(seq[:capacity]):
            out[i] = int(v) if isinstance(v, (int, float)) else 0
    return out


def migrate_config(raw) -> tuple[dict, str | None]:
    """把旧格式 / 不完整配置迁移为当前结构。返回 (config, warning)。"""
    cfg = _default_config()

    # 旧格式：顶层 [String] 数组
    if isinstance(raw, list):
        items = [x if isinstance(x, str) else "" for x in raw]
        n = len(items)
        if n > SLOT_CAPACITY:
            warning = (
                f"旧配置包含 {n} 个条目，超出 {SLOT_CAPACITY} 个槽位容量，"
                f"仅前 {SLOT_CAPACITY} 个被导入。"
            )
        else:
            warning = None
        item_count, is_large = _infer_item_count(min(n, SLOT_CAPACITY))
        cfg["itemCount"], cfg["isLargePanel"] = item_count, is_large
        if is_large:
            # 行优先整体保留
            for i in range(min(n, SLOT_CAPACITY)):
                cfg["lastOpenedFiles"][i] = items[i]
        else:
            # 旧版单列：第 i 项落到第一列，即索引 i * 4
            for i in range(min(n, MAX_ITEM_COUNT)):
                cfg["lastOpenedFiles"][i * COLUMNS] = items[i]
        return cfg, warning

    if not isinstance(raw, dict):
        return cfg, "配置内容不是对象或数组，已使用默认配置。"

    cfg["lastOpenedFiles"] = _normalize_slots(raw.get("lastOpenedFiles"))
    cfg["lastModifiedTimes"] = _normalize_mtimes(raw.get("lastModifiedTimes"))

    prefs = raw.get("preferences")
    if isinstance(prefs, dict):
        theme = prefs.get("theme")
        lang = prefs.get("language")
        if theme in ("default", "light", "dark"):
            cfg["preferences"]["theme"] = theme
        if isinstance(lang, str) and lang:
            cfg["preferences"]["language"] = lang

    ic = raw.get("itemCount")
    if isinstance(ic, int) and not isinstance(ic, bool) and 1 <= ic <= MAX_ITEM_COUNT:
        cfg["itemCount"] = ic
    else:
        # 缺失或越界：按配置长度推断（PRD §4.7）
        cfg["itemCount"], _ = _infer_item_count(len(cfg["lastOpenedFiles"]))

    il = raw.get("isLargePanel")
    cfg["isLargePanel"] = bool(il) if isinstance(il, bool) else False

    return cfg, None


def load_config() -> tuple[dict, str | None]:
    """加载配置。返回 (config, warning)；warning 非空表示发生了回退。"""
    global _LAST_LOAD_WARNING
    _LAST_LOAD_WARNING = None

    if not CONFIG_FILE.exists():
        return _default_config(), None

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        # PRD §4.7：不可静默覆盖损坏文件 —— 先备份再回退默认值
        backup = CONFIG_FILE.with_suffix(".json.corrupt")
        try:
            CONFIG_FILE.replace(backup)
            msg = f"配置文件损坏（{e}），已备份到 {backup.name} 并恢复默认配置。"
        except OSError:
            msg = f"配置文件损坏（{e}），已恢复默认配置。"
        _LAST_LOAD_WARNING = msg
        return _default_config(), msg
    except OSError as e:
        msg = f"无法读取配置文件：{e}"
        _LAST_LOAD_WARNING = msg
        return _default_config(), msg

    cfg, warning = migrate_config(raw)
    _LAST_LOAD_WARNING = warning
    return cfg, warning


def save_config(config: dict) -> tuple[bool, str]:
    """安全写入：先写临时文件再原子替换（PRD §4.7）。"""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        tmp.replace(CONFIG_FILE)
        return True, ""
    except OSError as e:
        return False, str(e)


def import_config(path: str) -> tuple[dict | None, str]:
    """从用户指定路径导入配置（PRD §4.7）。不修改、不删除来源文件。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"导入失败：{e}"

    cfg, warning = migrate_config(raw)
    return cfg, warning or ""


# ---------------------------------------------------------------------------
# 桌面服务层
# ---------------------------------------------------------------------------


def _desktop_files_dirs() -> list[pathlib.Path]:
    data_home = pathlib.Path(
        os.environ.get("XDG_DATA_HOME", str(pathlib.Path.home() / ".local/share"))
    )
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    dirs = [data_home / "applications"]
    for d in data_dirs.split(":"):
        if d.strip():
            dirs.append(pathlib.Path(d.strip()) / "applications")
    return [d for d in dirs if d.is_dir()]


def read_desktop_entry(path: str) -> dict | None:
    """解析 .desktop 文件，返回 [Desktop Entry] 段；无效则返回 None。"""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    # .desktop 规范区分大小写（Exec / Type / NoDisplay / Name[zh_CN]），
    # configparser 默认会把键名转小写，必须关掉。
    parser.optionxform = str
    try:
        # 用 utf-8 读取，避免本地编码差异导致 UnicodeDecodeError
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            parser.read_file(f)
    except (OSError, configparser.Error):
        return None
    if not parser.has_section("Desktop Entry"):
        return None
    return dict(parser.items("Desktop Entry"))


def _localized(entry: dict, key: str) -> str:
    """取本地化字段，优先 Name[zh_CN] 之类。"""
    for suffix in ("zh_CN", "zh", "en"):
        v = entry.get(f"{key}[{suffix}]")
        if v:
            return v
    return entry.get(key, "") or ""


def is_launchable_desktop(path: str) -> bool:
    """判断是否为可启动的应用启动项（PRD §4.2/§4.3）。"""
    if not path.endswith(".desktop"):
        return False
    entry = read_desktop_entry(path)
    if entry is None:
        return False
    if entry.get("Type", "Application") != "Application":
        return False
    return bool(entry.get("Exec"))


def list_installed_apps() -> list[tuple[str, str]]:
    """枚举已安装应用，返回按名称排序的 (名称, .desktop 路径)。"""
    seen: dict[str, str] = {}  # desktop id -> path（先出现的目录优先）
    for d in _desktop_files_dirs():
        for p in sorted(d.glob("*.desktop")):
            if p.name in seen:
                continue
            entry = read_desktop_entry(str(p))
            if entry is None:
                continue
            if entry.get("Type", "Application") != "Application":
                continue
            if entry.get("NoDisplay", "").strip().lower() == "true":
                continue
            if not entry.get("Exec"):
                continue
            seen[p.name] = str(p)
    apps = [(_localized(read_desktop_entry(p) or {}, "Name") or pathlib.Path(p).stem, p)
            for p in seen.values()]
    apps.sort(key=lambda t: t[0].lower())
    return apps


class DesktopServices:
    """文件打开、图标解析、外接卷管理。不依赖任何 UI 控件。"""

    # ---- 图标 (PRD §4.4) ----

    @staticmethod
    def icon_for(path: str, style: QStyle, size: int = 32) -> QIcon:
        if not path:
            return style.standardIcon(QStyle.SP_FileDialogNewFolder)

        p = pathlib.Path(path)
        if not p.exists():
            return style.standardIcon(QStyle.SP_MessageBoxWarning)

        if path.endswith(".desktop"):
            entry = read_desktop_entry(path)
            if entry:
                name = entry.get("Icon", "")
                if name:
                    icon = QIcon.fromTheme(name)
                    if not icon.isNull():
                        return icon
            # Qt6 没有 SP_ApplicationIcon，用桌面图标作为 .desktop 的通用回退
            return style.standardIcon(QStyle.SP_DesktopIcon)

        if p.is_dir():
            icon = QIcon.fromTheme("folder")
            return icon if not icon.isNull() else style.standardIcon(QStyle.SP_DirIcon)

        icon = QIcon.fromTheme("text-x-generic")
        return icon if not icon.isNull() else style.standardIcon(QStyle.SP_FileIcon)

    # ---- 名称 (PRD §4.4) ----

    @staticmethod
    def display_name(path: str) -> str:
        if not path:
            return ""
        if path.endswith(".desktop"):
            entry = read_desktop_entry(path)
            if entry:
                name = _localized(entry, "Name")
                if name:
                    return name
        p = pathlib.Path(path)
        return p.name or path

    # ---- 打开目标 (PRD §4.3) ----

    @staticmethod
    def open_target(path: str) -> tuple[bool, str]:
        """打开文件/文件夹/应用。返回 (是否成功, 错误信息)。

        使用系统桌面机制，不拼接 shell 命令。
        """
        if not path:
            return False, "该槽位尚未设置目标。"

        p = pathlib.Path(path)
        if not p.exists():
            return False, f"目标不存在或不可访问：\n{path}"

        if path.endswith(".desktop"):
            if not is_launchable_desktop(path):
                return False, f"无效的应用启动项（缺少可执行的 Exec 字段）：\n{path}"
            # 系统桌面应用信息机制：优先 gio launch，回退 gtk-launch
            ok, _out, _err = _run(["gio", "launch", path])
            if ok:
                return True, ""
            ok, _out, err = _run(["gtk-launch", p.stem])
            if ok:
                return True, ""
            return False, f"启动应用失败：{err or '未知错误'}"

        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.resolve()))):
            return True, ""
        return False, f"系统默认应用无法打开：\n{path}"


def _run(args: list[str], timeout: int = 15) -> tuple[bool, str, str]:
    """执行外部命令（不使用 shell），返回 (成功, stdout, stderr)。"""
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
        return r.returncode == 0, r.stdout, r.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "", str(e)


# ---- 外接卷 (PRD §4.5) ----


def list_removable_volumes() -> list[dict]:
    """列出「可移除且已挂载」的卷。使用 lsblk（util-linux，系统接口）。"""
    ok, out, _err = _run(
        ["lsblk", "-J", "-o", "NAME,LABEL,MOUNTPOINT,RM,TYPE,SIZE"], timeout=5
    )
    if not ok or not out.strip():
        return []

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []

    found: list[dict] = []

    def walk(node: dict, parent_removable: bool) -> None:
        rm_raw = node.get("rm")
        removable = parent_removable or rm_raw in (True, "1", 1)
        mountpoint = node.get("mountpoint")
        if removable and mountpoint:
            found.append(
                {
                    "device": f"/dev/{node.get('name', '')}",
                    "label": node.get("label") or node.get("name") or "",
                    "mountpoint": mountpoint,
                    "size": node.get("size") or "",
                }
            )
        for child in node.get("children") or []:
            walk(child, removable)

    for dev in data.get("blockdevices") or []:
        walk(dev, False)

    return found


# ---------------------------------------------------------------------------
# 界面层
# ---------------------------------------------------------------------------


class TargetChooserDialog(QDialog):
    """选择目标：文件 / 文件夹 / 已安装应用（PRD §4.2）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("选择目标"))
        self.chosen: str | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self.tr("请选择要放入槽位的内容：")))

        btn_file = QPushButton(self.tr("选择文件…"))
        btn_dir = QPushButton(self.tr("选择文件夹…"))
        btn_app = QPushButton(self.tr("从已安装应用中选择…"))
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
            self.tr("选择文件"),
            str(pathlib.Path.home()),
            self.tr("所有文件 (*)"),
        )
        if path:
            self.chosen = path
            self.accept()

    def _pick_dir(self):
        path = QFileDialog.getExistingDirectory(
            self, self.tr("选择文件夹"), str(pathlib.Path.home())
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
        self.setWindowTitle(self.tr("选择应用"))
        self.resize(520, 460)
        self.chosen: str | None = None
        self._apps = list_installed_apps()

        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText(self.tr("搜索应用…"))
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._accept())
        layout.addWidget(self.list)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate()
        self.search.setFocus()

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
        # 名称占用固定宽度的区域：超长在区域内截断，右侧按钮位置不随文本长度偏移
        self.name_label.setFixedWidth(SLOT_NAME_WIDTH)
        self.name_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.name_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.open_btn = QPushButton()
        self.open_btn.setFixedSize(30, 24)
        self.open_btn.clicked.connect(self._on_open_clicked)
        self.clear_btn = QPushButton("×")
        self.clear_btn.setFixedSize(22, 22)
        self.clear_btn.setToolTip(self.tr("清除该槽位"))
        self.clear_btn.clicked.connect(self.clear_requested.emit)

        # 图标 / 名称 / 打开 / 清除 紧凑地靠左排列（名称紧贴图标，按钮紧跟名称）
        layout.addWidget(self.icon_label, 0, Qt.AlignVCenter)
        layout.addWidget(self.name_label, 0, Qt.AlignVCenter)
        layout.addWidget(self.open_btn, 0)
        layout.addWidget(self.clear_btn, 0)
        layout.addStretch()

        self.set_target("")

    # -- 状态 --

    def set_target(self, path: str):
        self.target_path = path or ""

        if not self.target_path:
            self._full_name = self.tr("添加")
            self.name_label.setToolTip(self.tr("点击「+」选择文件、文件夹或应用"))
            self.open_btn.setText("+")
            self.open_btn.setToolTip(self.tr("添加目标"))
        else:
            self._full_name = DesktopServices.display_name(self.target_path)
            tip = f"{self._full_name}\n{self.target_path}"
            if not pathlib.Path(self.target_path).exists():
                tip += "\n" + self.tr("⚠ 目标不存在或不可访问，请重新定位或清除")
            self.name_label.setToolTip(tip)
            self.open_btn.setText(self.tr("开"))
            self.open_btn.setToolTip(self.tr("打开：%s") % self.target_path)

        self.name_label.set_full_text(getattr(self, "_full_name", ""))
        self._refresh_icon()

    def _refresh_icon(self):
        style = self.style()
        if not self.target_path:
            self.icon_label.clear()
            self.icon_label.setText("+")
            return
        icon = DesktopServices.icon_for(self.target_path, style, self.ICON_SIZE)
        self.icon_label.setText("")
        self.icon_label.setPixmap(icon.pixmap(self.ICON_SIZE, self.ICON_SIZE))

    def _on_open_clicked(self):
        if self.target_path:
            self.open_requested.emit()
        else:
            self.add_requested.emit()


class VolumeBar(QWidget):
    """外接卷区域（PRD §4.5）：刷新、单项弹出、全部弹出。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 4, 16, 4)
        outer.setSpacing(2)

        header = QHBoxLayout()
        self.title = QLabel(self.tr("外接卷"))
        header.addWidget(self.title)
        header.addStretch()
        self.btn_refresh = QPushButton(self.tr("刷新"))
        self.btn_refresh.clicked.connect(self.refresh)
        header.addWidget(self.btn_refresh)
        self.btn_eject_all = QPushButton(self.tr("全部弹出"))
        self.btn_eject_all.clicked.connect(self.eject_all)
        header.addWidget(self.btn_eject_all)
        outer.addLayout(header)

        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(2)
        outer.addLayout(self.rows_box)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        self.refresh()

    def refresh(self):
        while self.rows_box.count():
            item = self.rows_box.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        volumes = list_removable_volumes()
        self._volume_count = len(volumes)

        if not volumes:
            empty = QLabel(self.tr("无外接设备"))
            empty.setEnabled(False)
            self.rows_box.addWidget(empty)
            self.btn_eject_all.setEnabled(False)
            return

        self.btn_eject_all.setEnabled(not self._busy)
        for vol in volumes:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            label = QLabel(f"{vol['label']}  ({vol['size']})  {vol['mountpoint']}")
            label.setToolTip(vol["device"])
            h.addWidget(label, 1)
            btn = QPushButton(self.tr("弹出"))
            btn.setFixedWidth(72)
            btn.clicked.connect(lambda _c, v=vol, b=btn: self._eject_one(v, b))
            h.addWidget(btn)
            self.rows_box.addWidget(row)

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.btn_refresh.setEnabled(not busy)
        self.btn_eject_all.setEnabled(not busy and self._volume_count > 0)

    def _eject_one(self, vol: dict, button: QPushButton | None):
        """弹出单个卷：先卸载，再尝试断电。失败不抛异常。"""
        if self._busy:
            return
        self._set_busy(True)
        label = vol["label"]
        if button:
            button.setEnabled(False)
        self.status.setText(self.tr("正在弹出 %s …") % label)

        self._eject_sequence(
            vol,
            lambda ok, msg: self._finish_one(label, ok, msg, button),
        )

    def _finish_one(self, label, ok, msg, button):
        self._set_busy(False)
        if button:
            button.setEnabled(True)
        if ok:
            self.status.setText(self.tr("已安全弹出：%s") % label)
            self.refresh()
        else:
            self.status.setText(self.tr("弹出失败：%s — %s") % (label, msg))
            QMessageBox.warning(self, self.tr("弹出失败"), f"{label}\n{msg}")

    def _eject_sequence(self, vol: dict, done: Callable[[bool, str], None]):
        """`udisksctl unmount` 之后尝试 `udisksctl power-off`（异步、串行）。"""
        device = vol["device"]

        def on_poweroff(ok: bool, err: str):
            # power-off 失败不算致命（部分设备不支持），卸载成功即可
            done(True, "")

        def on_unmount(ok: bool, err: str):
            if not ok:
                done(False, err or self.tr("卸载失败"))
                return
            run_async(
                ["udisksctl", "power-off", "-b", device],
                self,
                on_poweroff,
            )

        run_async(["udisksctl", "unmount", "-b", device], self, on_unmount)

    def eject_all(self):
        if self._busy:
            return
        volumes = list_removable_volumes()
        if not volumes:
            self.status.setText(self.tr("没有可弹出的卷。"))
            return

        self._set_busy(True)
        results: list[tuple[str, bool, str]] = []
        total = len(volumes)

        def step():
            i = len(results)
            if i >= total:
                self._set_busy(False)
                failed = [r for r in results if not r[1]]
                self.status.setText(
                    self.tr("全部弹出完成：成功 %d，失败 %d")
                    % (total - len(failed), len(failed))
                )
                if failed:
                    detail = "\n".join(f"• {n}：{e}" for n, _ok, e in failed)
                    QMessageBox.warning(
                        self, self.tr("部分卷弹出失败"), detail
                    )
                self.refresh()
                return

            vol = volumes[i]
            self.status.setText(
                self.tr("正在弹出 %d/%d：%s …")
                % (i + 1, total, vol["label"])
            )

            def on_done(ok: bool, msg: str):
                results.append((vol["label"], ok, msg))
                step()

            self._eject_sequence(vol, on_done)

        step()


def run_async(args: list[str], parent, on_done: Callable[[bool, str], None]):
    """异步执行外部命令，避免阻塞主界面（PRD §4.5）。"""
    proc = QProcess(parent)
    proc.setProgram(args[0])
    proc.setArguments(args[1:])
    captured = {"err": "", "out": ""}
    fired = {"done": False}

    def finish(ok: bool, msg: str):
        # errorOccurred 与 finished 都可能触发，保证回调只执行一次
        if fired["done"]:
            return
        fired["done"] = True
        on_done(ok, msg)

    def read_output():
        captured["out"] += bytes(proc.readAllStandardOutput()).decode(
            "utf-8", "replace"
        )
        captured["err"] += bytes(proc.readAllStandardError()).decode("utf-8", "replace")

    proc.readyReadStandardOutput.connect(read_output)
    proc.readyReadStandardError.connect(read_output)

    def finished(code, _status):
        read_output()
        ok = code == 0
        msg = "" if ok else (captured["err"].strip() or f"退出码 {code}")
        proc.deleteLater()
        finish(ok, msg)

    proc.finished.connect(finished)
    proc.errorOccurred.connect(
        lambda _e: finish(False, f"无法执行 {args[0]}（请确认已安装 udisks2）")
    )
    proc.start()


class SettingsDialog(QDialog):
    """设置：主题、语言、全局重置（PRD §4.6）。"""

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("设置"))
        self.config = config
        self.reset_requested = False

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(self.tr("主题")))
        self.theme_combo = QComboBox()
        for value, label in (
            ("default", self.tr("跟随系统")),
            ("light", self.tr("浅色")),
            ("dark", self.tr("深色")),
        ):
            self.theme_combo.addItem(label, value)
        current_theme = config.get("preferences", {}).get("theme", "default")
        idx = self.theme_combo.findData(current_theme)
        self.theme_combo.setCurrentIndex(idx if idx >= 0 else 0)
        layout.addWidget(self.theme_combo)

        layout.addWidget(QLabel(self.tr("语言")))
        self.lang_combo = QComboBox()
        for value, label in (
            ("system", self.tr("跟随系统")),
            ("zh_CN", "简体中文"),
            ("en", "English"),
        ):
            self.lang_combo.addItem(label, value)
        current_lang = config.get("preferences", {}).get("language", "system")
        idx = self.lang_combo.findData(current_lang)
        self.lang_combo.setCurrentIndex(idx if idx >= 0 else 0)
        layout.addWidget(self.lang_combo)

        hint = QLabel(self.tr("语言变更在重启后对界面文本生效"))
        hint.setEnabled(False)
        layout.addWidget(hint)

        layout.addSpacing(8)
        btn_reset = QPushButton(self.tr("全局重置…"))
        btn_reset.clicked.connect(self._on_reset)
        layout.addWidget(btn_reset)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_reset(self):
        answer = QMessageBox.question(
            self,
            self.tr("确认全局重置"),
            self.tr(
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

    def __init__(self):
        super().__init__()
        self.config, self._load_warning = load_config()
        self._fix_invariants()

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(350, 450)
        self._slot_widgets: dict[int, SlotWidget] = {}

        self._setup_ui()
        self._relayout()

        if self._load_warning:
            QMessageBox.warning(self, self.tr("配置提示"), self._load_warning)

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

    def _last_opened(self) -> list:
        return _normalize_slots(self.config.get("lastOpenedFiles"))

    def _mtimes(self) -> list:
        return _normalize_mtimes(self.config.get("lastModifiedTimes"))

    def save(self):
        """保存配置。

        关键点（PRD §4.1 / §8）：只更新**当前可见**槽位，
        被隐藏的行/列数据原样保留，绝不清空。
        """
        lof = self._last_opened()
        mtimes = self._mtimes()
        now = int(time.time())

        for cfg_idx, widget in self._slot_widgets.items():
            if not (0 <= cfg_idx < SLOT_CAPACITY):
                continue
            path = widget.target_path
            if lof[cfg_idx] != path:
                lof[cfg_idx] = path
                mtimes[cfg_idx] = now if path else 0

        self.config["lastOpenedFiles"] = lof
        self.config["lastModifiedTimes"] = mtimes
        self.config["itemCount"] = self.item_count
        self.config["isLargePanel"] = self.is_large

        ok, err = save_config(self.config)
        if not ok:
            QMessageBox.warning(
                self, self.tr("保存失败"), self.tr("无法写入配置文件：\n%s") % err
            )

    # ---- 界面构建 ----

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 8, 0, 8)
        root.setSpacing(6)

        root.addLayout(self._build_top_bar())

        self.volume_bar = VolumeBar()
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

    def _build_top_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(16, 0, 16, 0)

        # 左：数量 减号 / 数值 / 加号 + 大面板开关（PRD §4.1 / §5.1）
        self.btn_dec = QPushButton("−")
        self.btn_dec.setFixedSize(28, 28)
        self.btn_dec.clicked.connect(lambda: self._change_count(-1))
        self.count_label = QLabel(str(self.item_count))
        self.count_label.setAlignment(Qt.AlignCenter)
        self.count_label.setMinimumWidth(28)
        self.btn_inc = QPushButton("+")
        self.btn_inc.setFixedSize(28, 28)
        self.btn_inc.clicked.connect(lambda: self._change_count(+1))

        bar.addWidget(self.btn_dec)
        bar.addWidget(self.count_label)
        bar.addWidget(self.btn_inc)

        self.large_toggle = QCheckBox(self.tr("大面板 (4列)"))
        self.large_toggle.setChecked(self.is_large)
        self.large_toggle.toggled.connect(self._on_large_toggled)
        bar.addWidget(self.large_toggle)

        bar.addStretch()

        title = QLabel(APP_NAME)
        f = title.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 3)
        title.setFont(f)
        bar.addWidget(title)

        bar.addStretch()

        meta = QLabel(f"v{APP_VERSION}")
        meta.setEnabled(False)
        meta.setToolTip(self.tr("作者：%s") % APP_AUTHOR)
        bar.addWidget(meta)

        btn_settings = QPushButton(self.tr("设置"))
        btn_settings.clicked.connect(self._open_settings)
        bar.addWidget(btn_settings)

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

            if 0 <= cfg_idx < SLOT_CAPACITY:
                slot.set_target(lof[cfg_idx])

            self.slots_layout.addWidget(slot, row, col)
            self._slot_widgets[cfg_idx] = slot

        for c in range(cols):
            self.slots_layout.setColumnStretch(c, 1)
        for r in range(rows):
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
            self.resize(width, min(height, self._max_window_height()))

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

    def _on_open(self, cfg_idx: int):
        slot = self._slot_widgets.get(cfg_idx)
        if slot is None:
            return
        ok, err = DesktopServices.open_target(slot.target_path)
        if not ok:
            # PRD §4.1：保留槽位内容，让用户可修复或清除
            QMessageBox.warning(self, self.tr("无法打开"), err)

    def _on_clear(self, cfg_idx: int):
        slot = self._slot_widgets.get(cfg_idx)
        if slot is None:
            return
        slot.set_target("")
        self.save()

    # ---- 设置 ----

    def _open_settings(self):
        dlg = SettingsDialog(self.config, self)
        if dlg.exec() != QDialog.Accepted:
            return

        if dlg.reset_requested:
            self.config = _default_config()
            self._fix_invariants()
            self.large_toggle.setChecked(self.is_large)
            self.count_label.setText(str(self.item_count))
            self._update_count_buttons()
            self._relayout()
            self.save()
            apply_theme(self.config)
            apply_language(QApplication.instance(), self.config)
            return

        self.config.setdefault("preferences", {})
        self.config["preferences"]["theme"] = dlg.selected_theme()
        self.config["preferences"]["language"] = dlg.selected_language()
        self.save()
        apply_theme(self.config)
        apply_language(QApplication.instance(), self.config)

    def closeEvent(self, event):
        self.save()
        super().closeEvent(event)


def apply_theme(config: dict):
    """应用主题（PRD §4.6）：default = 跟随系统。"""
    scheme = config.get("preferences", {}).get("theme", "default")
    hints = QApplication.instance().styleHints()
    mapping = {
        "dark": Qt.ColorScheme.Dark,
        "light": Qt.ColorScheme.Light,
    }
    hints.setColorScheme(mapping.get(scheme, Qt.ColorScheme.Unknown))


_qt_translators: list[QTranslator] = []


def apply_language(app: QApplication, config: dict):
    """应用语言设置（PRD §4.6）。

    加载 Qt 自带翻译（文件选择器、标准对话框按钮等）。应用自身字符串
    的翻译需要另外提供 .ts/.qm，界面文本变更在重启后生效。
    """
    lang = config.get("preferences", {}).get("language", "system")
    locale = QLocale.system() if lang == "system" else QLocale(lang)
    QLocale.setDefault(locale)

    for tr in _qt_translators:
        app.removeTranslator(tr)
    _qt_translators.clear()

    path = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
    candidates = [locale.name(), locale.name().split("_")[0]]
    for name in ("qtbase", "qt"):
        tr = QTranslator(app)
        for cand in candidates:
            if tr.load(f"{name}_{cand}", path):
                app.installTranslator(tr)
                _qt_translators.append(tr)
                break


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_AUTHOR)

    window = MyPanelWindow()
    apply_theme(window.config)
    apply_language(app, window.config)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
