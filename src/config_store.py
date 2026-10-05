"""Validated migrations and transactional user configuration storage."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import pathlib
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

COLUMNS = 4
SLOT_CAPACITY = 200
DEFAULT_ITEM_COUNT = 9
MAX_ITEM_COUNT = 50
SCHEMA_VERSION = 2
MAX_CONFIG_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Notice:
    message: str
    args: tuple = ()

    def text(self):
        from localization import _
        return _(self.message) % self.args if self.args else _(self.message)


class ConfigError(Exception):
    def __init__(self, message, *args):
        self.notice = Notice(message, args)
        super().__init__(message)

    def __str__(self):
        return self.notice.text()


def default_config():
    return {
        "schemaVersion": SCHEMA_VERSION,
        "lastOpenedFiles": [""] * SLOT_CAPACITY,
        "lastModifiedTimes": [0] * SLOT_CAPACITY,
        "preferences": {"theme": "default", "language": "system"},
        "itemCount": DEFAULT_ITEM_COUNT,
        "isLargePanel": False,
    }


def config_path():
    value = os.environ.get("XDG_CONFIG_HOME", "")
    base = pathlib.Path(value) if value and pathlib.Path(value).is_absolute() else pathlib.Path.home() / ".config"
    return base / "myPanel" / "myPanel.json"


def normalize_language(value):
    aliases = {"zh-Hans": "zh_CN", "zh-CN": "zh_CN", "zh": "zh_CN", "en-US": "en", "en_US": "en"}
    return aliases.get(value, value) if value in {"system", "zh_CN", "en", *aliases} else "system"


def migrate_config(raw):
    """Infer BEFORE padding; reject overflow rather than discard any target."""
    cfg = default_config()
    notices = []
    if isinstance(raw, list):
        original, count, large, version = raw, None, False, None
        metadata = []
    elif isinstance(raw, dict):
        version = raw.get("schemaVersion")
        if version is not None and (type(version) is not int or version not in (1, SCHEMA_VERSION)):
            raise ConfigError("不支持配置版本：%s", version)
        original = raw.get("lastOpenedFiles", [])
        count = raw.get("itemCount")
        large = raw.get("isLargePanel", False)
        if type(large) is not bool:
            raise ConfigError("isLargePanel 必须是布尔值。")
        metadata = raw.get("lastModifiedTimes", [])
        prefs = raw.get("preferences", {})
        if not isinstance(prefs, dict):
            raise ConfigError("preferences 必须是对象。")
        if prefs.get("theme", "default") not in ("default", "light", "dark"):
            notices.append(Notice("无效主题已恢复为跟随系统。"))
        else:
            cfg["preferences"]["theme"] = prefs.get("theme", "default")
        lang = prefs.get("language", "system")
        if not isinstance(lang, str):
            raise ConfigError("language 必须是字符串。")
        cfg["preferences"]["language"] = normalize_language(lang)
    else:
        raise ConfigError("配置内容必须是对象或路径数组。")

    if not isinstance(original, list):
        raise ConfigError("lastOpenedFiles 必须是路径数组。")
    n = len(original)
    if n > SLOT_CAPACITY:
        raise ConfigError("配置包含 %d 个条目，超过 %d 个槽位；已拒绝读取，原文件保持不变。", n, SLOT_CAPACITY)
    # 旧版本会把空槽位序列化为 null；按空字符串归一化，而不是整个
    # 拒绝配置（用户会失去全部已配置目标）。只对真正错误的条目报错。
    sanitized = []
    coerced_nulls = 0
    for p in original:
        if p is None:
            coerced_nulls += 1
            sanitized.append("")
        elif isinstance(p, str):
            if "\x00" in p:
                raise ConfigError("槽位必须是空字符串或不含 NUL 的绝对路径。")
            if p and not pathlib.PurePosixPath(p).is_absolute():
                raise ConfigError("槽位必须是空字符串或不含 NUL 的绝对路径。")
            sanitized.append(p)
        else:
            raise ConfigError("槽位必须是空字符串或不含 NUL 的绝对路径。")
    if coerced_nulls:
        notices.append(Notice("已将 %d 个空槽位从 null 归一化为空字符串。", (coerced_nulls,)))
    original = sanitized
    if count is not None and (type(count) is not int or not 1 <= count <= MAX_ITEM_COUNT):
        notices.append(Notice("无效的每列数量已按原始配置长度重新推断。"))
        count = None

    single = False
    if version == SCHEMA_VERSION:
        if n != SLOT_CAPACITY or count is None:
            raise ConfigError("版本 2 配置必须包含 200 个槽位和有效的每列数量。")
    elif count is not None:
        single = n == count and n != SLOT_CAPACITY
    else:
        if n and n % COLUMNS == 0:
            count = n // COLUMNS
        elif 1 <= n <= MAX_ITEM_COUNT:
            count, single = n, True
        else:
            count = DEFAULT_ITEM_COUNT
            if n:
                notices.append(Notice("无法可靠推断旧配置布局；全部 %d 项按原始顺序保留，可增大数量并切换大面板查看。", (n,)))

    cfg["itemCount"] = count
    cfg["isLargePanel"] = large if large is not None else False
    if not isinstance(metadata, list):
        metadata = []
        notices.append(Notice("无效的修改时间元数据已忽略。"))
    bad_times = False
    for i, path in enumerate(original):
        target = i * COLUMNS if single else i
        cfg["lastOpenedFiles"][target] = path
        value = metadata[i] if i < len(metadata) else 0
        if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)) or value < 0:
            bad_times = True
            value = 0
        cfg["lastModifiedTimes"][target] = int(value)
    if bad_times:
        notices.append(Notice("无效的修改时间元数据已忽略。"))
    return cfg, notices


def read_config(path):
    """Bounded reads; do not accept non-standard NaN/Infinity JSON values."""
    def invalid_constant(value):
        raise ValueError(value)
    try:
        with open(path, "rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise ConfigError("配置文件超过 2 MiB 限制，原文件保持不变。")
        raw = json.loads(data.decode("utf-8"), parse_constant=invalid_constant)
        cfg, notices = migrate_config(raw)
        return cfg, notices, hashlib.sha256(data).hexdigest()
    except ConfigError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise ConfigError("无法读取配置：%s", str(error)) from error


@dataclass
class LoadResult:
    config: dict
    notices: list[Notice] = field(default_factory=list)
    protected: bool = False
    backup: pathlib.Path | None = None


class ConfigStore:
    def __init__(self, path=None):
        self.path = pathlib.Path(path) if path is not None else config_path()
        self.protected = False
        self.expected = None

    def _fingerprint(self):
        try:
            info = self.path.stat()
            if info.st_size > MAX_CONFIG_BYTES:
                return ("oversized", info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
            digest = hashlib.sha256()
            with self.path.open("rb") as stream:
                data = stream.read(MAX_CONFIG_BYTES + 1)
                if len(data) > MAX_CONFIG_BYTES:
                    info = self.path.stat()
                    return ("oversized", info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino)
                digest.update(data)
            return digest.hexdigest()
        except FileNotFoundError:
            return None

    def load(self):
        try:
            self.expected = self._fingerprint()
            if self.expected is None:
                self.protected = False
                return LoadResult(default_config())
            cfg, notices, fingerprint = read_config(self.path)
            self.expected = fingerprint
            self.protected = False
            return LoadResult(cfg, notices)
        except (ConfigError, OSError) as error:
            self.protected = True
            notices = [error.notice if isinstance(error, ConfigError) else Notice("无法读取配置：%s", (str(error),))]
            backup = None
            try:
                if self.path.stat().st_size <= MAX_CONFIG_BYTES:
                    backup = self._backup()
                    notices.append(Notice("原配置已备份到：%s", (str(backup),)))
            except OSError as backup_error:
                notices.append(Notice("备份失败：%s；自动保存已禁用。", (str(backup_error),)))
            notices.append(Notice("当前使用临时空配置；原文件不会自动覆盖。请导入有效配置，或在设置中明确恢复保存。"))
            return LoadResult(default_config(), notices, True, backup)

    def _backup(self):
        backup = self.path.with_name(f"{self.path.name}.backup-{time.time_ns()}-{uuid.uuid4().hex[:8]}")
        with self.path.open("rb") as source, backup.open("xb") as target:
            try:
                os.fchmod(target.fileno(), 0o600)
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            except BaseException:
                target.close()
                backup.unlink(missing_ok=True)
                raise
        return backup

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def save(self, config, *, backup=False, recover=False):
        if self.protected and not recover:
            raise ConfigError("原配置受保护，未写入。请在设置中导入或明确恢复保存。")
        normalized, _notices = migrate_config(config)
        data = (json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        if len(data) > MAX_CONFIG_BYTES:
            raise ConfigError("配置文件超过 2 MiB 限制，原文件保持不变。")
        tmp_name = None
        saved_backup = None
        try:
            with self._locked():
                if self._fingerprint() != self.expected:
                    raise ConfigError("配置已被其他进程修改，未覆盖。请重新启动以加载最新配置。")
                if (backup or recover) and self.path.exists():
                    saved_backup = self._backup()
                with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".myPanel-", delete=False) as stream:
                    tmp_name = stream.name
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp_name, self.path)
                tmp_name = None
                self.expected = hashlib.sha256(data).hexdigest()
                self.protected = False
                # Directory sync is best effort after a successful atomic replacement.
                try:
                    directory = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                except OSError:
                    pass
            return saved_backup
        except ConfigError:
            raise
        except OSError as error:
            raise ConfigError("无法保存配置：%s", str(error)) from error
        finally:
            if tmp_name is not None:
                pathlib.Path(tmp_name).unlink(missing_ok=True)


def import_config(path):
    cfg, notices, _fingerprint = read_config(path)
    return copy.deepcopy(cfg), notices
