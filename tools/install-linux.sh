#!/usr/bin/env bash
# Linux 安装脚本：把构建产物安装到标准位置，并让启动器图标真正生效。
#
# 为什么需要它（Linux 图标机制与 Windows/macOS 不同）：
#   - 图标不嵌进可执行文件，也不会因为 .desktop 放在程序旁边就生效
#   - 桌面环境只扫描 applications/ 目录下的 .desktop，dist/ 里那份不会被扫到
#   - .desktop 若写「图标名」，需要主题已注册且图标缓存已刷新才可能命中
#
# 本脚本采用「绝对路径」策略，绕开上述不确定性：
#   Exec=/绝对路径/bin/YoudaoNoteExporter          （不依赖 PATH）
#   Icon=/绝对路径/.../YoudaoNoteExporter.png      （不依赖主题注册与图标缓存）
# 同时仍会把 hicolor 多尺寸树装到主题目录，供其它集成（如 Dock 关联）使用。
#
# 用法（仓库根目录，构建之后）：
#     bash tools/install-linux.sh                 # 默认取 ./dist，装到 ~/.local
#     bash tools/install-linux.sh /path/to/dist   # 指定产物目录
#     sudo PREFIX=/usr/local bash tools/install-linux.sh   # 系统级（所有用户）
#     bash tools/install-linux.sh --uninstall     # 卸载
set -euo pipefail

APP=YoudaoNoteExporter
PREFIX="${PREFIX:-$HOME/.local}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
THEME_DIR="$PREFIX/share/icons/hicolor"

# ---------- 卸载 ----------
if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$PREFIX/bin/$APP" "$PREFIX/share/applications/$APP.desktop"
  find "$THEME_DIR" -type f -name "$APP.png" -delete 2>/dev/null || true
  gtk-update-icon-cache -f "$THEME_DIR" 2>/dev/null || true
  update-desktop-database "$PREFIX/share/applications" 2>/dev/null || true
  echo "已卸载 $APP（$PREFIX）"
  exit 0
fi

SRC="${1:-$REPO_ROOT/dist}"
if [ ! -f "$SRC/$APP" ]; then
  echo "未找到 $SRC/$APP" >&2
  echo "请先构建：python3 build.py" >&2
  exit 1
fi

# ---------- 1) 可执行文件 ----------
install -Dm755 "$SRC/$APP" "$PREFIX/bin/$APP"

# ---------- 2) hicolor 多尺寸图标 ----------
if [ ! -d "$SRC/icons/hicolor" ]; then
  echo "错误：未找到 $SRC/icons/hicolor" >&2
  echo "请重新构建（build.py 会把图标树拷进产物）：python3 build.py" >&2
  exit 1
fi

find "$SRC/icons/hicolor" -type f -name "$APP.png" -print0 | while IFS= read -r -d '' png; do
  size="$(basename "$(dirname "$(dirname "$png")")")"
  install -Dm644 "$png" "$THEME_DIR/$size/apps/$APP.png"
done

# 补全 index.theme：没有它，该目录不是一个合法的图标主题
if [ ! -f "$THEME_DIR/index.theme" ]; then
  dirs=""
  for d in "$THEME_DIR"/*/apps; do
    [ -d "$d" ] || continue
    dirs="$dirs$(basename "$(dirname "$d")")/apps,"
  done
  {
    echo "[Icon Theme]"
    echo "Name=Hicolor"
    echo "Comment=Fallback icon theme"
    echo "Directories=${dirs%,}"
    echo ""
    for d in "$THEME_DIR"/*/apps; do
      [ -d "$d" ] || continue
      s="$(basename "$(dirname "$d")")"
      echo "[$s/apps]"
      echo "Size=${s%x*}"
      echo "Context=Applications"
      echo "Type=Threshold"
      echo ""
    done
  } > "$THEME_DIR/index.theme"
fi

gtk-update-icon-cache -f "$THEME_DIR" 2>/dev/null || true

# ---------- 3) .desktop：改写为绝对路径 ----------
for s in 512 256 128 64 48 32 24 22 16; do
  if [ -f "$THEME_DIR/${s}x${s}/apps/$APP.png" ]; then
    ICON_ABS="$THEME_DIR/${s}x${s}/apps/$APP.png"
    break
  fi
done
if [ -z "${ICON_ABS:-}" ]; then
  echo "错误：图标安装失败，未找到任何 $APP.png" >&2
  exit 1
fi

if [ -f "$SRC/$APP.desktop" ]; then
  mkdir -p "$PREFIX/share/applications"
  sed -e "s|^Exec=.*|Exec=$PREFIX/bin/$APP|" \
      -e "s|^Icon=.*|Icon=$ICON_ABS|" \
      "$SRC/$APP.desktop" > "$PREFIX/share/applications/$APP.desktop"
  chmod 644 "$PREFIX/share/applications/$APP.desktop"
else
  echo "警告：未找到 $SRC/$APP.desktop，已跳过（请先重新构建）" >&2
fi

update-desktop-database "$PREFIX/share/applications" 2>/dev/null || true

# ---------- 4) 校验与提示 ----------
n="$(find "$THEME_DIR" -type f -name "$APP.png" | wc -l | tr -d ' ')"
if [ "$n" -eq 0 ]; then
  echo "错误：未安装任何图标" >&2
  exit 1
fi

echo "安装完成："
echo "  可执行文件：$PREFIX/bin/$APP"
echo "  启动器：$PREFIX/share/applications/$APP.desktop"
echo "  图标尺寸数：$n（使用 $ICON_ABS）"
echo ""
echo "若应用菜单里图标仍未更新，可执行以下任一操作后重试："
echo "  1) 注销并重新登录"
echo "  2) gtk-update-icon-cache -f $THEME_DIR"
echo "也可直接运行验证：$PREFIX/bin/$APP"
