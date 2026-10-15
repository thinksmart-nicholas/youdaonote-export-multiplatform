#!/usr/bin/env bash
#
# 有道云笔记批量导出工具 - Linux 手动安装脚本
#
# 用法：把本脚本与以下 3 个 Release 产物放在同一个目录，然后执行：
#   bash install.sh
# 需要的同目录文件：
#   - YoudaoNoteExporter-Linux   （可执行程序）
#   - YoudaoNoteExporter.desktop （启动器）
#   - icons-hicolor.tar.gz       （hicolor 多尺寸图标树）
#
# 安装内容（均按当前用户，装到 ~/.local）：
#   - 可执行文件 -> ~/.local/bin/YoudaoNoteExporter
#   - 图标      -> ~/.local/share/icons/hicolor（经 xdg-icon-resource，自动建 index.theme + 刷缓存）
#   - 启动器    -> ~/.local/share/applications/YoudaoNoteExporter.desktop（Exec/Icon 写成本机绝对路径）
#   - 刷新启动器数据库，让应用菜单出现本程序

set -euo pipefail

APP="YoudaoNoteExporter"
BIN="YoudaoNoteExporter-Linux"
DESKTOP="YoudaoNoteExporter.desktop"
ICONS_TAR="icons-hicolor.tar.gz"

# 以脚本所在目录为准（即使从其它目录调用也能找到同目录的产物）
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SRC_DIR"

err() { echo "错误：$*" >&2; exit 1; }

[ -f "$BIN" ]       || err "找不到 $BIN，请把它与本脚本放在同一目录"
[ -f "$DESKTOP" ]   || err "找不到 $DESKTOP，请把它与本脚本放在同一目录"
[ -f "$ICONS_TAR" ] || err "找不到 $ICONS_TAR，请把它与本脚本放在同一目录"

# 1) 可执行文件
mkdir -p ~/.local/bin
install -m 755 "$BIN" ~/.local/bin/"$APP"
echo "已安装可执行文件：~/.local/bin/$APP"

# 2) 图标：解压后逐个尺寸经 xdg-icon-resource 安装（自动建 index.theme + 刷缓存）
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
tar -xzf "$ICONS_TAR" -C "$TMP"
ICONS_ROOT="$TMP/icons/hicolor"

if command -v xdg-icon-resource >/dev/null 2>&1; then
  for s in 16 22 24 32 48 64 128 256 512; do
    png="$ICONS_ROOT/${s}x${s}/apps/$APP.png"
    [ -f "$png" ] || continue
    xdg-icon-resource install --size "$s" --novendor "$png" "$APP" || true
  done
  echo "已安装图标（hicolor 主题，经 xdg-icon-resource）"
else
  # 兜底：直接拷贝（需目标主题有 index.theme，否则图标名可能查不到）
  for png in "$ICONS_ROOT"/*x*/apps/$APP.png; do
    [ -f "$png" ] || continue
    size="$(basename "$(dirname "$(dirname "$png")")")"
    dest="$HOME/.local/share/icons/hicolor/$size/apps"
    mkdir -p "$dest"
    cp "$png" "$dest/" || true
  done
  theme="$HOME/.local/share/icons/hicolor"
  [ -f "$theme/index.theme" ] || printf '[Icon Theme]\nName=hicolor\nDirectories=16x16/apps,22x22/apps,24x24/apps,32x32/apps,48x48/apps,64x64/apps,128x128/apps,256x256/apps,512x512/apps\n' > "$theme/index.theme"
  gtk-update-icon-cache -f "$theme" 2>/dev/null || true
  echo "已安装图标（直接拷贝兜底）"
fi

# 找到已安装的图标绝对路径，写进 .desktop 的 Icon=，确保一定能解析到
ICON_ABS=""
for s in 256 512 128 64 48; do
  cand="$HOME/.local/share/icons/hicolor/${s}x${s}/apps/$APP.png"
  [ -f "$cand" ] && { ICON_ABS="$cand"; break; }
done
[ -n "$ICON_ABS" ] || ICON_ABS="$APP"   # 极端兜底：退回图标名查找

# 3) 启动器：把 Exec / Icon 改写为本机绝对路径（更稳妥，避免依赖 PATH 与主题名查找）
APPLICATIONS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPLICATIONS_DIR"
DEST_DESKTOP="$APPLICATIONS_DIR/$APP.desktop"
sed -e "s|^Exec=.*|Exec=$HOME/.local/bin/$APP|" \
    -e "s|^Icon=.*|Icon=$ICON_ABS|" \
    "$DESKTOP" > "$DEST_DESKTOP"
chmod 644 "$DEST_DESKTOP"
echo "已安装启动器：$DEST_DESKTOP"

# 4) 刷新启动器数据库，让应用菜单出现本程序
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPLICATIONS_DIR" || true
fi

echo
echo "安装完成。若应用菜单未立即出现本程序，请注销后重新登录。"
