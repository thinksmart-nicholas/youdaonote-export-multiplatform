# other/ — 打包资源目录

存放打包所需的图标资源与生成脚本。

| 文件 | 用途 | 平台 |
| --- | --- | --- |
| `icon.png` | 1024×1024 源图 | Linux |
| `icon.ico` | 多尺寸（16/32/48/64/128/256） | Windows |
| `icon.icns` | 多尺寸（128/256/512/1024） | macOS |
| `icons/hicolor/<尺寸>/apps/YoudaoNoteExporter.png` | Freedesktop 图标主题多尺寸树（16/22/24/32/48/64/128/256/512） | Linux |
| `make_icon.py` | 图标生成脚本（Pillow 绘制，可重复执行） | — |

> Linux 的图标不嵌进可执行文件，`.desktop` 里写的是图标名 `YoudaoNoteExporter`，
> 桌面环境只从 hicolor 主题目录按尺寸查取。因此必须把 `icons/hicolor/` 安装到
> `~/.local/share/icons/hicolor/`（用户级）或 `/usr/share/icons/hicolor/`（系统级），
> 启动器才会显示正确图标，否则为通用占位图。安装命令见主 README 的
> 「安装 Linux 启动器图标」小节。

## 图标含义

蓝色渐变圆角方块背景，自上而下三个元素：

1. **云朵** —— 有道**云**笔记（云端）
2. **向下导出箭头** —— 导出动作
3. **叠放的两张笔记卡片**（带文字线）—— **批量**笔记

整体叙事：**从云端把一批笔记导出到本地**。

## 重新生成

```shell
python other/make_icon.py
```

依赖 `Pillow`（`pip install pillow`）。改动构图后重新运行即可同步更新三种格式。
