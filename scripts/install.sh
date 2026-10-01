#!/usr/bin/env bash
# myPanel 用户级安装 / 卸载（PRD §7）
#
#   ./scripts/install.sh              安装到 ~/.local/share
#   ./scripts/install.sh --uninstall  卸载
#
# 安装内容：
#   ~/.local/share/applications/myPanel.desktop
#   ~/.local/share/icons/hicolor/scalable/apps/myPanel.svg
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
DESKTOP_SRC="$ROOT/myPanel.desktop"
ICON_SRC="$ROOT/data/myPanel.svg"
LAUNCHER="$ROOT/scripts/run_panel.py"

uninstall() {
    rm -f "$APPS_DIR/myPanel.desktop" "$ICON_DIR/myPanel.svg"
    echo "已卸载 myPanel（配置文件 ~/.config/myPanel 未删除）"
    echo "如需一并删除配置：rm -rf ~/.config/myPanel"
    exit 0
}

[[ "${1:-}" == "--uninstall" ]] && uninstall

if [[ ! -f "$LAUNCHER" ]]; then
    echo "找不到启动脚本：$LAUNCHER" >&2
    exit 1
fi

# 依赖检查（PRD §7：依赖由系统包管理器满足）
if ! python3 -c "import PySide6" 2>/dev/null; then
    echo "缺少运行依赖 PySide6，请先安装：" >&2
    echo "  sudo apt install python3-pyside6.qtwidgets" >&2
    exit 1
fi

chmod +x "$LAUNCHER"

mkdir -p "$APPS_DIR" "$ICON_DIR"

# Exec 必须是绝对路径；用 | 作 sed 分隔符以容忍路径中的 /
sed "s|__EXEC__|$LAUNCHER|" "$DESKTOP_SRC" > "$APPS_DIR/myPanel.desktop"
chmod 644 "$APPS_DIR/myPanel.desktop"

install -m 644 "$ICON_SRC" "$ICON_DIR/myPanel.svg"

command -v gtk-update-icon-cache >/dev/null && \
    gtk-update-icon-cache -q -t -f "$DATA_HOME/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null && \
    update-desktop-database -q "$APPS_DIR" 2>/dev/null || true

echo "已安装 myPanel："
echo "  $APPS_DIR/myPanel.desktop"
echo "  $ICON_DIR/myPanel.svg"
echo "可在应用菜单中搜索 \"myPanel\" 启动，或直接运行：$LAUNCHER"
echo "卸载：$0 --uninstall"
