# myPanel

面向 Ubuntu 26.04 / Xubuntu 的文件、文件夹和桌面应用快捷面板。产品需求见 [PRD](myPanel-Linux-PRD.md)。

## 从源码运行

在已有 Python 3、PySide6 QtWidgets、PyGObject/GIO 和 UDisks2 的桌面环境中，从仓库根目录执行：

```sh
python3 scripts/run_panel.py
```

本轮实际验证环境为 Ubuntu 26.04、Python 3.14.4、PySide6/Qt 6.10.2。文件打开、应用启动和图标元数据使用 GIO；外接存储列表和设备操作使用 UDisks2 系统 D-Bus 接口。不需要以 root 运行应用。

设置中可导入旧版 JSON 配置、切换主题和语言；语言在重启后生效。每个已配置槽位的 `↻` 按钮可重新选择目标。失效路径不会自动清除。系统不支持断电/弹出时，会明确显示“仅卸载”；断电失败不会显示为安全弹出成功。

## 配置格式和保护

配置位于 `${XDG_CONFIG_HOME:-$HOME/.config}/myPanel/myPanel.json`。新版增加 `schemaVersion: 2`，保留原有兼容字段，固定保存 200 个槽位；索引为 `row * 4 + column`。隐藏行、隐藏列在缩小面板和重启后保留。

- 旧版 9 项数组，或 `itemCount: 9` 且只有 9 项的对象，映射到第一列的索引 `0, 4, …, 32`。
- 没有 `itemCount` 的 36 项配置推断为每列 9 项，按行优先顺序保存；缺少 `isLargePanel` 时仍默认单列。
- 无法判断布局的 51 项数组全部按原顺序保留，并提示增大面板查看；不会截断为 50 项。
- 超过 200 项、超过 2 MiB、结构损坏或不支持的版本会被拒绝。损坏配置不会被默认值自动覆盖。
- 保存使用同目录临时文件、原子替换和进程锁；检测到其他进程修改时拒绝覆盖。
- 导入和明确恢复前创建唯一备份。备份失败时不覆盖原文件；导入不会修改来源文件。

## 回归验证

测试使用标准库 `unittest`，无需 pytest。在 Linux 的上述运行依赖已满足时执行：

```sh
QT_QPA_PLATFORM=offscreen PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  python3 -B -m unittest discover -s tests -v
```

覆盖配置迁移、损坏文件保护、原子保存、并发覆盖防护、导入、隐藏槽位恢复、语言和应用名称、启动项选择、异步超时及控件销毁、设备分组和安全弹出失败反馈。设备测试使用模拟对象，不对真实磁盘执行卸载或断电。

离屏测试不能替代 Xfce X11/Wayland 中的文件选择、默认应用启动、主题图标及真实 USB 插拔/弹出的人工验收。安装脚本、`.desktop` 安装入口和 `.deb` 打包（本次建议第 8 项）保持本轮范围之外。

## 源码结构

- `config_store.py`：数据校验、迁移和持久化。
- `desktop_services.py`：GIO 桌面元数据、后台任务和应用启动。
- `volume_service.py`：UDisks2 监控、设备分组和异步移除。
- `window.py`：Qt 界面与交互。
- `localization.py` / `locales/`：gettext 和英语目录；Qt 标准对话框使用 Qt 翻译。
- `metadata.py` / `data/app.json`：应用版本、作者和日期。
- `main.py`：应用启动和主题、语言初始化。
